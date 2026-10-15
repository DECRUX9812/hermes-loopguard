"""TypeSafe backend: the real typesafe-sdk System One surface (Jev).

One `client.system_one(state, questions)` call per item answers every
question; answers come back in `response.answers` keyed by question name,
discriminated by their `type` field.

Vision/computer-use extension: pass `images` (ref -> PNG bytes) to attach
screenshots as assets, and use `Box` questions (Jev 1.14 only) for
grounding. Every request that carries an image or a Box question needs the
`TypeSafe-Danger-Zone-Images: true` header. Explicit versioned model ids
only (`jev-1.14.0`, `jev-1.13.0`); aliases are rejected.
"""
from __future__ import annotations

from typing import Any

from hermes_loopguard import paths
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.questions import (
    Answer,
    Box,
    BoxAnswer,
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
)

DANGER_ZONE_IMAGES_HEADER = "TypeSafe-Danger-Zone-Images"

# Aliases move when a new Jev release ships; config must pin versioned ids.
_JEV_ALIASES = {"jev-preview", "jev-latest"}


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

    def ask(
        self,
        state: str | dict,
        questions: dict[str, Question],
        *,
        images: dict[str, bytes] | None = None,
        model: str | None = None,
    ) -> GateResult:
        try:
            import typesafe_sdk
        except ImportError as e:
            raise BackendError("typesafe-sdk is not installed") from e

        key_env = self.cfg.get("api_key_env", "TYPESAFE_API_KEY")
        secret = paths.get_secret(key_env)
        if not secret:
            raise BackendError(f"Missing API key for typesafe (env: {key_env})")

        resolved_model = model or self.cfg.get("model") or None
        if model and model in _JEV_ALIASES:
            raise BackendError(
                f"Refusing alias model id {model!r}: pin an explicit versioned id "
                "(jev-1.14.0 or jev-1.13.0) so alias moves cannot change behavior silently"
            )

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
            elif isinstance(q, Box):
                sdk_box = getattr(typesafe_sdk, "Box", None)
                if sdk_box is None:
                    raise BackendError(
                        f"typesafe-sdk has no Box support; question '{name}' needs typesafe-sdk >= 0.7.5"
                    )
                sdk_qs[name] = sdk_box(instructions=q.instructions)
            else:
                raise BackendError(f"Unsupported question type for '{name}': {type(q).__name__}")

        sdk_assets: dict[str, Any] | None = None
        extra_headers: dict[str, str] | None = None
        if images or any(isinstance(q, Box) for q in questions.values()):
            # The API rejects image/Box requests without the danger-zone header.
            extra_headers = {DANGER_ZONE_IMAGES_HEADER: "true"}
        if images:
            sdk_image = getattr(typesafe_sdk, "Image", None)
            if sdk_image is None:
                raise BackendError(
                    "typesafe-sdk has no Image support; images need typesafe-sdk >= 0.7.5"
                )
            # Image accepts raw bytes (Buffer protocol); no temp file needed.
            sdk_assets = {ref: sdk_image(png) for ref, png in images.items()}

        timeout = float(self.cfg.get("timeout", 30))
        # Legacy shape first: system_one(state, questions) with the client-level
        # default model, so text-only calls behave exactly as before (and fake
        # SDK doubles that only accept two kwargs keep working). New kwargs are
        # added only when set.
        call_kwargs: dict[str, Any] = {}
        if model:
            call_kwargs["model"] = model
        if sdk_assets:
            call_kwargs["assets"] = sdk_assets
        if extra_headers:
            call_kwargs["extra_headers"] = extra_headers
        try:
            with typesafe_sdk.TypeSafeClient(
                api_key=secret, model=None if model else resolved_model, timeout=timeout
            ) as client:
                response = client.system_one(state=state, questions=sdk_qs, **call_kwargs)
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
        model_name = str(getattr(response, "model", "") or resolved_model or "typesafe")

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
            elif atype == "box":
                box = getattr(ans, "box", None)
                if box is None:
                    raise BackendError(f"Box answer for '{name}' has no box coordinates")
                answers[name] = BoxAnswer(
                    left=float(box.left),
                    top=float(box.top),
                    right=float(box.right),
                    bottom=float(box.bottom),
                    confidence=float(getattr(ans, "confidence", None) or 0.0),
                )
            else:
                raise BackendError(f"Unknown TypeSafe answer type for '{name}': {atype!r}")
        return answers
