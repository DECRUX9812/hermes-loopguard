"""Hermes surface for the computer-use agent (X3).

`register_cu(ctx, get_cfg)` registers:

- the `jev_computer` tool (run the whole agent loop from one tool call) and
  `loopguard_cu_status`;
- hooks on the STOCK `computer_use` tool: `pre_tool_call` safety gate before
  mutating actions (block/approve only, like the phase-1 tool gate — it never
  weakens Hermes's own approval rules) and `transform_tool_result` appending
  `VERIFIED` / `NOT VERIFIED: <reason>` when an expectation is available.

Everything is built lazily at call time; `register_cu` stays fast and never
raises (no cua-driver, no Pillow, no key -> the tools just report
unavailable).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

__all__ = ["register_cu", "MUTATING_ACTIONS"]

# Stock computer_use actions that can change app/desktop state (capture,
# list_apps, list_windows, focus_app and wait are read-only or inert).
MUTATING_ACTIONS = frozenset(
    {"click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value"}
)
_TYPING_ACTIONS = frozenset({"type", "set_value"})
_SAFETY_CACHE: dict[int, tuple[float, Any]] = {}
_SAFETY_CACHE_TTL_S = 120.0

JEV_COMPUTER_SCHEMA = {
    "name": "jev_computer",
    "description": (
        "Complete a task on the computer (Jev + cua-driver): plans the task into steps, "
        "grounds targets on the accessibility tree or with Jev bounding boxes, acts, verifies "
        "each step with cheap typed vision calls and pauses for approval before anything risky "
        "(payment, deletion, account changes, unsanctioned sending). Returns a compact summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "What to do, e.g. 'Fill the contact form with Ada Lovelace and submit'.",
            },
            "app": {
                "type": "string",
                "description": "Optional app name to scope actions to (e.g. 'chrome').",
            },
            "max_steps": {"type": "integer", "description": "Step cap (default from config, 12)."},
            "budget_usd": {"type": "number", "description": "Spend cap in USD (default from config, 0.50)."},
        },
        "required": ["instruction"],
    },
}

STATUS_SCHEMA = {
    "name": "loopguard_cu_status",
    "description": "LoopGuard computer-use status: whether jev_computer is enabled, its models, driver and recent runs.",
    "parameters": {"type": "object", "properties": {}},
}


def _cu_cfg(get_cfg: Callable[[], dict]) -> dict:
    try:
        return (get_cfg() or {}).get("computer_use") or {}
    except Exception:
        return {}


def _enabled(cu: dict) -> bool:
    return bool(cu.get("enabled"))


def _check_tool(cu: dict) -> bool:
    return _enabled(cu)


def _build_driver(cu: dict, app: str | None = None):
    from hermes_loopguard.cu.driver import get_driver

    drv = dict(cu.get("driver") or {})
    name = str(drv.pop("name", "cua") or "cua")
    if app:
        drv["app"] = app
    elif drv.get("app") is None:
        drv.pop("app", None)
    return get_driver(name, **drv)


def _build_gate(cu: dict):
    from hermes_loopguard.cu.vision import VisionGate

    return VisionGate(cu)


def _build_logger(cu: dict, store_factory: Callable[[], Any] | None):
    from hermes_loopguard.cu.log import CuRunLogger

    store = None
    if store_factory is not None:
        try:
            store = store_factory()
        except Exception:
            store = None
    frames_cfg = cu.get("frames") or {}
    return CuRunLogger(store, enabled=bool(cu.get("log", True)), retention=frames_cfg.get("retention", 20))


def _fmt_model_calls(calls: dict[str, int]) -> str:
    return ", ".join(f"{m} x{n}" for m, n in sorted(calls.items())) or "none"


def _summary(result: Any) -> str:
    lines = [
        f"jev_computer: state={result.state}, steps={len(result.steps)}, "
        f"cost=${result.usd:.4f} ({result.input_tokens} in / {result.output_tokens} out tokens), "
        f"wall={result.wall_s:.0f}s",
        f"model calls: {_fmt_model_calls(result.calls_per_model)}; "
        f"big-model calls: {result.big_model_calls}, avoided: {result.big_model_avoided}",
    ]
    if result.reason:
        lines.append(f"reason: {result.reason}")
    if result.done_check is not None:
        lines.append(f"final goal check: noul={result.done_check:.2f}")
    for rec in result.steps:
        line = f"  {rec.idx + 1}. {rec.action} {rec.target!r} [{rec.grounding}] -> {rec.verdict}"
        if rec.noul is not None:
            line += f" (noul={rec.noul:.2f})"
        if rec.effect:
            line += f" effect={rec.effect}"
        if rec.stall:
            line += f" stall={rec.stall}"
        lines.append(line)
    return "\n".join(lines)


# --- tool handlers ----------------------------------------------------------------


def _jev_computer_handler(args: dict, get_cfg: Callable[[], dict], store_factory=None, **kwargs) -> str:
    args = args if isinstance(args, dict) else {}
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        return "jev_computer: an 'instruction' is required."
    cu = _cu_cfg(get_cfg)
    if not _enabled(cu):
        return "jev_computer is unavailable: loopguard.computer_use.enabled is false."
    try:
        from hermes_loopguard.cu.agent import run_task

        driver = _build_driver(cu, app=args.get("app"))
    except Exception as e:
        return f"jev_computer is unavailable: {e}"
    cfg = dict(cu)
    if args.get("max_steps"):
        cfg["max_steps"] = int(args["max_steps"])
    if args.get("budget_usd"):
        cfg["budget_usd"] = float(args["budget_usd"])
    try:
        gate = _build_gate(cu)
        run_log = _build_logger(cu, store_factory)
        try:
            result = run_task(instruction, driver, gate, cfg, log=run_log)
        finally:
            try:
                driver.close()
            except Exception:
                pass
        return _summary(result)
    except Exception as e:
        return f"jev_computer failed: {e}"


def _status_handler(args: dict, get_cfg: Callable[[], dict], store_factory=None, **kwargs) -> str:
    cu = _cu_cfg(get_cfg)
    if not _enabled(cu):
        return "loopguard computer_use: disabled (set loopguard.computer_use.enabled)."
    lines = [
        f"loopguard computer_use: enabled, shadow={bool(cu.get('shadow'))}",
        f"driver: {json.dumps(cu.get('driver') or {})}",
        f"vision models: {', '.join(cu.get('models') or [])}",
        f"planner: {cu.get('planner_model')}; escalation: {(cu.get('escalation') or {}).get('model')}",
    ]
    try:
        run_log = _build_logger(cu, store_factory)
        runs = run_log.recent_runs(5)
        if runs:
            lines.append("recent runs:")
            for run in runs:
                lines.append(
                    f"  #{run.get('id')} {run.get('state')} steps={run.get('steps')} "
                    f"usd=${float(run.get('usd') or 0):.4f} {run.get('reason') or ''}"
                )
    except Exception:
        pass
    return "\n".join(lines)


# --- hooks on the stock computer_use tool -------------------------------------------


def _safety_cached_ttl(gate, frame):
    """gate.safety memoized by frame dhash with a small TTL."""
    from hermes_loopguard.cu import frames

    try:
        key: int | None = frames.dhash(frame.png)
    except (RuntimeError, ValueError):
        key = None
    now = time.time()
    if key is not None:
        hit = _SAFETY_CACHE.get(key)
        if hit and now - hit[0] < _SAFETY_CACHE_TTL_S:
            return hit[1]
    result = gate.safety(frame)
    if key is not None:
        if len(_SAFETY_CACHE) > 32:
            _SAFETY_CACHE.clear()
        _SAFETY_CACHE[key] = (now, result)
    return result


def make_cu_pre_tool_call(get_cfg: Callable[[], dict], store_factory=None) -> Callable:
    """pre_tool_call hook: safety gate before stock computer_use mutating
    actions.  Block/approve only — same contract as the phase-1 tool gate,
    never weakens Hermes's own rules.  Shadow mode logs the verdict and
    approves."""
    from hermes_loopguard.cu.agent import safety_decision

    def hook(tool_name=None, args=None, **kwargs):
        try:
            cu = _cu_cfg(get_cfg)
            if not _enabled(cu) or tool_name != "computer_use":
                return None
            action = (args or {}).get("action") or ""
            if action not in MUTATING_ACTIONS:
                return None
            driver = _build_driver(cu)
            try:
                frame = driver.screenshot()
            finally:
                driver.close()
            gate = _build_gate(cu)
            result = _safety_cached_ttl(gate, frame)
            decision = safety_decision(result, authorized=set(), threshold=gate.thresholds["safety"])
            shadow = bool(cu.get("shadow"))
            if shadow:
                log.info("loopguard cu safety (shadow): pause=%s %s", decision.pause, decision.reason)
                return None
            if decision.pause:
                return {
                    "action": "block",
                    "reason": f"loopguard safety gate: {decision.reason}",
                }
            return None
        except Exception as e:
            log.debug("loopguard cu pre_tool_call error: %s", e)
            return None

    hook.__name__ = "loopguard_cu_pre_tool_call"
    return hook


def make_cu_transform_result(get_cfg: Callable[[], dict]) -> Callable:
    """transform_tool_result hook: append VERIFIED / NOT VERIFIED when an
    expectation is derivable (typing actions).  Shadow mode logs only."""
    from hermes_loopguard.cu.agent import _usage_failed

    def hook(tool_name=None, args=None, result=None, **kwargs):
        try:
            cu = _cu_cfg(get_cfg)
            if not _enabled(cu) or tool_name != "computer_use" or not isinstance(result, str):
                return None
            action = (args or {}).get("action") or ""
            if action not in _TYPING_ACTIONS:
                return None
            text = (args or {}).get("text") or (args or {}).get("value")
            if not text:
                return None
            expectation = f"the typed text {text!r} is visible on screen"
            driver = _build_driver(cu)
            try:
                frame = driver.screenshot()
            finally:
                driver.close()
            gate = _build_gate(cu)
            verify = gate.verify(frame, expectation)
            shadow = bool(cu.get("shadow"))
            if verify.passed:
                suffix = "VERIFIED"
            else:
                reason = (
                    f"yes={verify.yes:.2f}"
                    if not _usage_failed(verify.usage)
                    else f"gate error: {', '.join(verify.usage.fallbacks or ['all models failed'])}"
                )
                suffix = f"NOT VERIFIED: {reason}"
            if shadow:
                log.info("loopguard cu verify (shadow): %s", suffix)
                return None
            return f"{result}\n[loopguard {suffix}]"
        except Exception as e:
            log.debug("loopguard cu transform_tool_result error: %s", e)
            return None

    hook.__name__ = "loopguard_cu_transform_result"
    return hook


# --- registration --------------------------------------------------------------------


def register_cu(ctx, get_cfg: Callable[[], dict], store_factory: Callable[[], Any] | None = None) -> None:
    """Register the jev_computer tools and the stock computer_use hooks.

    Fast, and never raises: any failure only logs (tools then simply report
    unavailable).
    """
    try:
        cu = _cu_cfg(get_cfg)
    except Exception:
        cu = {}
    check = lambda: _check_tool(_cu_cfg(get_cfg))  # noqa: E731

    try:
        ctx.register_tool(
            name="jev_computer",
            toolset="loopguard",
            schema=JEV_COMPUTER_SCHEMA,
            handler=lambda args, **kw: _jev_computer_handler(args, get_cfg, store_factory, **kw),
            check_fn=check,
            description="Drive the computer with cheap Jev vision gates (LoopGuard)",
        )
    except Exception:
        log.warning("LoopGuard: jev_computer registration failed", exc_info=True)

    try:
        ctx.register_tool(
            name="loopguard_cu_status",
            toolset="loopguard",
            schema=STATUS_SCHEMA,
            handler=lambda args, **kw: _status_handler(args, get_cfg, store_factory, **kw),
            check_fn=check,
            description="LoopGuard computer-use status",
        )
    except Exception:
        log.warning("LoopGuard: loopguard_cu_status registration failed", exc_info=True)

    try:
        ctx.register_hook("pre_tool_call", make_cu_pre_tool_call(get_cfg, store_factory))
    except Exception:
        log.debug("LoopGuard: cu pre_tool_call hook registration failed", exc_info=True)

    try:
        ctx.register_hook("transform_tool_result", make_cu_transform_result(get_cfg))
    except Exception:
        log.debug("LoopGuard: cu transform_tool_result hook registration failed", exc_info=True)
