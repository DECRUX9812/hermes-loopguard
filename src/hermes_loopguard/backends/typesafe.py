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

            key_env = self.cfg.get("api_key_env", "TYPESAFE_API_KEY")
            return bool(paths.get_secret(key_env))
        except ImportError:
            return False

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult:
        try:
            import typesafe_sdk
        except ImportError as e:
            raise BackendError("typesafe-sdk is not installed") from e

        key_env = self.cfg.get("api_key_env", "TYPESAFE_API_KEY")
        secret = paths.get_secret(key_env)
        if not secret:
            raise BackendError(f"Missing API key for typesafe (env: {key_env})")

        sdk_qs = {}
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

        try:
            client = typesafe_sdk.TypeSafeClient(api_key=secret)
        except TypeError:
            client = typesafe_sdk.TypeSafeClient()

        timeout = float(self.cfg.get("timeout", 30))
        model_arg = self.cfg.get("model") or None

        try:
            with client as c:
                r = c.system_one(state=state, questions=sdk_qs, model=model_arg, timeout=timeout)
        except Exception as e:
            raise BackendError(f"TypeSafeClient call failed: {e}") from e

        answers: dict[str, Answer] = {}
        for name, ans in getattr(r, "answers", {}).items():
            atype = getattr(ans, "type", "choice")
            if atype == "choice":
                answers[name] = ChoiceAnswer(
                    choice=str(ans.choice),
                    confidence=float(ans.confidence),
                    probabilities={str(k): float(v) for k, v in ans.probabilities.items()},
                )
            elif atype == "noul":
                answers[name] = NoulAnswer(noul=float(ans.noul))
            elif atype == "score":
                legend = getattr(ans, "legend", None)
                if legend is None and isinstance(questions.get(name), Score):
                    legend = {i: lvl for i, lvl in enumerate(questions[name].criteria)}
                answers[name] = ScoreAnswer(
                    score=float(ans.score),
                    confidence=float(ans.confidence),
                    probabilities={int(k): float(v) for k, v in ans.probabilities.items()},
                    legend={int(k): str(v) for k, v in (legend or {}).items()},
                )

        usage_obj = getattr(r, "usage", None)
        in_tok = int(getattr(usage_obj, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage_obj, "output_tokens", 0) or 0)
        in_cost = (in_tok * float(self.cfg.get("usd_per_mtok_in", 0.0))) / 1e6
        out_cost = (out_tok * float(self.cfg.get("usd_per_mtok_out", 0.0))) / 1e6
        usage = Usage(input_tokens=in_tok, output_tokens=out_tok, cost_usd=in_cost + out_cost)
        model_name = getattr(r, "model", "typesafe")

        return GateResult(
            answers=answers,
            backend=self.name,
            model=model_name,
            usage=usage,
            raw=r,
        )
