from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Union


@dataclass(frozen=True)
class Choice:
    instructions: str
    criteria: dict[str, str | None]
    type: str = "choice"


@dataclass(frozen=True)
class Noul:
    instructions: str
    criteria: dict[str, str] | None = None
    type: str = "noul"


@dataclass(frozen=True)
class Score:
    instructions: str
    criteria: list[str]
    type: str = "score"


Question = Union[Choice, Noul, Score]


@dataclass
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    type: str = "choice"


@dataclass
class NoulAnswer:
    noul: float
    type: str = "noul"


@dataclass
class ScoreAnswer:
    score: float
    confidence: float
    probabilities: dict[int, float]
    legend: dict[int, str]
    type: str = "score"


Answer = Union[ChoiceAnswer, NoulAnswer, ScoreAnswer]


def normalize(probs: dict) -> dict:
    if not probs:
        return {}
    cleaned = {k: max(0.0, float(v)) for k, v in probs.items()}
    total = sum(cleaned.values())
    if total <= 0.0:
        n = len(cleaned)
        return {k: 1.0 / n for k in cleaned}
    return {k: v / total for k, v in cleaned.items()}


def choice_answer(probs: dict[str, float]) -> ChoiceAnswer:
    norm = normalize(probs)
    if not norm:
        return ChoiceAnswer(choice="", confidence=0.0, probabilities={})
    best_choice = max(norm, key=lambda k: norm[k])
    return ChoiceAnswer(choice=best_choice, confidence=norm[best_choice], probabilities=norm)


def score_answer(probs: dict[int, float], levels: list[str]) -> ScoreAnswer:
    int_probs = {}
    for i in range(len(levels)):
        val = probs.get(i)
        if val is None:
            val = probs.get(str(i), 0.0)
        int_probs[i] = float(val)
    norm = normalize(int_probs)
    score = sum(lvl * p for lvl, p in norm.items())
    confidence = max(norm.values()) if norm else 0.0
    legend = {i: lvl for i, lvl in enumerate(levels)}
    return ScoreAnswer(score=score, confidence=confidence, probabilities=norm, legend=legend)


def question_from_spec(spec: dict) -> Question:
    if not isinstance(spec, dict):
        raise ValueError("Question spec must be a dict")
    qtype = spec.get("type", "choice")
    instructions = spec.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Question spec must have non-empty string instructions")
    if qtype == "choice":
        criteria = spec.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError("Choice question criteria must be a non-empty dict")
        return Choice(instructions=instructions, criteria=criteria)
    elif qtype == "noul":
        criteria = spec.get("criteria")
        if criteria is not None and not isinstance(criteria, dict):
            raise ValueError("Noul question criteria must be a dict or None")
        return Noul(instructions=instructions, criteria=criteria)
    elif qtype == "score":
        criteria = spec.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError("Score question criteria must be a non-empty list of levels")
        return Score(instructions=instructions, criteria=criteria)
    else:
        raise ValueError(f"Unknown question type: {qtype}")


def questions_from_spec(spec: dict[str, dict]) -> dict[str, Question]:
    if not isinstance(spec, dict):
        raise ValueError("Questions spec must be a dict")
    return {name: question_from_spec(qspec) for name, qspec in spec.items()}


def question_to_dict(q: Question) -> dict:
    if isinstance(q, Choice):
        return {"type": "choice", "instructions": q.instructions, "criteria": q.criteria}
    elif isinstance(q, Noul):
        return {"type": "noul", "instructions": q.instructions, "criteria": q.criteria}
    elif isinstance(q, Score):
        return {"type": "score", "instructions": q.instructions, "criteria": q.criteria}
    raise ValueError(f"Unknown question type: {type(q)}")


def questions_version(qs: dict[str, Question]) -> str:
    from hermes_loopguard.fingerprint import canonical

    data = {name: question_to_dict(qs[name]) for name in sorted(qs)}
    digest = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()[:12]
    return f"q-{digest}"


def answer_to_dict(a: Answer) -> dict:
    if isinstance(a, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": a.choice,
            "confidence": a.confidence,
            "probabilities": a.probabilities,
        }
    elif isinstance(a, NoulAnswer):
        return {
            "type": "noul",
            "noul": a.noul,
        }
    elif isinstance(a, ScoreAnswer):
        return {
            "type": "score",
            "score": a.score,
            "confidence": a.confidence,
            "probabilities": {str(k): v for k, v in a.probabilities.items()},
            "legend": {str(k): v for k, v in a.legend.items()},
        }
    raise ValueError(f"Unknown answer type: {type(a)}")


def answer_from_dict(d: dict) -> Answer:
    atype = d.get("type")
    if atype == "choice":
        return ChoiceAnswer(
            choice=str(d["choice"]),
            confidence=float(d["confidence"]),
            probabilities={str(k): float(v) for k, v in d.get("probabilities", {}).items()},
        )
    elif atype == "noul":
        return NoulAnswer(noul=float(d["noul"]))
    elif atype == "score":
        return ScoreAnswer(
            score=float(d["score"]),
            confidence=float(d["confidence"]),
            probabilities={int(k): float(v) for k, v in d.get("probabilities", {}).items()},
            legend={int(k): str(v) for k, v in d.get("legend", {}).items()},
        )
    raise ValueError(f"Unknown answer type: {atype}")
