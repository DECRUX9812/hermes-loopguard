from __future__ import annotations

import io
import json
from pathlib import Path
import pytest
import yaml

from hermes_loopguard.backends.base import GateResult, Usage
from hermes_loopguard.change_gate import classify
from hermes_loopguard.config import WatcherSpec
from hermes_loopguard.cron import WAKE_FALSE, WAKE_TRUE, run_watcher
from hermes_loopguard.db import Store
from hermes_loopguard.questions import ChoiceAnswer
from hermes_loopguard.router import poll


class FakeAcceptanceBackend:
    # FAKE BACKEND (unit test only)
    name = "fake_acceptance"

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def ask(self, state, questions):
        self.calls += 1
        title = state.get("title", "") if isinstance(state, dict) else str(state)
        if "Critical Bug" in title:
            kind_ans = ChoiceAnswer(choice="bug", confidence=0.9, probabilities={"bug": 0.9, "spam": 0.1})
        else:
            kind_ans = ChoiceAnswer(choice="spam", confidence=0.9, probabilities={"bug": 0.1, "spam": 0.9})
        return GateResult(
            answers={"kind": kind_ans},
            backend=self.name,
            model="fake-v1",
            usage=Usage(input_tokens=20, output_tokens=10, cost_usd=0.0001),
        )


def test_router_cron_full_lifecycle(tmp_path: Path, monkeypatch):
    # Setup files and paths
    hermes_home = tmp_path / "hermes_home"
    watchers_dir = hermes_home / "loopguard" / "watchers"
    watchers_dir.mkdir(parents=True, exist_ok=True)
    source_file = tmp_path / "items.json"

    initial_items = [
        {"id": "1", "title": "Critical Bug in login", "body": "Cannot login"},
        {"id": "2", "title": "Spam issue", "body": "Buy cheap watches"},
    ]
    source_file.write_text(json.dumps(initial_items), encoding="utf-8")

    watcher_def = {
        "name": "issues",
        "source": {"file": str(source_file)},
        "id_field": "id",
        "title_field": "title",
        "gate": {
            "questions": {
                "kind": {
                    "type": "choice",
                    "instructions": "Classify the issue",
                    "criteria": {"bug": "A real bug", "spam": "Spam or noise"},
                }
            },
            "route_question": "kind",
            "routes": {
                "bug": {"handler": "agent", "min_prob": 0.6, "brief": "Fix the bug"},
                "spam": {"handler": "drop", "min_prob": 0.6},
            },
            "default_route": "review",
        },
    }

    watcher_file = watchers_dir / "issues.yaml"
    watcher_file.write_text(yaml.safe_dump(watcher_def), encoding="utf-8")

    fake_backend = FakeAcceptanceBackend()
    chain_factory_calls = 0

    def test_chain_factory():
        nonlocal chain_factory_calls
        chain_factory_calls += 1
        return [fake_backend]

    store = Store(tmp_path / "test.db")
    cfg = {"backend": "auto"}

    # --- POLL 1 ---
    out1 = io.StringIO()
    err1 = io.StringIO()
    ret1 = run_watcher(
        "issues",
        cfg=cfg,
        store=store,
        chain_factory=test_chain_factory,
        out=out1,
        err=err1,
    )
    assert ret1 == 0
    stdout_lines1 = [line.strip() for line in out1.getvalue().splitlines() if line.strip()]
    assert stdout_lines1[-1] == WAKE_TRUE
    brief1 = out1.getvalue()
    assert "1: Critical Bug in login" in brief1
    assert "2: Spam issue" not in brief1
    assert fake_backend.calls == 2
    assert chain_factory_calls == 1

    # --- POLL 2 (identical file) ---
    out2 = io.StringIO()
    err2 = io.StringIO()
    ret2 = run_watcher(
        "issues",
        cfg=cfg,
        store=store,
        chain_factory=test_chain_factory,
        out=out2,
        err=err2,
    )
    assert ret2 == 0
    stdout_lines2 = [line.strip() for line in out2.getvalue().splitlines() if line.strip()]
    assert stdout_lines2[-1] == WAKE_FALSE
    # Assert zero model calls on poll 2 AND chain_factory was never called on poll 2
    assert fake_backend.calls == 2
    assert chain_factory_calls == 1

    # --- EDIT ITEM 2 ---
    # Edit item 2: only item 2 is re-gated
    updated_items = [
        {"id": "1", "title": "Critical Bug in login", "body": "Cannot login"},
        {"id": "2", "title": "Spam issue updated", "body": "Buy cheap watches v2"},
    ]
    source_file.write_text(json.dumps(updated_items), encoding="utf-8")

    out3 = io.StringIO()
    err3 = io.StringIO()
    ret3 = run_watcher(
        "issues",
        cfg=cfg,
        store=store,
        chain_factory=test_chain_factory,
        out=out3,
        err=err3,
    )
    assert ret3 == 0
    stdout_lines3 = [line.strip() for line in out3.getvalue().splitlines() if line.strip()]
    # Since item 2 routes to "drop" and item 1 was unchanged (skipped), no items admitted -> WAKE_FALSE
    assert stdout_lines3[-1] == WAKE_FALSE
    # Fake backend called exactly once for the edited item 2!
    assert fake_backend.calls == 3
    assert chain_factory_calls == 2


