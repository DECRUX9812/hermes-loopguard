from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hermes_loopguard.backends import ask_with_fallback
from hermes_loopguard.backends.base import Backend, GateResult, Usage
from hermes_loopguard.questions import (
    Answer,
    ChoiceAnswer,
    NoulAnswer,
    Question,
    ScoreAnswer,
    questions_from_spec,
    questions_version,
)


@dataclass
class RouteRule:
    name: str  # route name == a category of the route Choice question, or "review"/"drop"
    handler: str  # "agent" | "kanban" | "review" | "drop"
    min_prob: float = 0.6  # min Choice probability for this category
    when: dict[str, str] = field(default_factory=dict)  # question name -> condition ">=0.6", ">0.5", "<=1", "<0.3", "==2"
    brief: str = ""  # extra instructions placed in the agent brief for this route


@dataclass
class GateSpec:
    questions: dict[str, Question]
    route_question: str  # name of the Choice question whose categories map to routes
    routes: dict[str, RouteRule]  # category -> rule
    default_route: str = "review"  # used when below threshold, condition fails, or backend errors


@dataclass
class GateDecision:
    route: str
    handler: str
    category: str | None
    prob: float
    answers: dict[str, Answer]
    reasons: list[str]
    backend: str
    model: str
    question_version: str
    usage: Usage
    error: str | None = None


def check_condition(value: float, cond: str) -> bool:
    cond = cond.strip()
    if cond.startswith(">="):
        return value >= float(cond[2:].strip())
    elif cond.startswith("<="):
        return value <= float(cond[2:].strip())
    elif cond.startswith("!="):
        return value != float(cond[2:].strip())
    elif cond.startswith("=="):
        return value == float(cond[2:].strip())
    elif cond.startswith("="):
        return value == float(cond[1:].strip())
    elif cond.startswith(">"):
        return value > float(cond[1:].strip())
    elif cond.startswith("<"):
        return value < float(cond[1:].strip())
    else:
        return value == float(cond)


def gate_spec_from_dict(d: dict, default_min_prob: float = 0.6) -> GateSpec:
    questions = questions_from_spec(d["questions"])
    route_question = d["route_question"]
    default_route = d.get("default_route", "review")
    routes: dict[str, RouteRule] = {}
    for cat, rdata in d.get("routes", {}).items():
        if isinstance(rdata, dict):
            handler = rdata.get("handler", "review")
            min_prob = float(rdata.get("min_prob", default_min_prob))
            when = dict(rdata.get("when", {}))
            brief = rdata.get("brief", "")
            routes[cat] = RouteRule(name=cat, handler=handler, min_prob=min_prob, when=when, brief=brief)
        elif isinstance(rdata, RouteRule):
            routes[cat] = rdata

    if default_route not in routes:
        routes[default_route] = RouteRule(name=default_route, handler="review")

    return GateSpec(
        questions=questions,
        route_question=route_question,
        routes=routes,
        default_route=default_route,
    )


def decide(spec: GateSpec, answers: dict[str, Answer]) -> tuple[str, list[str]]:
    if spec.route_question not in answers:
        return spec.default_route, [f"Route question '{spec.route_question}' missing from answers"]

    choice_ans = answers[spec.route_question]
    if not isinstance(choice_ans, ChoiceAnswer):
        return spec.default_route, [f"Route question '{spec.route_question}' is not a ChoiceAnswer"]

    top_cat = choice_ans.choice
    prob = choice_ans.probabilities.get(top_cat, 0.0)
    reasons = [f"top choice is '{top_cat}' with p={prob:.2f}"]

    if top_cat not in spec.routes:
        reasons.append(
            f"category '{top_cat}' has no configured route rule, falling back to default '{spec.default_route}'"
        )
        return spec.default_route, reasons

    rule = spec.routes[top_cat]
    if prob < rule.min_prob:
        reasons.append(
            f"p={prob:.2f} below min_prob {rule.min_prob:.2f} for route '{top_cat}', falling back to default '{spec.default_route}'"
        )
        return spec.default_route, reasons

    for qname, cond in rule.when.items():
        if qname not in answers:
            reasons.append(
                f"condition question '{qname}' missing from answers, falling back to default '{spec.default_route}'"
            )
            return spec.default_route, reasons
        ans = answers[qname]
        if isinstance(ans, NoulAnswer):
            val = ans.noul
        elif isinstance(ans, ScoreAnswer):
            val = ans.score
        elif isinstance(ans, ChoiceAnswer):
            val = ans.confidence
        else:
            val = 0.0
        if not check_condition(val, cond):
            reasons.append(
                f"condition '{qname} {cond}' failed (value={val:.2f}), falling back to default '{spec.default_route}'"
            )
            return spec.default_route, reasons

    reasons.append(f"admitted to route '{top_cat}'")
    return top_cat, reasons


def evaluate(spec: GateSpec, state: str | dict, chain: list[Backend]) -> GateDecision:
    q_ver = questions_version(spec.questions)
    try:
        gate_result = ask_with_fallback(chain, state, spec.questions)
    except Exception as e:
        error_msg = str(e)
        default_rule = spec.routes.get(spec.default_route)
        handler = default_rule.handler if default_rule else "review"
        return GateDecision(
            route=spec.default_route,
            handler=handler,
            category=None,
            prob=0.0,
            answers={},
            reasons=[f"Backend error: {error_msg}, falling back to default '{spec.default_route}'"],
            backend="error",
            model="none",
            question_version=q_ver,
            usage=Usage(),
            error=error_msg,
        )

    route, reasons = decide(spec, gate_result.answers)
    rule = spec.routes.get(route)
    handler = rule.handler if rule else "review"
    choice_ans = gate_result.answers.get(spec.route_question)
    category = choice_ans.choice if isinstance(choice_ans, ChoiceAnswer) else None
    prob = (
        choice_ans.probabilities.get(category, 0.0)
        if (isinstance(choice_ans, ChoiceAnswer) and category)
        else 0.0
    )

    return GateDecision(
        route=route,
        handler=handler,
        category=category,
        prob=prob,
        answers=gate_result.answers,
        reasons=reasons,
        backend=gate_result.backend,
        model=gate_result.model,
        question_version=q_ver,
        usage=gate_result.usage,
        error=gate_result.error,
    )
