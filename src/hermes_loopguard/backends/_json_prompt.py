from __future__ import annotations

import json
from typing import Any

from hermes_loopguard.backends.base import BackendError
from hermes_loopguard.questions import (
    Answer,
    Choice,
    Noul,
    Question,
    Score,
    choice_answer,
    NoulAnswer,
    score_answer,
)


def build_prompt(questions: dict[str, Question]) -> str:
    lines = [
        "You are evaluating the provided item state against a set of questions.",
        "Output ONLY a JSON object with one key per question.",
        "Format for each question type:",
        '- Choice: {"<qname>": {"probabilities": {"<category>": float, ...}}} (probabilities should sum to 1.0)',
        '- Noul: {"<qname>": {"yes": float}} (a float between 0.0 and 1.0 indicating probability of true/yes)',
        '- Score: {"<qname>": {"probabilities": {"0": float, "1": float, ...}}} (probabilities across levels)',
        "",
        "Questions to evaluate:",
    ]

    for name, q in questions.items():
        if isinstance(q, Choice):
            lines.append(f"\nQuestion '{name}' [Choice]: {q.instructions}")
            lines.append("Categories and criteria:")
            for cat, crit in q.criteria.items():
                crit_text = f": {crit}" if crit else ""
                lines.append(f"  - {cat}{crit_text}")
        elif isinstance(q, Noul):
            lines.append(f"\nQuestion '{name}' [Noul]: {q.instructions}")
            if q.criteria:
                lines.append(f"  Criteria: true={q.criteria.get('true', '')}, false={q.criteria.get('false', '')}")
        elif isinstance(q, Score):
            lines.append(f"\nQuestion '{name}' [Score]: {q.instructions}")
            lines.append("Levels:")
            for idx, level in enumerate(q.criteria):
                lines.append(f"  {idx}: {level}")

    lines.append("\nRespond with ONLY valid JSON containing all specified question keys.")
    return "\n".join(lines)


def build_schema(questions: dict[str, Question]) -> dict:
    props = {}
    for name, q in questions.items():
        if isinstance(q, Choice):
            cat_props = {cat: {"type": "number"} for cat in q.criteria}
            props[name] = {
                "type": "object",
                "properties": {
                    "probabilities": {
                        "type": "object",
                        "properties": cat_props,
                        "required": list(q.criteria.keys()),
                        "additionalProperties": False,
                    }
                },
                "required": ["probabilities"],
                "additionalProperties": False,
            }
        elif isinstance(q, Noul):
            props[name] = {
                "type": "object",
                "properties": {
                    "yes": {"type": "number"},
                },
                "required": ["yes"],
                "additionalProperties": False,
            }
        elif isinstance(q, Score):
            lvl_props = {str(i): {"type": "number"} for i in range(len(q.criteria))}
            props[name] = {
                "type": "object",
                "properties": {
                    "probabilities": {
                        "type": "object",
                        "properties": lvl_props,
                        "required": [str(i) for i in range(len(q.criteria))],
                        "additionalProperties": False,
                    }
                },
                "required": ["probabilities"],
                "additionalProperties": False,
            }
    return {
        "type": "object",
        "properties": props,
        "required": list(questions.keys()),
        "additionalProperties": False,
    }


def parse_answers(obj: dict, questions: dict[str, Question]) -> dict[str, Answer]:
    answers: dict[str, Answer] = {}
    for name, q in questions.items():
        if name not in obj:
            raise BackendError(f"Missing question '{name}' in reply")
        item = obj[name]
        if not isinstance(item, dict):
            raise BackendError(f"Expected dict for question '{name}', got {type(item).__name__}")

        if isinstance(q, Choice):
            probs = item.get("probabilities")
            if not isinstance(probs, dict):
                raise BackendError(f"Choice question '{name}' missing 'probabilities' dict")
            answers[name] = choice_answer({str(k): float(v) for k, v in probs.items()})
        elif isinstance(q, Noul):
            val = item.get("yes")
            if val is None:
                val = item.get("probability", item.get("p", 0.5))
            try:
                p = max(0.0, min(1.0, float(val)))
            except (ValueError, TypeError) as e:
                raise BackendError(f"Invalid probability value for noul question '{name}': {val}") from e
            answers[name] = NoulAnswer(noul=p)
        elif isinstance(q, Score):
            probs = item.get("probabilities")
            if not isinstance(probs, dict):
                raise BackendError(f"Score question '{name}' missing 'probabilities' dict")
            answers[name] = score_answer({int(k): float(v) for k, v in probs.items()}, q.criteria)
    return answers
