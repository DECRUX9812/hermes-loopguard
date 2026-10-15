from __future__ import annotations

import yaml
import pytest

from hermes_loopguard import paths
from hermes_loopguard.db import Store
from hermes_loopguard.ledger import BUDGET_MESSAGE
from hermes_loopguard.plugin import register


class FakeExecution:
    # FAKE CRON EXECUTION (unit test only)
    job_id = "job-x"
    execution_id = "exec-x"
    job_name = "lg-nope"


class FakeLlm:
    def complete_structured(self, **kwargs):
        raise AssertionError("the plugin must not call the LLM at registration time")


class FakeCtx:
    # FAKE CONTEXT (unit test only)
    def __init__(self, execution=None):
        self.cli_commands: list[dict] = []
        self.aux_tasks: list[tuple] = []
        self.commands: list[tuple] = []
        self.hooks: dict[str, list] = {}
        self.llm = FakeLlm()
        self._execution = execution

    def register_cli_command(self, **kwargs):
        self.cli_commands.append(kwargs)

    def register_auxiliary_task(self, key, **kwargs):
        self.aux_tasks.append((key, kwargs))

    def register_command(self, name, handler, description=""):
        self.commands.append((name, handler, description))

    def register_hook(self, name, handler):
        self.hooks.setdefault(name, []).append(handler)

    def current_cron_execution(self):
        return self._execution


def _write_config(tool_gate_enabled: bool = True) -> None:
    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "shadow": False,
                "tool_gate": {
                    "enabled": tool_gate_enabled,
                    "scope": "automations",
                    "network": "approve",
                    "destructive": "block",
                    "unclear": "approve",
                },
                "kanban": {"spend_cap_tokens": 400},
            }
        ),
        encoding="utf-8",
    )


def test_register_wires_commands_and_hooks(tmp_path):
    _write_config()
    ctx = FakeCtx()

    register(ctx)

    assert [c["name"] for c in ctx.cli_commands] == ["loopguard"]
    assert ctx.cli_commands[0]["setup_fn"].__name__ == "setup_parser"
    assert ctx.cli_commands[0]["handler_fn"].__name__ == "handle"
    assert ctx.cli_commands[0]["help"] == "Stop automations from burning tokens"

    assert [key for key, _ in ctx.aux_tasks] == ["loopguard_gate"]
    assert ctx.aux_tasks[0][1]["display_name"] == "LoopGuard gate"

    assert ctx.commands and ctx.commands[0][0] == "loopguard"
    assert ctx.commands[0][2] == "LoopGuard savings"

    for name in (
        "post_api_request",
        "post_llm_call",
        "on_session_end",
        "kanban_task_claimed",
        "kanban_task_completed",
        "kanban_task_blocked",
        "on_kanban_worker_exited",
        "pre_tool_call",
        "pre_verify",
    ):
        assert name in ctx.hooks, name


def test_register_survives_a_broken_config(tmp_path):
    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("shadow: [unclosed\n", encoding="utf-8")

    ctx = FakeCtx()
    register(ctx)  # must not raise

    assert [c["name"] for c in ctx.cli_commands] == ["loopguard"]


def test_slash_command_returns_the_stats_headline(tmp_path):
    _write_config()
    ctx = FakeCtx()
    register(ctx)
    _, handler, _ = ctx.commands[0]

    store = Store.open_default()
    store.log_poll(
        watcher="issues", n_items=2, n_changed=0, n_admitted=0, woke=False, shadow=False,
        est_tokens_avoided=25000, est_usd_avoided=0.075,
    )
    store.close()

    text = handler("")
    assert text.splitlines()[0].startswith("LoopGuard: 1 runs skipped")
    assert "issues" in text


def test_pre_tool_call_block_beats_approve(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _write_config()
    ctx = FakeCtx(execution=FakeExecution())
    register(ctx)
    hook = ctx.hooks["pre_tool_call"][0]

    store = Store.open_default()
    store.upsert_item("issues", "7", fingerprint="fp-7", title="A")
    store.set_item_state("issues", "7", "budget_exhausted")
    store.set_kv("kanban_task:t_abc", {"watcher": "issues", "item_id": "7", "fingerprint": "fp-7"})
    store.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    # budget_guard blocks, the tool gate would approve the same network command: block wins
    assert hook(tool_name="terminal", args={"command": "curl http://example.com"}) == {
        "action": "block",
        "message": BUDGET_MESSAGE,
    }

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    verdict = hook(tool_name="terminal", args={"command": "curl http://example.com"})
    assert verdict is not None
    assert verdict["action"] == "approve"


def test_pre_tool_call_is_silent_when_disabled(tmp_path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tool_gate_enabled=False)
    ctx = FakeCtx(execution=FakeExecution())
    register(ctx)
    hook = ctx.hooks["pre_tool_call"][0]

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing")
    assert hook(tool_name="terminal", args={"command": "rm -rf /"}) is None
