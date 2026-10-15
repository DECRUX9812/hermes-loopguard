from __future__ import annotations

import pytest

from hermes_loopguard.change_gate import ACTIONABLE, ItemDelta, classify
from hermes_loopguard.config import WatcherSpec
from hermes_loopguard.db import Store


def test_classify_prior_none_new(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"})
    items = [{"id": "1", "title": "First"}]
    deltas = classify(store, spec, items)
    assert len(deltas) == 1
    assert deltas[0].status == "new"
    assert deltas[0].prior is None


def test_classify_unchanged_and_changed(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"}, fingerprint_fields=["title"])
    item1 = {"id": "1", "title": "Hello"}
    store.upsert_item("w1", "1", fingerprint="fp-hello", title="Hello")

    # When fingerprint matches
    from hermes_loopguard.fingerprint import fingerprint
    fp = fingerprint(item1, ["title"])
    store.upsert_item("w1", "1", fingerprint=fp, title="Hello")

    deltas = classify(store, spec, [item1])
    assert len(deltas) == 1
    assert deltas[0].status == "unchanged"

    # When fingerprint differs
    item_changed = {"id": "1", "title": "Hello World"}
    deltas2 = classify(store, spec, [item_changed])
    assert len(deltas2) == 1
    assert deltas2[0].status == "changed"


def test_classify_budget_exhausted(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"}, spend_cap_tokens=1000)
    store.upsert_item("w1", "1", fingerprint="fp-old", title="T")
    store.add_item_spend("w1", "1", tokens=1200, usd=0.01)

    deltas = classify(store, spec, [{"id": "1", "title": "T2"}])
    assert len(deltas) == 1
    assert deltas[0].status == "budget_exhausted"


def test_classify_attempts_exhausted(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"}, max_attempts=2)
    store.upsert_item("w1", "1", fingerprint="fp-old", title="T")
    store.set_item_state("w1", "1", "blocked")
    store.bump_attempt("w1", "1", "fp-old")
    store.bump_attempt("w1", "1", "fp-old")

    deltas = classify(store, spec, [{"id": "1", "title": "T_different"}])
    assert len(deltas) == 1
    assert deltas[0].status == "attempts_exhausted"


def test_classify_claimed(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"})
    store.upsert_item("w1", "1", fingerprint="fp-old", title="T")
    store.try_claim("w1", "1", "fp-old", "owner-X", ttl_s=3600)

    deltas = classify(store, spec, [{"id": "1", "title": "T_different"}])
    assert len(deltas) == 1
    assert deltas[0].status == "claimed"


def test_classify_duplicate_ids_kept_first(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"})
    items = [
        {"id": "dup", "title": "First"},
        {"id": "dup", "title": "Second"},
        {"id": "unique", "title": "Third"},
    ]
    deltas = classify(store, spec, items)
    assert len(deltas) == 2
    assert deltas[0].item_id == "dup"
    assert deltas[0].title == "First"
    assert deltas[1].item_id == "unique"


def test_classify_is_pure_read(store: Store):
    spec = WatcherSpec(name="w1", source={"file": "dummy"})
    items = [{"id": "1", "title": "New Item"}]
    classify(store, spec, items)
    # Store should not have any items
    assert store.get_item("w1", "1") is None
