from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from hermes_loopguard import sources
from hermes_loopguard.backends.base import Usage
from hermes_loopguard.change_gate import ACTIONABLE, ItemDelta, classify
from hermes_loopguard.config import WatcherSpec
from hermes_loopguard.db import Store
from hermes_loopguard.fingerprint import canonical, dotted_get
from hermes_loopguard.questions import answer_to_dict
from hermes_loopguard.semantic_gate import GateDecision, evaluate, gate_spec_from_dict


@dataclass
class RoutedItem:
    delta: ItemDelta
    route: str
    handler: str
    outcome: str
    decision: GateDecision | None = None
    prob: float | None = None


@dataclass
class PollResult:
    watcher: str
    total: int
    deltas: list[ItemDelta]
    routed: list[RoutedItem]
    wake: bool
    brief: str
    shadow: bool
    run_id: int | None
    gate_usage: Usage
    est_tokens_avoided: int = 0
    est_usd_avoided: float = 0.0
    errors: list[str] = field(default_factory=list)


_KANBAN_TASK_RE = re.compile(r"t_[A-Za-z0-9]+")
_INT_RE = re.compile(r"\b(\d+)\b")


def _hermes_bin() -> str:
    """The hermes binary that runs this process, not whatever `hermes` PATH
    resolves to: cron scripts run on the install's own Python
    (cron/scheduler_script.py::_script_argv), and PATH may hold a different
    install (e.g. ~/.local/bin/hermes)."""
    if sys.executable:
        sibling = Path(sys.executable).with_name("hermes")
        if sibling.is_file():
            return str(sibling)
    return "hermes"


def build_kanban_argv(spec: WatcherSpec, delta: ItemDelta) -> list[str]:
    """Argv for `hermes kanban create`, matching stock Hermes' CLI:
    `title` is positional; `--board` is a TOP-LEVEL `hermes kanban` flag and
    must come before the `create` subcommand; assignee/tenant are `create`
    options; `--max-retries N` trips the failure breaker on the Nth failure,
    i.e. N total attempts — exactly LoopGuard's `max_attempts`."""
    cmd: list[str] = [_hermes_bin(), "kanban"]
    if spec.kanban.get("board"):
        cmd.extend(["--board", str(spec.kanban["board"])])
    cmd.extend([
        "create",
        delta.title,
        "--body",
        canonical(delta.item),
        "--idempotency-key",
        f"loopguard:{spec.name}:{delta.item_id}:{delta.fingerprint}",
        "--created-by",
        "loopguard",
        "--json",
        "--max-retries",
        str(spec.max_attempts),
    ])
    if spec.kanban.get("assignee"):
        cmd.extend(["--assignee", str(spec.kanban["assignee"])])
    if spec.kanban.get("tenant"):
        cmd.extend(["--tenant", str(spec.kanban["tenant"])])
    return cmd


