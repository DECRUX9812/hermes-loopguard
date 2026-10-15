from __future__ import annotations

import time

import pytest

from hermes_loopguard.db import Store
from hermes_loopguard.stats import compute_stats, format_stats, parse_since


def test_parse_since():
    now = time.time()
    assert parse_since(None) is None
    assert parse_since("all") is None
    assert parse_since("nonsense") is None
    assert parse_since("24h") == pytest.approx(now - 86400, abs=5)
    assert parse_since("7d") == pytest.approx(now - 7 * 86400, abs=5)
    assert parse_since("30m") == pytest.approx(now - 1800, abs=5)


def _seed(store: Store) -> None:
    for _ in range(3):
        store.log_poll(
            watcher="w1", n_items=5, n_changed=0, n_admitted=0, woke=False, shadow=False,
            est_tokens_avoided=25000, est_usd_avoided=0.08, gate_tokens=100, gate_usd=0.0002,
        )
    store.log_poll(
        watcher="w1", n_items=5, n_changed=1, n_admitted=1, woke=True, shadow=False,
        gate_tokens=50, gate_usd=0.0001,
    )
    store.log_poll(watcher="w1", n_items=2, n_changed=0, n_admitted=0, woke=True, shadow=True)

    run_id = store.open_run(watcher="w1", owner="owner", items=["i1"])
    store.add_run_usage(run_id, 1000, 200, 0.02)
    store.close_run(run_id, completed=True, outcome="completed")

    store.upsert_item("w1", "i1", fingerprint="fp1", title="T1")
    store.set_item_state("w1", "i1", "completed")
    store.upsert_item("w1", "i2", fingerprint="fp2", title="T2")
    store.set_item_state("w1", "i2", "blocked")

    store.log_decision(watcher="w1", kind="semantic", item_id="i1", route="agent")
    store.log_decision(watcher="w1", kind="route", item_id="i1", route="agent", outcome="admitted")
    store.log_decision(watcher="w1", kind="route", item_id="i2", route="drop", outcome="dropped")
    store.log_decision(watcher="w1", kind="change_gate", outcome="error", note="boom")


def test_compute_stats_numbers(store: Store):
    _seed(store)
    cfg = {"pricing": {"usd_per_mtok": 3.0, "est_tokens_per_run": 25000}}

    stats = compute_stats(store, cfg)

    assert stats["polls"] == 5
    assert stats["skipped_runs"] == 3
    assert stats["woken_runs"] == 1
    assert stats["skip_rate"] == pytest.approx(0.75)
    assert stats["shadow_polls"] == 1
    assert stats["shadow_would_skip"] == 1
    assert stats["items_gated"] == 1
    assert stats["routes"] == {"agent": 1, "drop": 1}
    assert stats["tokens_avoided"] == 75000
    assert stats["usd_avoided"] == pytest.approx(0.24)
    assert stats["gate_tokens"] == 350
    assert stats["gate_usd"] == pytest.approx(0.0007)
    assert stats["agent_tokens"] == 1200
    assert stats["agent_usd"] == pytest.approx(0.02)
    assert stats["net_usd_saved"] == pytest.approx(0.24 - 0.0007)
    assert stats["completed_items"] == 1
    assert stats["cost_per_completed"] == pytest.approx(0.0207)
    assert stats["errors"] == 1
    assert stats["review_open"] == 0
    assert stats["generated_at"] > 0

    assert stats["per_watcher"]["w1"]["skipped_runs"] == 3
    assert stats["per_watcher"]["w1"]["tokens_avoided"] == 75000

    assert len(stats["timeline"]) == 14
    today = stats["timeline"][-1]
    assert today["skipped"] == 3
    assert today["woken"] == 1
    assert today["tokens_avoided"] == 75000

    assert stats["recent"][0]["kind"] == "change_gate"
    assert len(stats["recent"]) == 4


def test_format_stats_headline_and_table(store: Store):
    _seed(store)
    report = format_stats(compute_stats(store, {"pricing": {"usd_per_mtok": 3.0}}))
    headline = report.splitlines()[0]
    assert headline.startswith("LoopGuard: 3 runs skipped (75%)")
    assert "~75.0k tokens / $0.24 avoided" in headline
    assert "gate cost $0.00" in headline
    assert "$0.02 per completed task" in headline
    assert "Watcher" in report
    assert "w1" in report
    assert "Routes: agent 1, drop 1" in report


def test_format_stats_without_completed_items(store: Store):
    store.log_poll(
        watcher="w1", n_items=1, n_changed=0, n_admitted=0, woke=False, shadow=False,
        est_tokens_avoided=1000, est_usd_avoided=0.003,
    )
    report = format_stats(compute_stats(store, {}))
    assert "n/a per completed task" in report


def test_compute_stats_since_window(store: Store):
    store.log_poll(
        watcher="w1", n_items=1, n_changed=0, n_admitted=0, woke=False, shadow=False,
        est_tokens_avoided=1000, est_usd_avoided=0.003,
    )
    cur = store.conn.cursor()
    cur.execute("UPDATE polls SET created_at = ?", (time.time() - 10 * 86400,))

    assert compute_stats(store, {}, since="24h")["polls"] == 0
    assert compute_stats(store, {}, since="30d")["polls"] == 1
    assert compute_stats(store, {})["polls"] == 1
