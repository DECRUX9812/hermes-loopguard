"""Stall and loop detection for the computer-use agent (X3).

Pure code first: a dhash ring buffer of recent frames plus the action
sequence.  An action whose before/after frames did not change (hash path;
Jev `gate.changed` only when the hash is ambiguous) counts as a no-op, as
does the driver reporting `effect="suspected_noop"`.  A-B-A-B patterns
(same action alternating between two screens) and K consecutive no-change
steps are flagged so the agent can stop retrying and go terminal.

No Pillow and no model calls here: callers pass pre-computed hashes.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StallVerdict", "StallDetector", "is_aba_loop"]


@dataclass
class StallVerdict:
    """Outcome of observing one action's before/after frames."""

    noop: bool = False  # the action changed nothing
    loop: bool = False  # A-B-A-B pattern detected
    stale: bool = False  # too many consecutive no-change steps
    distance: int = -1  # hamming distance between the two hashes
    path: str = "hash"  # "hash" | "vision" | "effect" | "ambiguous"


def is_aba_loop(history: list[tuple[str, int]], period: int = 2) -> bool:
    """True when the last ``2 * period`` (action, frame-hash) entries repeat
    with that period (e.g. A-B-A-B for period 2).  Entries must be pairwise
    equal AND the two periods must differ (identical repeats are staleness,
    not a loop)."""
    n = 2 * period
    if len(history) < n:
        return False
    window = history[-n:]
    first, second = window[:period], window[period:]
    if first != second:
        return False
    return len(set(first)) > 1


class StallDetector:
    """Ring buffer of (action, after-frame hash) plus a stale-step counter.

    ``observe`` is called once per attempted action with the before/after
    dhashes.  When the hash distance is ambiguous (between the unchanged and
    changed thresholds) the caller may pass ``changed`` from the Jev
    ``gate.changed`` answer; without it the step is optimistically treated as
    changed.  ``force_noop=True`` (driver reported ``suspected_noop``)
    counts as a no-op regardless of the hashes.
    """

    def __init__(
        self,
        *,
        unchanged_max: int = 3,
        changed_min: int = 12,
        stale_limit: int = 4,
        ring: int = 8,
    ):
        self.unchanged_max = int(unchanged_max)
        self.changed_min = int(changed_min)
        self.stale_limit = max(1, int(stale_limit))
        self.ring = max(4, int(ring))
        self.history: list[tuple[str, int]] = []
        self.stale_steps = 0

    def observe(
        self,
        action: str,
        before_hash: int,
        after_hash: int,
        *,
        changed: bool | None = None,
        force_noop: bool = False,
    ) -> StallVerdict:
        distance = (before_hash ^ after_hash).bit_count()
        if force_noop:
            unchanged, path = True, "effect"
        elif changed is not None:
            # An explicit answer (Jev gate.changed, or a verify that passed)
            # outranks the hash: dhash cannot see small meaningful changes
            # like typed text or focus rings.
            unchanged, path = not changed, "vision"
        elif distance <= self.unchanged_max:
            unchanged, path = True, "hash"
        elif distance >= self.changed_min:
            unchanged, path = False, "hash"
        else:
            unchanged, path = False, "ambiguous"  # Jev unavailable: not a no-op
        self.stale_steps = self.stale_steps + 1 if unchanged else 0
        self.history.append((str(action), int(after_hash)))
        del self.history[:-self.ring]
        return StallVerdict(
            noop=unchanged,
            loop=is_aba_loop(self.history),
            stale=self.stale_steps >= self.stale_limit,
            distance=distance,
            path=path,
        )
