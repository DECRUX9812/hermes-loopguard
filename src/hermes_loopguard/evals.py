"""Offline evaluation harness for the semantic gate.

Labelled JSONL cases -> one `semantic_gate.evaluate` per case -> accuracy,
misses (expected an admitting route, gate did not admit), false admits
(gate admitted, expected something else), a confusion matrix and a
threshold sweep re-decided from cached answers with no extra model calls.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

ADMIT_HANDLERS = {"agent", "kanban"}


@dataclass
class EvalCase:
    id: str
    state: dict | str
    expected_route: str
    note: str = ""


@dataclass
class EvalRow:
    case: EvalCase
    predicted_route: str
    decision: Any  # semantic_gate.GateDecision


@dataclass
class EvalReport:
    n: int
    accuracy: float
    misses: list = field(default_factory=list)
    false_admits: list = field(default_factory=list)
    confusion: dict = field(default_factory=dict)
    sweep: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def load_cases(path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            cases.append(EvalCase(id=d["id"], state=d["state"], expected_route=d["expected_route"],
                                  note=d.get("note", "")))
    return cases


def _handler_for(spec, route: str) -> str:
    rule = spec.routes.get(route)
    return rule.handler if rule is not None else "review"


def _tally(spec, pairs):
    confusion: dict[str, dict[str, int]] = {}
    misses: list[tuple] = []
    false_admits: list[tuple] = []
    correct = 0
    for case, predicted in pairs:
        row = confusion.setdefault(case.expected_route, {})
        row[predicted] = row.get(predicted, 0) + 1
        if case.expected_route == predicted:
            correct += 1
        exp_admits = _handler_for(spec, case.expected_route) in ADMIT_HANDLERS
        pred_admits = _handler_for(spec, predicted) in ADMIT_HANDLERS
        if exp_admits and not pred_admits:
            misses.append((case, predicted))
        if pred_admits and not exp_admits:
            false_admits.append((case, predicted))
    n = len(pairs)
    accuracy = correct / n if n else 0.0
    return confusion, misses, false_admits, accuracy


def _usage_totals(decisions) -> dict:
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    for decision in decisions:
        u = getattr(decision, "usage", None)
        usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        usage["cost_usd"] += getattr(u, "cost_usd", 0.0) or 0.0
        usage["calls"] += 1
    return usage


def run_eval(spec, cases, chain) -> EvalReport:
    from hermes_loopguard import semantic_gate

    rows = []
    for case in cases:
        decision = semantic_gate.evaluate(spec, case.state, chain)
        rows.append(EvalRow(case=case, predicted_route=decision.route, decision=decision))
    return _build_report(spec, rows)


def _build_report(spec, rows: list[EvalRow]) -> EvalReport:
    confusion, miss_pairs, fa_pairs, accuracy = _tally(
        spec, [(row.case, row.predicted_route) for row in rows])
    miss_ids = {(c.id, p) for c, p in miss_pairs}
    fa_ids = {(c.id, p) for c, p in fa_pairs}
    misses = [r for r in rows if (r.case.id, r.predicted_route) in miss_ids]
    false_admits = [r for r in rows if (r.case.id, r.predicted_route) in fa_ids]
    report = EvalReport(n=len(rows), accuracy=accuracy, misses=misses, false_admits=false_admits,
                        confusion=confusion, rows=rows,
                        usage=_usage_totals(r.decision for r in rows))
    report.sweep = threshold_sweep(spec, rows)
    return report


def _spec_at_threshold(spec, threshold: float):
    routes = {}
    for name, rule in spec.routes.items():
        if rule.handler in ADMIT_HANDLERS:
            rule = replace(rule, min_prob=threshold)
        routes[name] = rule
    return replace(spec, routes=routes)


def threshold_sweep(spec, rows, thresholds=(0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)) -> list[dict]:
    from hermes_loopguard import semantic_gate

    out = []
    for t in thresholds:
        swept = _spec_at_threshold(spec, t)
        pairs = []
        for row in rows:
            route, _reasons = semantic_gate.decide(swept, row.decision.answers)
            pairs.append((row.case, route))
        _confusion, misses, false_admits, accuracy = _tally(spec, pairs)
        out.append({"threshold": t, "misses": len(misses), "false_admits": len(false_admits),
                    "accuracy": accuracy})
    return out


def format_report(report: EvalReport) -> str:
    lines = []
    usage = report.usage
    lines.append(f"LoopGuard eval: {report.n} cases, accuracy {report.accuracy:.1%}, "
                 f"tokens in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)} "
                 f"cost=${usage.get('cost_usd', 0.0):.4f}, backend calls={usage.get('calls', 0)}")

    routes = sorted(set(report.confusion)
                    | {p for row in report.confusion.values() for p in row})
    lines.append("")
    lines.append("Confusion matrix (expected -> predicted):")
    lines.append("expected \\ predicted".ljust(24)
                 + "".join(r[:14].rjust(18) for r in routes))
    for expected in routes:
        cells = report.confusion.get(expected, {})
        lines.append(expected[:22].ljust(24)
                     + "".join(str(cells.get(r, 0)).rjust(18) for r in routes))

    def section(title: str, rows: list[EvalRow]):
        lines.append("")
        lines.append(f"{title} ({len(rows)}):")
        if not rows:
            lines.append("  none")
        for row in rows:
            note = f" -- {row.case.note}" if row.case.note else ""
            lines.append(f"  {row.case.id}: expected {row.case.expected_route} -> "
                         f"predicted {row.predicted_route} "
                         f"(top prob {getattr(row.decision, 'prob', 0):.2f}){note}")

    section("Misses (expected an admitting route, gate did not admit)", report.misses)
    section("False admits (gate admitted, expected something else)", report.false_admits)

    lines.append("")
    lines.append("Threshold sweep (min_prob on admitting routes; marker = fewest false "
                 "admits among those with the fewest misses):")
    lines.append("  threshold  misses  false_admits  accuracy")
    min_misses = min((e["misses"] for e in report.sweep), default=0)
    best_fa = min((e["false_admits"] for e in report.sweep if e["misses"] == min_misses),
                  default=0)
    for entry in report.sweep:
        marker = "*" if (entry["misses"] == min_misses
                         and entry["false_admits"] == best_fa) else " "
        lines.append(f"{marker} {entry['threshold']:<9} {entry['misses']:>7} "
                     f"{entry['false_admits']:>14} {entry['accuracy']:>9.1%}")
    return "\n".join(lines)
