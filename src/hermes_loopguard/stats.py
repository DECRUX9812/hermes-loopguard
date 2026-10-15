"""Savings and activity statistics over the LoopGuard store.

Pure reads: `compute_stats` never writes, and `format_stats` is plain text
(no colour codes, so it is safe to pipe or paste).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DECISION_SCAN_LIMIT = 100000
_RECENT_LIMIT = 25
_TIMELINE_DAYS = 14


def parse_since(s: str | None) -> float | None:
    """Turn "24h" / "7d" / "30m" into an absolute unix timestamp, or None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    text = str(s).strip().lower()
    if not text or text in {"all", "any", "forever"}:
        return None
    if text.isdigit():
        return time.time() - int(text)
    unit = text[-1]
    if unit not in _UNITS:
        return None
    try:
        amount = float(text[:-1])
    except ValueError:
        return None
    return time.time() - amount * _UNITS[unit]


def _item_states(store, watcher: str | None = None) -> dict[str, int]:
    query = "SELECT state, COUNT(*) AS n FROM items"
    params: list = []
    if watcher:
        query += " WHERE watcher = ?"
        params.append(watcher)
    query += " GROUP BY state"
    cur = store.conn.cursor()
    cur.execute(query, params)
    return {row["state"]: int(row["n"]) for row in cur.fetchall()}


def _core(polls, runs, decisions, item_states, usd_per_mtok) -> dict:
    real = [p for p in polls if not p.get("shadow")]
    shadow = [p for p in polls if p.get("shadow")]
    skipped = sum(1 for p in real if not p.get("woke"))
    woken = sum(1 for p in real if p.get("woke"))
    total_runs = skipped + woken

    gate_tokens = sum(int(p.get("gate_tokens") or 0) for p in polls)
    gate_usd = sum(float(p.get("gate_usd") or 0.0) for p in polls)
    if not gate_usd and gate_tokens:
        gate_usd = gate_tokens * usd_per_mtok / 1e6

    agent_tokens = sum(
        int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in runs
    )
    agent_usd = sum(float(r.get("usd") or 0.0) for r in runs)
    if not agent_usd and agent_tokens:
        agent_usd = agent_tokens * usd_per_mtok / 1e6

    tokens_avoided = sum(int(p.get("est_tokens_avoided") or 0) for p in polls)
    usd_avoided = sum(float(p.get("est_usd_avoided") or 0.0) for p in polls)

    routes: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    errors = 0
    items_gated = 0
    for d in decisions:
        if d.get("kind") == "semantic":
            items_gated += 1
        if d.get("kind") == "route" and d.get("route"):
            routes[d["route"]] = routes.get(d["route"], 0) + 1
        outcome = d.get("outcome")
        if outcome:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == "error":
                errors += 1

    completed_items = int(item_states.get("completed", 0))
    return {
        "polls": len(polls),
        "skipped_runs": skipped,
        "woken_runs": woken,
        "skip_rate": (skipped / total_runs) if total_runs else 0.0,
        "shadow_polls": len(shadow),
        "shadow_would_skip": sum(1 for p in shadow if int(p.get("n_admitted") or 0) == 0),
        "items_gated": items_gated,
        "routes": routes,
        "outcomes": outcomes,
        "gate_tokens": gate_tokens,
        "gate_usd": gate_usd,
        "agent_tokens": agent_tokens,
        "agent_usd": agent_usd,
        "tokens_avoided": tokens_avoided,
        "usd_avoided": usd_avoided,
        "net_usd_saved": usd_avoided - gate_usd,
        "completed_items": completed_items,
        "cost_per_completed": ((agent_usd + gate_usd) / completed_items) if completed_items else None,
        "errors": errors,
    }


def _watcher_names(store) -> list[str]:
    names: set[str] = set()
    cur = store.conn.cursor()
    for table in ("polls", "runs", "items", "decisions"):
        try:
            cur.execute(f"SELECT DISTINCT watcher FROM {table}")
        except Exception:
            continue
        names.update(row["watcher"] for row in cur.fetchall() if row["watcher"])
    return sorted(names)


def _per_watcher(store, watcher, since_ts, usd_per_mtok) -> dict:
    names = [watcher] if watcher else _watcher_names(store)
    out = {}
    for name in names:
        polls = store.polls(watcher=name, since=since_ts)
        runs = store.runs(watcher=name, since=since_ts)
        decisions = store.decisions(watcher=name, limit=_DECISION_SCAN_LIMIT)
        if since_ts is not None:
            decisions = [d for d in decisions if (d.get("created_at") or 0) >= since_ts]
        out[name] = _core(polls, runs, decisions, _item_states(store, name), usd_per_mtok)
    return out