def test_shadow_mode_always_wakes_no_claims(tmp_path: Path):
    source_file = tmp_path / "shadow_items.json"
    source_file.write_text(json.dumps([{"id": "s1", "title": "Critical Bug"}]), encoding="utf-8")

    spec = WatcherSpec(
        name="shadow_watch",
        source={"file": str(source_file)},
        shadow=True,
    )
    store = Store(tmp_path / "shadow.db")
    cfg = {"shadow": False}

    res = poll(spec, store=store, cfg=cfg, owner="test-owner")
    assert res.wake is True
    assert res.brief == ""
    assert res.shadow is True
    assert store.claim_live("shadow_watch", "s1") is False


def test_claimed_item_not_admitted_twice(tmp_path: Path):
    source_file = tmp_path / "claim_items.json"
    source_file.write_text(json.dumps([{"id": "c1", "title": "Bug"}]), encoding="utf-8")

    spec = WatcherSpec(
        name="claim_watch",
        source={"file": str(source_file)},
    )
    store = Store(tmp_path / "concurrent.db")
    cfg = {}

    # Owner 1 polls -> claims and admits item
    res1 = poll(spec, store=store, cfg=cfg, owner="owner-1")
    assert res1.wake is True
    assert len(res1.routed) == 1
    assert res1.routed[0].outcome == "admitted"

    # Owner 2 polls while claim is live -> item is not admitted
    res2 = poll(spec, store=store, cfg=cfg, owner="owner-2")
    assert res2.wake is False
    assert len(res2.routed) == 0  # item status is 'unchanged' in change_gate since fp same


def test_skip_summary_row_only_logged_when_non_actionable(tmp_path: Path):
    source_file = tmp_path / "skip_rows_items.json"
    source_file.write_text(json.dumps([{"id": "1", "title": "Bug A"}]), encoding="utf-8")
    spec = WatcherSpec(name="skip_rows", source={"file": str(source_file)})
    store = Store(tmp_path / "skip_rows.db")

    def skip_rows():
        rows = store.decisions(watcher="skip_rows", limit=100)
        return [r for r in rows if r.get("kind") == "change_gate" and r.get("outcome") == "skip"]

    # Poll 1: the new item is admitted -> no change_gate/skip row at all.
    res1 = poll(spec, store=store, cfg={}, owner="o1")
    assert res1.wake is True
    assert skip_rows() == []

    # Poll 2: the item is unchanged -> exactly one summary row with counts.
    res2 = poll(spec, store=store, cfg={}, owner="o2")
    assert res2.wake is False
    rows = skip_rows()
    assert len(rows) == 1
    assert rows[0].get("note") == "1 unchanged, 0 other"
