"""Unit tests for the Hermes surface (X3): jev_computer tool registration,
status tool, and the hooks on the stock computer_use tool.  All fakes; the
db round-trip uses the real Store against a temp LOOPGUARD_DB."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hermes_loopguard.cu import agent, hermes_tool
from hermes_loopguard.cu.drivers.fake import FakeDriver
from hermes_loopguard.cu.log import CuRunLogger
from hermes_loopguard.cu.driver import Frame
from hermes_loopguard.cu.vision import SafetyResult, VerifyResult, VisionUsage

FIX = Path(__file__).parent / "fixtures" / "cu" / "vision"


class FakeGate:
    """FAKE gate: canned answers for the hook tests."""

    def __init__(self):
        self.thresholds = {"presence": 0.6, "box_confidence": 0.5, "verify": 0.7, "safety": 0.2}
        self.verify_result = VerifyResult(passed=True, yes=0.95, usage=VisionUsage(model="jev-1.14.0"))
        self.safety_result = SafetyResult(category="none", probs={"none": 0.99},
                                          confirm_dialog=0.0, pause=False, usage=None)

    def verify(self, after, expected, *, before=None):
        return self.verify_result

    def safety(self, frame):
        return self.safety_result

    def locate(self, frame, target, *, region=None):
        return LocateResult(found=False, presence=0, confidence=0, box=None, center=None,
                            grounding="none", usage=VisionUsage())

    def changed(self, before, after):
        from hermes_loopguard.cu.vision import ChangeResult

        return ChangeResult(changed=True, distance=20, path="hash", usage=None)

    def ask(self, images, state, questions):
        return {n: NoulAnswer(noul=0.9) for n in questions}, VisionUsage()


class FakeCtx:
    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.hooks: dict[str, list] = {}

    def register_tool(self, name=None, toolset=None, schema=None, handler=None,
                      check_fn=None, **kwargs):
        self.tools[name] = {
            "schema": schema, "handler": handler, "check_fn": check_fn, "toolset": toolset,
        }

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)


def cfg(enabled: bool = True, **extra) -> dict:
    base = {"computer_use": {"enabled": enabled}}
    base["computer_use"].update(extra)
    return base


# --------------------------------------------------------------------- registration


def test_register_cu_registers_tools_and_hooks():
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    assert set(ctx.tools) == {"jev_computer", "loopguard_cu_status"}
    assert ctx.tools["jev_computer"]["toolset"] == "loopguard"
    params = ctx.tools["jev_computer"]["schema"]["parameters"]
    assert params["required"] == ["instruction"]
    assert set(ctx.hooks) == {"pre_tool_call", "transform_tool_result"}


def test_register_cu_never_raises_on_broken_config():
    ctx = FakeCtx()

    def broken():
        raise RuntimeError("config blew up")

    hermes_tool.register_cu(ctx, broken)  # must not raise
    assert set(ctx.tools) == {"jev_computer", "loopguard_cu_status"}


def test_register_cu_never_raises_without_hermes_shape():
    """A ctx without register_hook (older Hermes) still registers the tools."""
    class _NoHooks:
        def __init__(self):
            self.tools = {}

        def register_tool(self, **kwargs):
            self.tools[kwargs.get("name")] = kwargs

    ctx = _NoHooks()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    assert set(ctx.tools) == {"jev_computer", "loopguard_cu_status"}


def test_check_fn_reflects_enabled():
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(False))
    assert ctx.tools["jev_computer"]["check_fn"]() is False


# --------------------------------------------------------------------- handlers


def test_jev_computer_disabled_reports_unavailable():
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(False))
    out = ctx.tools["jev_computer"]["handler"]({"instruction": "do it"})
    assert "unavailable" in out and "enabled" in out


def test_jev_computer_requires_instruction():
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    assert "instruction" in ctx.tools["jev_computer"]["handler"]({})


def test_jev_computer_handler_returns_summary(monkeypatch):
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))

    def fake_run_task(instruction, driver, gate, cfg, *, log=None):
        rec = agent.StepRecord(idx=0, action="click", target="Submit", grounding="a11y",
                               verdict="pass", noul=0.93)
        return agent.RunResult(
            state="paused_for_approval", steps=[rec], calls_per_model={"jev-1.14.0": 2},
            input_tokens=200, output_tokens=20, usd=0.001, wall_s=12.0,
            reason="payment risk 0.90; frame=/tmp/x.png",
        )

    monkeypatch.setattr(agent, "run_task", fake_run_task)
    monkeypatch.setattr(hermes_tool, "_build_driver",
                        lambda cu, app=None: type("D", (), {"close": lambda self: None})())
    out = ctx.tools["jev_computer"]["handler"]({"instruction": "buy thing"})
    assert "state=paused_for_approval" in out
    assert "payment risk" in out and "/tmp/x.png" in out
    assert "jev-1.14.0 x2" in out


def test_jev_computer_handler_failure_is_text_not_raise(monkeypatch):
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    monkeypatch.setattr(hermes_tool, "_build_driver",
                        lambda cu, app=None: (_ for _ in ()).throw(RuntimeError("no cua-driver")))
    out = ctx.tools["jev_computer"]["handler"]({"instruction": "x"})
    assert "unavailable" in out


def test_status_handler_disabled_and_enabled():
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(False))
    assert "disabled" in ctx.tools["loopguard_cu_status"]["handler"]({})
    hermes_tool.register_cu(ctx, lambda: cfg(True, models=["jev-1.14.0", "jev-1.13.0"],
                                           planner_model="gemini-3.1-flash-lite",
                                           driver={"name": "cua", "socket": "/tmp/lg-cu/cua.sock"}))
    out = ctx.tools["loopguard_cu_status"]["handler"]({})
    assert "enabled" in out and "jev-1.14.0" in out


# --------------------------------------------------------------------- hooks

GATE_OK = SafetyResult(category="none", probs={"none": 0.99}, confirm_dialog=0.0, pause=False,
                       usage=VisionUsage(backend="typesafe", model="jev-1.14.0"))


def test_pre_tool_call_blocks_mutating_action_on_safety_pause(monkeypatch):
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    hook = ctx.hooks["pre_tool_call"][0]

    driver = make_driver()
    gate = FakeGate()
    gate.safety_result = SafetyResult(
        category="payment", probs={"payment": 0.98, "none": 0.0}, confirm_dialog=0.9,
        pause=True, usage=VisionUsage(backend="typesafe", model="jev-1.14.0"),
    )
    monkeypatch.setattr(hermes_tool, "_build_driver", lambda cu, app=None: driver)
    monkeypatch.setattr(hermes_tool, "_build_gate", lambda cu: gate)

    verdict = hook(tool_name="computer_use", args={"action": "click", "x": 1, "y": 2})
    assert isinstance(verdict, dict) and verdict["action"] == "block"
    assert "payment" in verdict["reason"]
    # Read-only actions and other tools are never gated.
    assert hook(tool_name="computer_use", args={"action": "capture"}) is None
    assert hook(tool_name="terminal", args={"action": "click"}) is None


def test_pre_tool_call_shadow_logs_only(monkeypatch):
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True, shadow=True))
    hook = ctx.hooks["pre_tool_call"][0]
    monkeypatch.setattr(hermes_tool, "_build_driver", lambda cu, app=None: make_driver())
    gate = FakeGate()
    gate.safety_result = SafetyResult(category="payment", probs={"payment": 0.98},
                                      confirm_dialog=0.9, pause=True, usage=None)
    monkeypatch.setattr(hermes_tool, "_build_gate", lambda cu: gate)
    assert hook(tool_name="computer_use", args={"action": "click"}) is None


def test_transform_tool_result_appends_verified(monkeypatch):
    ctx = FakeCtx()
    hermes_tool.register_cu(ctx, lambda: cfg(True))
    hook = ctx.hooks["transform_tool_result"][0]
    monkeypatch.setattr(hermes_tool, "_build_driver", lambda cu, app=None: make_driver())

    gate = FakeGate()
    monkeypatch.setattr(hermes_tool, "_build_gate", lambda cu: gate)
    out = hook(tool_name="computer_use", args={"action": "type", "text": "Ada"},
               result='{"output": "typed"}')
    assert out.endswith("[loopguard VERIFIED]")

    gate.verify_result = VerifyResult(passed=False, yes=0.1, usage=VisionUsage(model="jev-1.14.0"))
    out = hook(tool_name="computer_use", args={"action": "type", "text": "Ada"},
               result='{"output": "typed"}')
    assert "NOT VERIFIED" in out
    # No expectation derivable -> untouched.
    assert hook(tool_name="computer_use", args={"action": "click"}, result="ok") is None
    assert hook(tool_name="other_tool", args={"action": "type", "text": "x"}, result="ok") is None


# --------------------------------------------------------------------- persistence


def make_driver():
    fixture = {
        "frames": [{"file": "form.png", "width": 1280, "height": 800}],
        "elements": {"Google-chrome": []},
        "actions": {},
    }
    return FakeDriver(fixture, base_dir=FIX)


def test_store_migration_creates_cu_tables(store):
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'cu_%'"
    ).fetchall()
    assert {r["name"] for r in rows} == {"cu_runs", "cu_steps"}


def test_run_logger_roundtrip(store, tmp_path):
    logger = CuRunLogger(store, base_dir=tmp_path / "frames", retention=5)
    run_id = logger.start_run("bench", "fill the contact form")
    assert run_id is not None
    frame = Frame(png=(FIX / "form.png").read_bytes(), width=1280, height=800, ts=0.0)
    path = logger.save_frame(run_id, 0, "before", frame)
    assert path and Path(path).exists()
    assert Path(path).with_suffix(".jpg").exists(), "JPEG thumbnail saved"

    rec = agent.StepRecord(idx=0, action="click", target="Submit", grounding="a11y",
                           point=(100, 200), verdict="pass", noul=0.9, effect="unverifiable",
                           frame_before=path)
    rec.usages.append(VisionUsage(backend="typesafe", model="jev-1.14.0",
                                  input_tokens=8488, output_tokens=75, usd=0.000356))
    logger.record_step(run_id, rec)
    logger.finish_run(run_id, agent.RunResult(
        state="completed", steps=[rec], calls_per_model={"jev-1.14.0": 1},
        input_tokens=8488, output_tokens=75, usd=0.000356, wall_s=5.0,
        big_model_calls=0, big_model_avoided=1, done_check=0.9,
    ))

    run_row = store.conn.execute("SELECT * FROM cu_runs WHERE id = ?", (run_id,)).fetchone()
    assert run_row["state"] == "completed" and run_row["big_model_avoided"] == 1
    step_row = store.conn.execute("SELECT * FROM cu_steps WHERE run_id = ?", (run_id,)).fetchone()
    assert step_row["action"] == "click" and step_row["model"] == "jev-1.14.0"
    assert json.loads(step_row["point"]) == [100, 200]
    assert logger.recent_runs(5)[0]["id"] == run_id


def test_run_logger_never_raises(store, tmp_path):
    logger = CuRunLogger(None, base_dir=tmp_path / "f")
    run_id = logger.start_run("t", "i")
    logger.record_step(run_id, None)  # garbage in, no crash out
    logger.finish_run(run_id, None)
    assert logger.save_frame(run_id, 0, "before", None) is None
