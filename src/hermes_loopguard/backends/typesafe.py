"""TypeSafe backend: the real typesafe-sdk System One surface (Jev).

One `client.system_one(state, questions)` call per item answers every
question; answers come back in `response.answers` keyed by question name,
discriminated by their `type` field.
"""
from __future__ import annotations

from typing import Any

from hermes_loopguard import paths
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.questions import (
    Answer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)


class TypeSafeBackend:
    name = "typesafe"

    def __init__(self, cfg: dict | None = None) -> None:
        self.global_cfg = cfg or {}
        self.cfg = self.global_cfg.get("typesafe", {})

    def available(self) -> bool:
        try:
            import typesafe_sdk  # noqa: F401

        except ImportError:
            return False
        key_env = self.cfg.get("api_key_env", "TYPESAFE_API_KEY")
        return bool(paths.get_secret(key_env))

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult:
        try:
            import typesafe_sdk
        except ImportError as e:
            raise BackendError("typesafe-sdk is not installed") from e

        key_env = self.cfg.get("api_key_env", "TYPESAFE_API_KEY")
        secret = paths.get_secret(key_env)
        if not secret:
            raise BackendError(f"Missing API key for typesafe (env: {key_env})")

        sdk_qs: dict[str, Any] = {}
        for name, q in questions.items():
            if isinstance(q, Choice):
                sdk_qs[name] = typesafe_sdk.Choice(instructions=q.instructions, criteria=q.criteria)
            elif isinstance(q, Noul):
                if q.criteria:
                    sdk_qs[name] = typesafe_sdk.Noul(instructions=q.instructions, criteria=q.criteria)
                else:
                    sdk_qs[name] = typesafe_sdk.Noul(instructions=q.instructions)
            elif isinstance(q, Score):
                sdk_qs[name] = typesafe_sdk.Score(instructions=q.instructions, criteria=q.criteria)
            else:
                raise BackendError(f"Unsupported question type for '{name}': {type(q).__name__}")

        model = self.cfg.get("model") or None
        timeout = float(self.cfg.get("timeout", 30))
        try:
            with typesafe_sdk.TypeSafeClient(api_key=secret, model=model, timeout=timeout) as client:
                response = client.system_one(state=state, questions=sdk_qs)
        except typesafe_sdk.TypeSafeError as e:
            raise BackendError(f"TypeSafe System One call failed: {e}") from e
        except Exception as e:
            raise BackendError(f"TypeSafeClient call failed: {e}") from e

        answers = self._map_answers(getattr(response, "answers", {}) or {}, questions)
        usage_obj = getattr(response, "usage", None)
        in_tok = int(getattr(usage_obj, "input_tokens", None) or 0)
        out_tok = int(getattr(usage_obj, "output_tokens", None) or 0)
        in_cost = (in_tok * float(self.cfg.get("usd_per_mtok_in", 0.0))) / 1e6
        out_cost = (out_tok * float(self.cfg.get("usd_per_mtok_out", 0.0))) / 1e6
        usage = Usage(input_tokens=in_tok, output_tokens=out_tok, cost_usd=in_cost + out_cost)
        model_name = str(getattr(response, "model", "") or model or "typesafe")

        return GateResult(
            answers=answers,
            backend=self.name,
            model=model_name,
            usage=usage,
            raw=response,
        )

    def _map_answers(
        self, sdk_answers: dict[str, Any], questions: dict[str, Question]
    ) -> dict[str, Answer]:
        answers: dict[str, Answer] = {}
        for name, ans in sdk_answers.items():
            atype = getattr(ans, "type", None)
            if atype == "choice":
                answers[name] = ChoiceAnswer(
                    choice=str(ans.choice),
                    confidence=float(ans.confidence),
                    probabilities={str(k): float(v) for k, v in ans.probabilities.items()},
                )
            elif atype == "noul":
                answers[name] = NoulAnswer(noul=float(ans.noul))
            elif atype == "score":
                legend = {int(k): str(v) for k, v in (ans.legend or {}).items()}
                if not legend and isinstance(questions.get(name), Score):
                    legend = {i: lvl for i, lvl in enumerate(questions[name].criteria)}
                answers[name] = ScoreAnswer(
                    score=float(ans.score),
                    confidence=float(ans.confidence),
                    probabilities={int(k): float(v) for k, v in ans.probabilities.items()},
                    legend=legend,
                )
            else:
                raise BackendError(f"Unknown TypeSafe answer type for '{name}': {atype!r}")
        return answers