def _timeline(polls) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    days: dict[str, dict] = {}
    order = []
    for offset in range(_TIMELINE_DAYS - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        order.append(day)
        days[day] = {
            "day": day,
            "skipped": 0,
            "woken": 0,
            "tokens_avoided": 0,
            "usd_avoided": 0.0,
        }
    for p in polls:
        if p.get("shadow"):
            continue
        ts = p.get("created_at")
        if not ts:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        bucket = days.get(day)
        if bucket is None:
            continue
        if p.get("woke"):
            bucket["woken"] += 1
        else:
            bucket["skipped"] += 1
        bucket["tokens_avoided"] += int(p.get("est_tokens_avoided") or 0)
        bucket["usd_avoided"] += float(p.get("est_usd_avoided") or 0.0)
    return [days[day] for day in order]


def _recent(decisions) -> list[dict]:
    keys = (
        "id",
        "created_at",
        "watcher",
        "kind",
        "item_id",
        "route",
        "proposed_route",
        "backend",
        "model",
        "outcome",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "note",
        "answers",
    )
    out = []
    for d in decisions[:_RECENT_LIMIT]:
        compact = {k: d.get(k) for k in keys}
        compact["shadow"] = bool(d.get("shadow"))
        out.append(compact)
    return out


def compute_stats(store, cfg, *, watcher=None, since=None) -> dict:
    since_ts = parse_since(since) if isinstance(since, str) else since
    usd_per_mtok = float((cfg or {}).get("pricing", {}).get("usd_per_mtok", 3.0))

    polls = store.polls(watcher=watcher, since=since_ts)
    runs = store.runs(watcher=watcher, since=since_ts)
    decisions = store.decisions(watcher=watcher, limit=_DECISION_SCAN_LIMIT)
    if since_ts is not None:
        decisions = [d for d in decisions if (d.get("created_at") or 0) >= since_ts]

    stats = _core(polls, runs, decisions, _item_states(store, watcher), usd_per_mtok)
    stats["review_open"] = len(
        [r for r in store.review_queue("open") if watcher is None or r.get("watcher") == watcher]
    )
    stats["per_watcher"] = _per_watcher(store, watcher, since_ts, usd_per_mtok)
    stats["timeline"] = _timeline(polls)
    stats["recent"] = _recent(decisions)
    stats["generated_at"] = time.time()
    return stats


def _fmt_tokens(value) -> str:
    n = float(value or 0)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.1f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{int(n)}"


def _fmt_usd(value) -> str:
    return f"${float(value or 0.0):.2f}"


def format_headline(stats: dict) -> str:
    skipped = stats.get("skipped_runs", 0)
    pct = float(stats.get("skip_rate", 0.0)) * 100
    per_task = stats.get("cost_per_completed")
    tail = f"{_fmt_usd(per_task)} per completed task" if per_task is not None else "n/a per completed task"
    return (
        f"LoopGuard: {skipped} runs skipped ({pct:.0f}%), "
        f"~{_fmt_tokens(stats.get('tokens_avoided'))} tokens / "
        f"{_fmt_usd(stats.get('usd_avoided'))} avoided, "
        f"gate cost {_fmt_usd(stats.get('gate_usd'))}, {tail}"
    )


def format_watcher_table(stats: dict) -> list[str]:
    per_watcher = stats.get("per_watcher") or {}
    if not per_watcher:
        return []
    lines = [
        "Watcher".ljust(22)
        + "Polls".rjust(7)
        + "Skipped".rjust(9)
        + "Woken".rjust(7)
        + "Avoided".rjust(10)
        + "Gate $".rjust(9)
        + "Net $".rjust(9)
    ]
    for name in sorted(per_watcher):
        w = per_watcher[name]
        lines.append(
            name[:21].ljust(22)
            + str(w.get("polls", 0)).rjust(7)
            + str(w.get("skipped_runs", 0)).rjust(9)
            + str(w.get("woken_runs", 0)).rjust(7)
            + _fmt_tokens(w.get("tokens_avoided")).rjust(10)
            + _fmt_usd(w.get("gate_usd")).rjust(9)
            + _fmt_usd(w.get("net_usd_saved")).rjust(9)
        )
    return lines


def format_stats(stats: dict) -> str:
    lines = [format_headline(stats), ""]
    table = format_watcher_table(stats)
    if table:
        lines.extend(table)
    else:
        lines.append("No watcher activity recorded.")

    routes = stats.get("routes") or {}
    if routes:
        lines.append("")
        lines.append("Routes: " + ", ".join(f"{k} {v}" for k, v in sorted(routes.items())))

    lines.append("")
    lines.append(
        f"Polls {stats.get('polls', 0)} (shadow {stats.get('shadow_polls', 0)}), "
        f"items gated {stats.get('items_gated', 0)}, "
        f"gate tokens {_fmt_tokens(stats.get('gate_tokens'))}, "
        f"agent tokens {_fmt_tokens(stats.get('agent_tokens'))}, "
        f"completed {stats.get('completed_items', 0)}, "
        f"open reviews {stats.get('review_open', 0)}, errors {stats.get('errors', 0)}"
    )
    return "\n".join(lines)
