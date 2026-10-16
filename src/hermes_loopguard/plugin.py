"""Hermes plugin entry point: `hermes_agent.plugins` key `loopguard`.

`register(ctx)` must be fast (no DB open, no network) and must never raise
because of a LoopGuard config error, so a broken watcher file can never stop
Hermes from starting.
"""
from __future__ import annotations

import logging
import os
import threading

from hermes_loopguard import paths

log = logging.getLogger(__name__)

_STORE = None
_STORE_KEY = None
_STORE_LOCK = threading.Lock()

_CFG_CACHE: dict = {"key": None, "cfg": None}
_CFG_LOCK = threading.Lock()


def _config_file():
    config_dir = paths.config_dir()
    for name in ("config.yaml", "config.yml"):
        candidate = config_dir / name
        if candidate.is_file():
            return candidate
    return config_dir / "config.yaml"


def get_cfg() -> dict:
    """LoopGuard config, reloaded only when a config file changes.

    Layers the values saved in Settings > Plugins
    (``plugins.entries.loopguard.settings``, read through Hermes's own config
    loader) over ``<HERMES_HOME>/loopguard/config.yaml``. Only keys the user
    actually saved are applied, so existing config-file users keep their
    values, and the cron shim (which reads this same function) follows the
    settings too.
    """
    try:
        path = _config_file()
        hermes_cfg = paths.hermes_home() / "config.yaml"
        key = (
            (str(path), path.stat().st_mtime if path.is_file() else None),
            (str(hermes_cfg), hermes_cfg.stat().st_mtime if hermes_cfg.is_file() else None),
        )
    except Exception:
        key = (None, None)
    with _CFG_LOCK:
        if _CFG_CACHE["cfg"] is not None and _CFG_CACHE["key"] == key:
            return _CFG_CACHE["cfg"]

    from hermes_loopguard.config import apply_plugin_settings, hermes_plugin_settings, load_config

    cfg = apply_plugin_settings(load_config(), hermes_plugin_settings())
    with _CFG_LOCK:
        _CFG_CACHE["cfg"] = cfg
        _CFG_CACHE["key"] = key
    return cfg


def store_factory():
    """One Store per process, opened lazily and keyed by the resolved DB path
    (the path depends on HERMES_HOME, which is profile-scoped)."""
    global _STORE, _STORE_KEY
    try:
        key = str(paths.db_path())
    except Exception:
        key = None
    with _STORE_LOCK:
        if _STORE is not None and _STORE_KEY == key:
            return _STORE
        from hermes_loopguard.db import Store

        previous = _STORE
        _STORE = Store.open_default()
        _STORE_KEY = key
    if previous is not None:
        try:
            previous.close()
        except Exception:
            log.debug("LoopGuard: could not close the previous store", exc_info=True)
    return _STORE


class _LazyLlm:
    """Forwards to the live Hermes plugin context LLM (resolved per call)."""

    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._ctx.llm, name)


def _hook_exists(name: str) -> bool:
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return name in VALID_HOOKS
    except Exception:
        return True


def register(ctx) -> None:
    try:
        _register(ctx)
    except Exception:
        log.warning("LoopGuard plugin registration failed", exc_info=True)


def _register(ctx) -> None:
    from hermes_loopguard import cli, tool_gate
    from hermes_loopguard.backends import hermes_llm
    from hermes_loopguard.ledger import Ledger

    try:
        hermes_llm.set_llm(_LazyLlm(ctx))
    except Exception:
        log.debug("LoopGuard: could not install the lazy LLM shim", exc_info=True)

    if hasattr(ctx, "register_auxiliary_task"):
        try:
            ctx.register_auxiliary_task(
                "loopguard_gate",
                display_name="LoopGuard gate",
                description=(
                    "Cheap typed classifier that decides whether an automation wakes "
                    "the main agent."
                ),
            )
        except Exception:
            log.debug("LoopGuard: auxiliary task registration failed", exc_info=True)

    try:
        ctx.register_cli_command(
            name="loopguard",
            help="Stop automations from burning tokens",
            setup_fn=cli.setup_parser,
            handler_fn=cli.handle,
        )
    except Exception:
        log.warning("LoopGuard: CLI command registration failed", exc_info=True)

    if hasattr(ctx, "register_command"):
        try:
            ctx.register_command(
                "loopguard",
                handler=lambda raw_args="": _slash_stats(),
                description="LoopGuard savings",
            )
        except Exception:
            log.debug("LoopGuard: slash command registration failed", exc_info=True)

    def cron_execution():
        if not hasattr(ctx, "current_cron_execution"):
            return None
        try:
            return ctx.current_cron_execution()
        except Exception:
            return None

    def in_automation() -> bool:
        if os.environ.get("HERMES_KANBAN_TASK"):
            return True
        return cron_execution() is not None

    ledger = Ledger(store_factory, get_cfg, cron_execution=cron_execution)

    ledger_hooks = {
        "post_api_request": ledger.post_api_request,
        "post_llm_call": ledger.post_llm_call,
        "on_session_end": ledger.on_session_end,
        "kanban_task_claimed": ledger.kanban_task_claimed,
        "kanban_task_completed": ledger.kanban_task_completed,
        "kanban_task_blocked": ledger.kanban_task_blocked,
        "on_kanban_worker_exited": ledger.on_kanban_worker_exited,
    }
    for name, handler in ledger_hooks.items():
        _register_hook(ctx, name, handler)

    from hermes_loopguard import backends, tool_gate

    tool_hook = tool_gate.make_pre_tool_call_hook(
        get_cfg, in_automation=in_automation, store_factory=store_factory,
        semantic=tool_gate.make_semantic_classifier(lambda: backends.backend_chain(get_cfg())),
    )

    def pre_tool_call(tool_name=None, args=None, **kwargs):
        block = None
        approve = None
        try:
            budget = ledger.budget_guard(tool_name=tool_name, args=args, **kwargs)
        except Exception:
            budget = None
        if isinstance(budget, dict):
            if budget.get("action") == "block":
                block = budget
            elif budget.get("action") == "approve":
                approve = budget
        try:
            verdict = tool_hook(tool_name=tool_name, args=args, **kwargs)
        except Exception:
            verdict = None
        if isinstance(verdict, dict):
            if verdict.get("action") == "block":
                block = verdict
            elif verdict.get("action") == "approve" and approve is None:
                approve = verdict
        return block or approve

    _register_hook(ctx, "pre_tool_call", pre_tool_call)

    from hermes_loopguard import completion

    verify_hook = completion.make_pre_verify_hook(
        get_cfg,
        store_factory=store_factory,
        chain_factory=lambda: backends.backend_chain(get_cfg()),
    )
    _register_hook(ctx, "pre_verify", verify_hook)

    # X3: jev_computer tools + stock computer_use hooks (never raises).
    try:
        from hermes_loopguard.cu.hermes_tool import register_cu

        register_cu(ctx, get_cfg, store_factory=store_factory)
    except Exception:
        log.warning("LoopGuard: computer-use registration failed", exc_info=True)


def _register_hook(ctx, name: str, handler) -> None:
    if not _hook_exists(name):
        log.debug("LoopGuard: Hermes has no hook named %s; skipping", name)
        return
    try:
        ctx.register_hook(name, handler)
    except Exception:
        log.debug("LoopGuard: hook %s registration failed", name, exc_info=True)


def _slash_stats() -> str:
    from hermes_loopguard.stats import compute_stats, format_headline, format_watcher_table

    stats = compute_stats(store_factory(), get_cfg())
    return "\n".join([format_headline(stats)] + format_watcher_table(stats))
