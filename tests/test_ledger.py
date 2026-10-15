from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hermes_loopguard.config import load_watcher
from hermes_loopguard.db import Store
from hermes_loopguard.ledger import BUDGET_MESSAGE, Ledger, cost_from_usage, tokens_from_usage
from hermes_loopguard.router import poll


class FakeExecution:
    # FAKE CRON EXECUTION (unit test only)
    def __init__(self, job_id="job-1", execution_id="exec-1", job_name="lg-issues"):
        self.job_id = job_id
        self.execution_id = execution_id
        self.job_name = job_name


def _install_watcher(name="issues", items=None, **extra):
    home = Path(os.environ["HERMES_HOME"])
    watchers = home / "loopguard" / "watchers"
    watchers.mkdir(parents=True, exist_ok=True)
    source = home / f"{name}_items.json"
    source.write_text(json.dumps(items or []), encoding="utf-8")
    spec = {"name": name, "source": {"file": str(source)}}
    spec.update(extra)
    (watchers / f"{name}.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return spec


def _ledger(store, cfg=None, execution=None):
    return Ledger(lambda: store, lambda: cfg or {}, cron_execution=lambda: execution)


def test_tokens_and_cost_from_usage_spellings():
    assert tokens_from_usage({"input_tokens": 3, "output_tokens": 4}) == (3, 4)
    assert tokens_from_usage({"prompt_tokens": 5, "completion_tokens": 6}) == (5, 6)
    assert tokens_from_usage({"input_tokens": None, "output_tokens": None}) == (0, 0)
    assert tokens_from_usage({}) == (0, 0)
    assert tokens_from_usage(None) == (0, 0)
    assert cost_from_usage({"cost_usd": 0.5}) == 0.5
    assert cost_from_usage({}) is None


def test_ledger_cron_flow(tmp_path: Path):
    _install_watcher(items=[{"id": "1", "title": "A"}, {"id": "2", "title": "B"}])
    store = Store(tmp_path / "ledger.db")
    result = poll(load_watcher("issues"), store=store, cfg={}, owner="cron:issues:1")
    assert result.wake is True

    run = store.find_open_run(watcher="issues")
    assert run is not None
    assert run["items"] == ["1", "2"]
    assert run["job_id"] is None

    execution = FakeExecution()
    ledger = _ledger(store, {"pricing": {"usd_per_mtok": 3.0}}, execution)

    ledger.post_api_request(usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01})

    run = store.find_open_run(watcher="issues")
    assert run["job_id"] == "job-1"  # bound to the cron job
    assert run["tokens_in"] == 100
    assert run["tokens_out"] == 50
    assert run["usd"] == pytest.approx(0.01)
    # 150 tokens split evenly over the two items in the run
    assert store.get_item("issues", "1")["spend_tokens"] == 75
    assert store.get_item("issues", "2")["spend_tokens"] == 75

    ledger.post_llm_call(
        assistant_response="Working on it.\nDONE 1\nBLOCKED 2: needs more information\n"
    )
    assert store.get_item("issues", "1")["state"] == "completed"
    assert store.get_item("issues", "2")["state"] == "blocked"
    assert store.claim_live("issues", "1") is False
    assert store.claim_live("issues", "2") is False
    outcomes = [d["outcome"] for d in store.decisions(watcher="issues", kind="route")]
    assert "done" in outcomes
    assert "blocked" in outcomes

    # ids outside the run's items are ignored
    ledger.post_llm_call(assistant_response="DONE 99")
    assert store.get_item("issues", "99") is None

    ledger.on_session_end(completed=True)
    assert store.find_open_run(watcher="issues") is None
    closed = store.runs(watcher="issues")[0]
    assert closed["completed"] is True
    assert closed["outcome"] == "completed"
    store.close()


def test_on_session_end_marks_claimed_items_failed(tmp_path: Path):
    _install_watcher(items=[{"id": "1", "title": "A"}])
    store = Store(tmp_path / "ledger_failed.db")
    poll(load_watcher("issues"), store=store, cfg={}, owner="cron:issues:2")

    ledger = _ledger(store, {}, FakeExecution(job_id="job-9", execution_id="exec-9"))
    ledger.on_session_end(completed=False, interrupted=True)

    assert store.get_item("issues", "1")["state"] == "failed"
    assert store.claim_live("issues", "1") is False
    closed = store.runs(watcher="issues")[0]
    assert closed["completed"] is False
    assert closed["outcome"] == "interrupted"
    store.close()


