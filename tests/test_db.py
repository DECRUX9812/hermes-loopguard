from __future__ import annotations

import time
from pathlib import Path
import pytest

from hermes_loopguard.db import Store


def test_items_crud_and_attempts(store: Store):
    assert store.get_item("w1", "item-1") is None

    store.upsert_item("w1", "item-1", fingerprint="fp-111", title="Title 1")
    item = store.get_item("w1", "item-1")
    assert item is not None
    assert item["fingerprint"] == "fp-111"
    assert item["title"] == "Title 1"
    assert item["state"] == "new"
    assert item["attempts"] == 0

    store.set_item_state("w1", "item-1", "claimed", route="agent")
    item = store.get_item("w1", "item-1")
    assert item["state"] == "claimed"
    assert item["route"] == "agent"

    att = store.bump_attempt("w1", "item-1", "fp-111")
    assert att == 1
    item = store.get_item("w1", "item-1")
    assert item["attempts"] == 1
    assert item["last_attempt_fp"] == "fp-111"

    store.add_item_spend("w1", "item-1", tokens=500, usd=0.0015)
    item = store.get_item("w1", "item-1")
    assert item["spend_tokens"] == 500
    assert pytest.approx(item["spend_usd"]) == 0.0015


def test_claims_exclusive_and_expiry(store: Store):
    now = time.time()
    # Initial claim succeeds
    assert store.try_claim("w1", "item-1", "fp-1", "owner-A", ttl_s=10) is True
    assert store.claim_live("w1", "item-1", now=now) is True

    # Concurrent claim by another owner fails
    assert store.try_claim("w1", "item-1", "fp-1", "owner-B", ttl_s=10) is False

    # Owner-A claims list
    claims = store.claims_for_owner("owner-A")
    assert len(claims) == 1
    assert claims[0]["item_id"] == "item-1"

    # Expired claim can be retaken
    future = now + 20
    assert store.claim_live("w1", "item-1", now=future) is False
    # Mock time passing by passing an expired condition or release
    # In try_claim, time.time() is used; let's test release_claim
    store.release_claim("w1", "item-1")
    assert store.claim_live("w1", "item-1") is False
    assert store.try_claim("w1", "item-1", "fp-2", "owner-B", ttl_s=10) is True


def test_claim_expiry_retake(tmp_path: Path):
    db_file = tmp_path / "expiry.db"
    s = Store(db_file)
    # Claim with very short ttl
    assert s.try_claim("w1", "item-1", "fp-1", "owner-A", ttl_s=0.05) is True
    time.sleep(0.08)
    assert s.claim_live("w1", "item-1") is False
    assert s.try_claim("w1", "item-1", "fp-1", "owner-B", ttl_s=10) is True
    s.close()


def test_two_store_objects_on_one_file_see_each_other(tmp_path: Path):
    db_file = tmp_path / "shared.db"
    store1 = Store(db_file)
    store2 = Store(db_file)

    store1.upsert_item("w1", "item-99", fingerprint="fp-99", title="Shared Item")
    item_from_store2 = store2.get_item("w1", "item-99")
    assert item_from_store2 is not None
    assert item_from_store2["title"] == "Shared Item"

    assert store1.try_claim("w1", "item-99", "fp-99", "owner-1", ttl_s=3600) is True
    assert store2.try_claim("w1", "item-99", "fp-99", "owner-2", ttl_s=3600) is False
    assert store2.claim_live("w1", "item-99") is True

    store1.close()
    store2.close()


def test_decisions_and_polls(store: Store):
    d_id = store.log_decision(
        watcher="w1",
        kind="semantic",
        item_id="i-1",
        route="agent",
        proposed_route="agent",
        answers={"choice": "bug"},
        outcome="admitted",
        tokens_in=100,
        tokens_out=20,
        cost_usd=0.0003,
    )
    assert d_id > 0
    decs = store.decisions(watcher="w1")
    assert len(decs) == 1
    assert decs[0]["kind"] == "semantic"
    assert decs[0]["answers"] == {"choice": "bug"}

    p_id = store.log_poll(
        watcher="w1",
        n_items=5,
        n_changed=2,
        n_admitted=1,
        woke=True,
        shadow=False,
        gate_tokens=120,
        gate_usd=0.0003,
    )
    assert p_id > 0
    polls = store.polls(watcher="w1")
    assert len(polls) == 1
    assert polls[0]["woke"] is True
    assert polls[0]["n_admitted"] == 1


def test_runs_and_review_queue(store: Store):
    run_id = store.open_run(watcher="w1", owner="cron:w1:123", items=["i-1", "i-2"])
    assert run_id > 0
    store.add_run_usage(run_id, 500, 100, 0.002)
    open_run = store.find_open_run(watcher="w1")
    assert open_run is not None
    assert open_run["items"] == ["i-1", "i-2"]
    assert open_run["tokens_in"] == 500

    store.close_run(run_id, completed=True, outcome="done")
    assert store.find_open_run(watcher="w1") is None
    recent = store.recent_run_tokens("w1")
    assert recent == [600]

    rev_id = store.enqueue_review("w1", "i-3", title="Check me", reason="low prob", payload={"foo": "bar"})
    q = store.review_queue("open")
    assert len(q) == 1
    assert q[0]["item_id"] == "i-3"
    assert q[0]["payload"] == {"foo": "bar"}
    store.resolve_review(rev_id, "approved")
    assert len(store.review_queue("open")) == 0


def test_counters_and_kv(store: Store):
    assert store.get_counter("cnt") == 0
    assert store.bump_counter("cnt") == 1
    assert store.bump_counter("cnt") == 2
    assert store.get_counter("cnt") == 2
    store.reset_counter("cnt")
    assert store.get_counter("cnt") == 0

    assert store.get_kv("conf") is None
    store.set_kv("conf", {"active": True, "limit": 10})
    assert store.get_kv("conf") == {"active": True, "limit": 10}
