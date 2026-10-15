from __future__ import annotations

from typing import Any

from hermes_loopguard.backends.base import GateResult, Usage
from hermes_loopguard.fingerprint import canonical
from hermes_loopguard.questions import (
    Answer,
    Choice,
    Noul,
    Question,
    Score,
    choice_answer,
    normalize,
    NoulAnswer,
    ScoreAnswer,
)


class LocalBackend:
    name = "local"
    model = "heuristic-v1"

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}

    def available(self) -> bool:
        return True

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult:
        if isinstance(state, (dict, list)):
            state_text = canonical(state).lower()
        else:
            state_text = str(state).lower()

        local_cfg = self.cfg.get("local", {})
        kw_config = local_cfg.get("keywords", {})

        answers: dict[str, Answer] = {}

        for name, q in questions.items():
            if isinstance(q, Choice):
                cat_hits: dict[str, int] = {}
                total_hits = 0
                for cat in q.criteria:
                    kws = list(kw_config.get(cat, []))
                    kws.extend(cat.replace("_", " ").replace("-", " ").split())
                    crit_text = q.criteria.get(cat)
                    if crit_text:
                        kws.extend(crit_text.split())
                    hits = sum(state_text.count(kw.lower()) for kw in kws if kw.strip())
                    cat_hits[cat] = hits
                    total_hits += hits

                if total_hits == 0:
                    n = len(q.criteria)
                    probs = {cat: 1.0 / n for cat in q.criteria} if n > 0 else {}
                else:
                    raw_scores = {cat: float(hits) + 0.5 for cat, hits in cat_hits.items()}
                    probs = normalize(raw_scores)
                answers[name] = choice_answer(probs)

            elif isinstance(q, Noul):
                noul_kws = kw_config.get(name, {})
                if isinstance(noul_kws, dict) and (noul_kws.get("yes") or noul_kws.get("no")):
                    yes_list = noul_kws.get("yes", [])
                    no_list = noul_kws.get("no", [])
                    yes_hits = sum(state_text.count(kw.lower()) for kw in yes_list if kw.strip())
                    no_hits = sum(state_text.count(kw.lower()) for kw in no_list if kw.strip())
                    p = (yes_hits + 0.5) / (yes_hits + no_hits + 1.0)
                else:
                    p = 0.5
                answers[name] = NoulAnswer(noul=p)

            elif isinstance(q, Score):
                n_levels = len(q.criteria) if q.criteria else 1
                probs = {i: 1.0 / n_levels for i in range(n_levels)}
                legend = {i: lvl for i, lvl in enumerate(q.criteria)}
                answers[name] = ScoreAnswer(
                    score=0.0,
                    confidence=1.0 / n_levels,
                    probabilities=probs,
                    legend=legend,
                )

        return GateResult(
            answers=answers,
            backend=self.name,
            model=self.model,
            usage=Usage(input_tokens=0, output_tokens=0, cost_usd=0.0),
        )