def test_on_session_end_marks_budget_exhausted(tmp_path: Path):
    _install_watcher(items=[{"id": "1", "title": "A"}], spend_cap_tokens=100)
    store = Store(tmp_path / "ledger_budget.db")
    poll(load_watcher("issues"), store=store, cfg={}, owner="cron:issues:4")

    ledger = _ledger(store, {}, FakeExecution(job_id="job-4", execution_id="exec-4"))
    ledger.post_api_request(usage={"input_tokens": 500, "output_tokens": 0})
    ledger.on_session_end(completed=True)

    assert store.get_item("issues", "1")["state"] == "budget_exhausted"
    kinds = [d["kind"] for d in store.decisions(watcher="issues")]
    assert "budget" in kinds
    store.close()


def test_budget_guard_blocks_after_cap(tmp_path: Path):
    _install_watcher(items=[{"id": "1", "title": "A"}], spend_cap_tokens=100)
    store = Store(tmp_path / "ledger_guard.db")
    poll(load_watcher("issues"), store=store, cfg={}, owner="cron:issues:3")

    ledger = _ledger(store, {}, FakeExecution(job_id="job-3", execution_id="exec-3"))
    assert ledger.budget_guard(tool_name="terminal", args={"command": "ls"}) is None

    ledger.post_api_request(usage={"input_tokens": 200, "output_tokens": 0})

    assert ledger.budget_guard(tool_name="terminal", args={"command": "ls"}) == {
        "action": "block",
        "message": BUDGET_MESSAGE,
    }
    # the reporting tools always stay available
    assert ledger.budget_guard(tool_name="kanban_block") is None
    assert ledger.budget_guard(tool_name="kanban_comment") is None
    assert ledger.budget_guard(tool_name="kanban_show") is None
    store.close()


def test_budget_guard_kanban_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = Store(tmp_path / "ledger_kanban.db")
    store.upsert_item("issues", "7", fingerprint="fp-7", title="A")
    store.add_item_spend("issues", "7", 500, 0.0)
    store.set_kv("kanban_task:t_abc", {"watcher": "issues", "item_id": "7", "fingerprint": "fp-7"})
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")

    ledger = _ledger(store, {"kanban": {"spend_cap_tokens": 400}})
    assert ledger.budget_guard(tool_name="terminal") == {"action": "block", "message": BUDGET_MESSAGE}
    assert ledger.budget_guard(tool_name="kanban_block") is None

    store.set_item_state("issues", "7", "budget_exhausted")
    assert ledger.budget_guard(tool_name="terminal") is not None
    store.close()


def test_kanban_observers_update_the_mapped_item(tmp_path: Path):
    store = Store(tmp_path / "ledger_observers.db")
    store.upsert_item("issues", "9", fingerprint="fp-9", title="A")
    store.try_claim("issues", "9", "fp-9", "kanban:t_x")
    store.set_kv("kanban_task:t_x", {"watcher": "issues", "item_id": "9", "fingerprint": "fp-9"})

    ledger = _ledger(store)
    ledger.kanban_task_claimed(task_id="t_x")
    ledger.kanban_task_completed(task_id="t_x", summary="fixed")

    assert store.get_item("issues", "9")["state"] == "completed"
    assert store.claim_live("issues", "9") is False
    assert [d["outcome"] for d in store.decisions(kind="kanban")] == ["completed", "claimed"]

    ledger.kanban_task_blocked(task_id="t_x", reason="no repro")
    assert store.get_item("issues", "9")["state"] == "blocked"

    ledger.on_kanban_worker_exited(task_id="t_x", exit_kind="clean_exit")
    assert store.decisions(kind="kanban")[0]["outcome"] == "worker_exited"
    store.close()


def test_ledger_without_context_is_a_noop(tmp_path: Path):
    store = Store(tmp_path / "ledger_noop.db")
    ledger = _ledger(store)
    ledger.post_api_request(usage={"prompt_tokens": 10, "completion_tokens": 5})
    ledger.post_llm_call(assistant_response="DONE 1")
    ledger.on_session_end(completed=True)
    assert store.runs() == []
    assert store.decisions() == []
    store.close()
