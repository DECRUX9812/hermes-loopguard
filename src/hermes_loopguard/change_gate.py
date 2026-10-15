from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_loopguard.config import WatcherSpec
from hermes_loopguard.fingerprint import dotted_get, fingerprint, item_identity


@dataclass
class ItemDelta:
    item_id: str
    title: str
    fingerprint: str
    item: dict
    status: str
    prior: dict | None


ACTIONABLE = {"new", "changed"}


def classify(store, spec: WatcherSpec, items: list[dict], *, now: float | None = None) -> list[ItemDelta]:
    deltas: list[ItemDelta] = []
    seen_ids = set()

    for item in items:
        try:
            item_id = item_identity(item, spec.id_field)
        except Exception:
            continue
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        title = str(dotted_get(item, spec.title_field, default=item_id))
        fp = fingerprint(item, spec.fingerprint_fields)
        prior = store.get_item(spec.name, item_id)

        if prior is None:
            status = "new"
        elif prior.get("state") == "budget_exhausted" or prior.get("spend_tokens", 0) >= spec.spend_cap_tokens:
            status = "budget_exhausted"
        elif prior.get("fingerprint") == fp:
            status = "unchanged"
        elif prior.get("state") in {"blocked", "failed", "claimed"} and prior.get("attempts", 0) >= spec.max_attempts:
            status = "attempts_exhausted"
        elif store.claim_live(spec.name, item_id, now=now):
            status = "claimed"
        else:
            status = "changed"

        deltas.append(
            ItemDelta(
                item_id=item_id,
                title=title,
                fingerprint=fp,
                item=item,
                status=status,
                prior=prior,
            )
        )

    return deltas
