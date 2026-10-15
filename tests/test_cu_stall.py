"""Unit tests for the computer-use stall/loop detector (pure code, no
models, no Pillow, no driver)."""

from __future__ import annotations

from hermes_loopguard.cu.stall import StallDetector, is_aba_loop


def test_noop_on_identical_frames():
    d = StallDetector()
    v = d.observe("click:Submit", 0b1010, 0b1010)
    assert v.noop and v.path == "hash" and v.distance == 0
    assert not v.loop and not v.stale


def test_noop_forced_by_suspected_noop_effect():
    d = StallDetector()
    # Very different hashes, but the driver says the action was a suspected no-op.
    v = d.observe("click:Submit", 0b0000, 0b1111, force_noop=True)
    assert v.noop and v.path == "effect"


def test_changed_when_distance_above_threshold():
    d = StallDetector(unchanged_max=3, changed_min=12)
    # distance 20 (>= changed_min): real change, not a no-op.
    v = d.observe("click:Submit", 0, 0xFFFFF)
    assert not v.noop and v.distance == 20 and v.path == "hash"


def test_hash_ambiguity_resolved_by_vision_answer():
    d = StallDetector(unchanged_max=1, changed_min=8)
    # distance 4 (ambiguous band): without a Jev answer, optimistically "changed".
    v = d.observe("click:ok", 0, 0b1111)  # distance 4
    assert not v.noop and v.path == "ambiguous"
    # With the Jev answer "nothing meaningful changed" -> no-op, path "vision".
    v = d.observe("click:ok", 0, 0b1111, changed=False)
    assert v.noop and v.path == "vision"
    # With the Jev answer "changed" -> not a no-op.
    d2 = StallDetector(unchanged_max=1, changed_min=8)
    v = d2.observe("click:ok", 0, 0b1111, changed=True)
    assert not v.noop and v.path == "vision"


def test_stale_after_k_consecutive_noops():
    d = StallDetector(stale_limit=3)
    verdicts = [d.observe("click", 0, 0) for _ in range(3)]
    assert [v.stale for v in verdicts] == [False, False, True]
    assert d.stale_steps == 3
    # A real change (distance 20 >= changed_min) resets the counter.
    d.observe("click", 0, 0xFFFFF)
    assert d.stale_steps == 0


def test_aba_loop_detected_and_cleared_by_variety():
    d = StallDetector()
    seq = [("click:a", 10), ("click:b", 20), ("click:a", 10), ("click:b", 20)]
    verdicts = [d.observe(a, 0, h) for a, h in seq]
    assert not any(v.loop for v in verdicts[:3])
    assert verdicts[3].loop, "A-B-A-B must be flagged on the second B"
    # A new distinct action breaks the pattern.
    v = d.observe("click:c", 0, 30)
    assert not v.loop


def test_identical_repeats_are_stale_not_aba():
    d = StallDetector()
    for _ in range(4):
        v = d.observe("click:a", 5, 5)
    assert not v.loop
    assert v.stale


def test_is_aba_loop_needs_two_distinct_phases():
    assert not is_aba_loop([("a", 1), ("a", 1), ("a", 1), ("a", 1)])
    assert not is_aba_loop([("a", 1), ("b", 2), ("a", 1)])
    assert is_aba_loop([("a", 1), ("b", 2), ("a", 1), ("b", 2)])
    assert not is_aba_loop([("a", 1), ("b", 2), ("a", 1), ("b", 3)])