def parse_kanban_task_id(output: str) -> str | None:
    """Extract the created task id from `hermes kanban create` output."""
    text = (output or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("id", "task_id"):
            if data.get(key):
                return str(data[key])
    m = _KANBAN_TASK_RE.search(text)
    if m:
        return m.group(0)
    m = _INT_RE.search(text)
    if m:
        return m.group(1)
    return None


def build_brief(spec: WatcherSpec, routed: list[RoutedItem], skipped: int, n_deferred: int = 0) -> str:
    admitted = [r for r in routed if r.outcome == "admitted"]
    lines = [f"## LoopGuard brief: {spec.name}"]
    if spec.brief:
        lines.append(spec.brief)

    counts_str = f"Admitted {len(admitted)} items ({skipped} skipped"
    if n_deferred > 0:
        counts_str += f", {n_deferred} deferred"
    counts_str += ")."
    lines.append(counts_str)
    lines.append("")

    closing = "Report each item on its own line as DONE <id> or BLOCKED <id>: <reason>."

    gate_spec = gate_spec_from_dict(spec.gate) if spec.gate else None
    sections: list[str] = []
    for r in admitted:
        delta = r.delta
        item_lines = [f"### {delta.item_id}: {delta.title}"]
        prob_str = f"p={r.prob:.2f}" if r.prob is not None else "p=1.00"
        item_lines.append(f"route={r.route} {prob_str}")

        route_brief = ""
        if gate_spec and r.route in gate_spec.routes:
            route_brief = gate_spec.routes[r.route].brief
        if route_brief:
            item_lines.append(route_brief)

        fields = spec.state_fields or spec.fingerprint_fields or list(delta.item.keys())
        state = {"title": delta.title, **{f: dotted_get(delta.item, f) for f in fields}}
        state_json = canonical(state)
        item_lines.append("```json")
        item_lines.append(state_json)
        item_lines.append("```")
        sections.append("\n".join(item_lines))

    # Keep total chars <= spec.max_brief_chars
    header = "\n".join(lines) + "\n"
    overhead = len(header) + len(closing) + 4
    if sections:
        avail = max(200, spec.max_brief_chars - overhead)
        per_sec = max(80, avail // len(sections))
        final_sections = []
        for s in sections:
            if len(s) > per_sec:
                final_sections.append(s[: max(50, per_sec - 25)] + "\n...[truncated]\n```")
            else:
                final_sections.append(s)
        sections = final_sections

    brief_text = header + "\n\n".join(sections) + "\n\n" + closing
    if len(brief_text) > spec.max_brief_chars:
        cutoff = max(100, spec.max_brief_chars - len(closing) - 30)
        brief_text = brief_text[:cutoff] + "\n...[truncated]\n\n" + closing

    return brief_text


def poll(
    spec: WatcherSpec,
    *,
    store: Store,
    cfg: dict,
    owner: str,
    chain_factory: Callable[[], list[Any]] | None = None,
    items: list[dict] | None = None,
    dry_run: bool = False,
    kanban_runner: Callable[[WatcherSpec, ItemDelta, GateDecision | None], Any] | None = None,
) -> PollResult:
    if items is None:
        items = sources.load_items(spec.source)

    deltas = classify(store, spec, items)

    actionable = [d for d in deltas if d.status in ACTIONABLE]
    non_actionable = [d for d in deltas if d.status not in ACTIONABLE]

    n_unchanged = sum(1 for d in non_actionable if d.status == "unchanged")
    n_other = len(non_actionable) - n_unchanged

    if not dry_run:
        # Summary row only when there is something non-actionable; a poll
        # that admitted everything logs no skip rows at all.
        if non_actionable:
            store.log_decision(
                watcher=spec.name,
                kind="change_gate",
                outcome="skip",
                note=f"{n_unchanged} unchanged, {n_other} other",
            )
        for d in non_actionable:
            if d.status != "unchanged":
                store.log_decision(
                    watcher=spec.name,
                    kind="change_gate",
                    item_id=d.item_id,
                    outcome=d.status,
                )

    to_process = actionable[: spec.max_items_per_run]
    deferred = actionable[spec.max_items_per_run:]

    gate_usage = Usage(0, 0, 0.0)
    chain = None
    gate_spec = None
    if spec.gate is not None:
        gate_spec = gate_spec_from_dict(spec.gate, default_min_prob=cfg.get("route_min_prob", 0.6))
        if to_process:
            if chain_factory is not None:
                chain = chain_factory()
            else:
                from hermes_loopguard.backends import backend_chain

                chain = backend_chain(cfg)

    is_shadow = spec.shadow if spec.shadow is not None else cfg.get("shadow", False)
    routed: list[RoutedItem] = []

    for delta in to_process:
        force_key = f"force_route:{spec.name}:{delta.item_id}"
        forced = store.get_kv(force_key)
        if forced:
            route = str(forced)
            rule = gate_spec.routes.get(route) if gate_spec else None
            handler = rule.handler if rule is not None else "agent"
            decision = None
            prob = None
            if not dry_run:
                store.log_decision(
                    watcher=spec.name,
                    kind="route",
                    item_id=delta.item_id,
                    route=route,
                    proposed_route=route,
                    outcome="forced",
                    note="forced by review",
                )
                store.set_kv(force_key, None)
        elif gate_spec is not None:
            fields = spec.state_fields or spec.fingerprint_fields or list(delta.item.keys())
            state = {"title": delta.title, **{f: dotted_get(delta.item, f) for f in fields}}
            decision = evaluate(gate_spec, state, chain or [])
            route = decision.route
            handler = decision.handler
            prob = decision.prob
            gate_usage.input_tokens += decision.usage.input_tokens
            gate_usage.output_tokens += decision.usage.output_tokens
            gate_usage.cost_usd += decision.usage.cost_usd

            if not dry_run:
                ans_dicts = {k: answer_to_dict(v) for k, v in decision.answers.items()}
                store.log_decision(
                    watcher=spec.name,
                    kind="semantic",
                    item_id=delta.item_id,
                    backend=decision.backend,
                    model=decision.model,
                    question_version=decision.question_version,
                    answers=ans_dicts,
                    tokens_in=decision.usage.input_tokens,
                    tokens_out=decision.usage.output_tokens,
                    cost_usd=decision.usage.cost_usd,
                    route=decision.route,
                )
        else:
            route = "agent"
            handler = "agent"
            decision = None
            prob = None

        if is_shadow:
            if not dry_run:
                store.upsert_item(spec.name, delta.item_id, fingerprint=delta.fingerprint, title=delta.title)
                store.log_decision(
                    watcher=spec.name,
                    kind="route",
                    item_id=delta.item_id,
                    route=route,
                    proposed_route=route,
                    shadow=True,
                    outcome="shadow",
                )
            routed.append(
                RoutedItem(
                    delta=delta,
                    route=route,
                    handler=handler,
                    outcome="shadow",
                    decision=decision,
                    prob=prob,
                )
            )
        else:
            if not dry_run:
                store.upsert_item(spec.name, delta.item_id, fingerprint=delta.fingerprint, title=delta.title)

            if handler == "agent":
                if dry_run:
                    outcome = "admitted"
                else:
                    claimed = store.try_claim(
                        spec.name,
                        delta.item_id,
                        delta.fingerprint,
                        owner,
                        ttl_s=spec.claim_ttl_s,
                    )
                    if claimed:
                        store.set_item_state(spec.name, delta.item_id, "claimed", route=route)
                        store.bump_attempt(spec.name, delta.item_id, delta.fingerprint)
                        outcome = "admitted"
                    else:
                        outcome = "claim_lost"

            elif handler == "kanban":
                if dry_run:
                    outcome = "kanban_created"
                else:
                    claimed = store.try_claim(
                        spec.name,
                        delta.item_id,
                        delta.fingerprint,
                        owner,
                        ttl_s=spec.claim_ttl_s,
                    )
                    if claimed:
                        if kanban_runner is None:

                            def default_kanban_runner(s, d, dec):
                                cmd = build_kanban_argv(s, d)
                                proc = subprocess.run(cmd, capture_output=True, text=True)
                                if proc.returncode != 0:
                                    raise RuntimeError(f"kanban create failed: {proc.stderr}")
                                task_id = parse_kanban_task_id(proc.stdout)
                                if task_id:
                                    store.set_kv(
                                        f"kanban_task:{task_id}",
                                        {
                                            "watcher": s.name,
                                            "item_id": d.item_id,
                                            "fingerprint": d.fingerprint,
                                        },
                                    )

                            kanban_runner = default_kanban_runner

                        try:
                            kanban_runner(spec, delta, decision)
                            store.set_item_state(spec.name, delta.item_id, "claimed", route=route)
                            outcome = "kanban_created"
                        except Exception:
                            store.release_claim(spec.name, delta.item_id)
                            outcome = "kanban_error"
                    else:
                        outcome = "claim_lost"

            elif handler == "review":
                if not dry_run:
                    reason = "; ".join(decision.reasons) if decision else "queued for review"
                    store.enqueue_review(
                        spec.name,
                        delta.item_id,
                        title=delta.title,
                        reason=reason,
                        payload=delta.item,
                    )
                    store.set_item_state(spec.name, delta.item_id, "review", route=route)
                outcome = "queued"

            elif handler == "drop":
                if not dry_run:
                    store.set_item_state(spec.name, delta.item_id, "dropped", route=route)
                outcome = "dropped"

            else:
                if not dry_run:
                    store.set_item_state(spec.name, delta.item_id, "review", route=route)
                outcome = "queued"

            if not dry_run:
                store.log_decision(
                    watcher=spec.name,
                    kind="route",
                    item_id=delta.item_id,
                    route=route,
                    proposed_route=route,
                    outcome=outcome,
                )
            routed.append(
                RoutedItem(
                    delta=delta,
                    route=route,
                    handler=handler,
                    outcome=outcome,
                    decision=decision,
                    prob=prob,
                )
            )

    if is_shadow:
        wake = True
        brief = ""
    else:
        admitted = [r for r in routed if r.outcome == "admitted"]
        wake = len(admitted) > 0
        n_skipped = len(deltas) - len(admitted)
        brief = build_brief(spec, routed, n_skipped, n_deferred=len(deferred)) if wake else ""

    run_id = None
    if wake and not dry_run:
        admitted_ids = [r.delta.item_id for r in routed if r.outcome == "admitted"]
        run_id = store.open_run(watcher=spec.name, owner=owner, items=admitted_ids)

    est_tokens_avoided = 0
    est_usd_avoided = 0.0
    if not wake and not is_shadow:
        recent = store.recent_run_tokens(spec.name)
        if recent:
            est_tokens_avoided = int(statistics.median(recent))
        else:
            est_tokens_avoided = int(cfg.get("pricing", {}).get("est_tokens_per_run", 25000))
        usd_per_mtok = float(cfg.get("pricing", {}).get("usd_per_mtok", 3.0))
        est_usd_avoided = (est_tokens_avoided * usd_per_mtok) / 1e6

    if not dry_run:
        n_admitted = sum(1 for r in routed if r.outcome == "admitted")
        n_changed = len(actionable)
        store.log_poll(
            watcher=spec.name,
            n_items=len(deltas),
            n_changed=n_changed,
            n_admitted=n_admitted,
            woke=wake,
            shadow=is_shadow,
            est_tokens_avoided=est_tokens_avoided,
            est_usd_avoided=est_usd_avoided,
            gate_tokens=gate_usage.input_tokens + gate_usage.output_tokens,
            gate_usd=gate_usage.cost_usd,
        )

    return PollResult(
        watcher=spec.name,
        total=len(deltas),
        deltas=deltas,
        routed=routed,
        wake=wake,
        brief=brief,
        shadow=is_shadow,
        run_id=run_id,
        gate_usage=gate_usage,
        est_tokens_avoided=est_tokens_avoided,
        est_usd_avoided=est_usd_avoided,
        errors=[],
    )
