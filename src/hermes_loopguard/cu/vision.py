"""VisionGate: cheap Jev vision checks for computer use.

Every screenshot decision (did the click land? where is the button? is this
a payment dialog?) runs through a typed Jev request first; bigger models in
the fallback chain only see the request when the cheaper ones fail.

Images are prepared once per request (`prepare_image`), placed in the
request exactly once under a fresh uuid reference, and referred to by that
reference from question instructions. Backend failures never raise out of
locate/verify/changed/safety: they degrade to a not-found / not-passed /
pause result with the error recorded in `VisionUsage.fallbacks`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from hermes_loopguard.backends.base import Usage as BackendUsage
from hermes_loopguard.backends.typesafe import TypeSafeBackend
from hermes_loopguard.cu import frames
from hermes_loopguard.cu.pricing import usd
from hermes_loopguard.questions import Answer, Box, BoxAnswer, Choice, Noul

# Explicit versioned ids only; aliases (jev-preview / jev-latest) move when a
# new Jev release ships and must never appear in config.
JEV_114 = "jev-1.14.0"
JEV_113 = "jev-1.13.0"
JEV_MODELS = (JEV_114, JEV_113)
DEFAULT_MODELS = [JEV_114, JEV_113, "openai:gemini-3.8-flash-high"]
DEFAULT_THRESHOLDS = {
    "presence": 0.6,
    "box_confidence": 0.5,
    "verify": 0.7,
    "safety": 0.2,
    # Stage-1 screen-level probs at or above this pause WITHOUT the stage-2
    # action-level check; the band [safety, safety_hard) gets the check.
    "safety_hard": 0.6,
}
DEFAULT_HASH = {"unchanged_max": 3, "changed_min": 12}

SAFETY_CATEGORIES = (
    "payment",
    "deletion",
    "send_or_publish",
    "account_or_security_change",
    "none",
)

# Stage 2 asks one Noul per category about the EXACT step the agent is about
# to take ("Would clicking 'Dark mode' in `<ref>` spend money?").
STAGE2_PHRASES = {
    "payment": "spend money",
    "deletion": "delete data",
    "account_or_security_change": "change a password, login, account or security setting",
    "send_or_publish": "send or publish something to other people",
}
_STAGE2_GERUND = {
    "click": "clicking",
    "type": "typing into",
    "key": "pressing",
    "scroll": "scrolling",
}


class VisionError(RuntimeError):
    """All models in the chain failed; `usage` carries the failure record."""

    def __init__(self, message: str, usage: "VisionUsage | None" = None) -> None:
        super().__init__(message)
        self.usage = usage


@dataclass
class VisionUsage:
    backend: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_s: float = 0.0
    fallbacks: list[str] = field(default_factory=list)


@dataclass
class LocateResult:
    found: bool
    presence: float
    confidence: float
    box: tuple[float, float, float, float] | None
    center: tuple[int, int] | None
    grounding: str
    usage: VisionUsage


@dataclass
class VerifyResult:
    passed: bool
    yes: float
    usage: VisionUsage


@dataclass
class ChangeResult:
    changed: bool | None
    distance: int
    path: str
    usage: VisionUsage | None


@dataclass
class SafetyResult:
    category: str
    probs: dict[str, float]
    confirm_dialog: float
    pause: bool
    usage: VisionUsage | None
    error: str | None = None


@dataclass
class SafetyActionResult:
    """Stage-2 action-level check: per-category Nouls about ONE step.

    `probs[c]` answers "would this exact action spend money / delete data /
    change a security setting / send or publish?" for category c.  Empty with
    `error` set when every model in the chain failed (policy pauses on that).
    """

    probs: dict[str, float]
    usage: VisionUsage | None
    error: str | None = None


class VisionGate:
    """Ask cheap typed vision questions about screenshots, cheapest model first."""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or {}
        self.models = list(self.cfg.get("models") or DEFAULT_MODELS)
        for m in self.models:
            if m.startswith("openai:"):
                continue
            if m not in JEV_MODELS:
                raise ValueError(
                    f"computer_use.models must list explicit versioned ids "
                    f"({' / '.join(JEV_MODELS)}) or 'openai:<id>', never aliases; got {m!r}"
                )
        self.thresholds = {**DEFAULT_THRESHOLDS, **(self.cfg.get("thresholds") or {})}
        self.hash_cfg = {**DEFAULT_HASH, **(self.cfg.get("hash") or {})}
        self.timeout_s = float(self.cfg.get("timeout_s", 30))

    # --- backend plumbing -------------------------------------------------
    def _typesafe(self) -> TypeSafeBackend:
        return TypeSafeBackend(
            {"typesafe": {**self.cfg.get("typesafe", {}), "timeout": self.timeout_s}}
        )

    def _openai(self):
        from hermes_loopguard.backends.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(
            {"openai": {**self.cfg.get("openai", {}), "timeout": self.timeout_s}}
        )

    def _backend_for(self, model: str):
        return self._openai() if model.startswith("openai:") else self._typesafe()

    @staticmethod
    def _explicit_id(model: str) -> str:
        return model.split(":", 1)[1] if model.startswith("openai:") else model

    @staticmethod
    def _mk_usage(
        backend: str, model: str, u: BackendUsage | None, latency_s: float, fallbacks: list[str]
    ) -> VisionUsage:
        in_tok = int(getattr(u, "input_tokens", 0) or 0)
        out_tok = int(getattr(u, "output_tokens", 0) or 0)
        return VisionUsage(
            backend=backend,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            usd=usd(model, in_tok, out_tok),
            latency_s=latency_s,
            fallbacks=fallbacks,
        )

    def _failed_usage(self, fallbacks: list[str]) -> VisionUsage:
        return VisionUsage(backend="", model="", fallbacks=list(fallbacks))

    # --- the one request primitive ----------------------------------------
    def ask(
        self,
        images: dict[str, bytes],
        state_text: str,
        questions: dict[str, Any],
    ) -> tuple[dict[str, Answer], VisionUsage]:
        """One model request with prepared images; walks the model chain.

        Raises VisionError when every model fails; the error's `usage`
        records the fallbacks tried so far.
        """
        has_box = any(isinstance(q, Box) for q in questions.values())
        fallbacks: list[str] = []
        last_error: Exception | None = None
        for model in self.models:
            if has_box and model != JEV_114 and not model.startswith("openai:"):
                # Box is a Jev 1.14 primitive; older Jev answers 400.
                fallbacks.append(f"{model}: skipped (Box questions require {JEV_114})")
                continue
            explicit = self._explicit_id(model)
            backend = self._backend_for(model)
            started = time.perf_counter()
            try:
                result = backend.ask(
                    state_text, questions, images=images or None, model=explicit
                )
            except Exception as e:  # backend errors fall to the next model
                fallbacks.append(f"{model}: {e}")
                last_error = e
                continue
            latency = time.perf_counter() - started
            usage = self._mk_usage(backend.name, explicit, result.usage, latency, fallbacks)
            return result.answers, usage
        raise VisionError(
            f"all vision models failed ({len(fallbacks)} tried); last: {last_error}",
            usage=self._failed_usage(fallbacks),
        )

    # --- image prep ---------------------------------------------------------
    def _prepare(self, png: bytes) -> tuple[bytes, int, int]:
        return frames.prepare_image(png)

    # --- gates --------------------------------------------------------------
    def locate(
        self,
        frame: Any,
        target: str,
        *,
        region: tuple[int, int, int, int] | None = None,
    ) -> LocateResult:
        orig_w = int(frame.width)
        orig_h = int(frame.height)
        x_off = y_off = 0
        crop_w, crop_h = orig_w, orig_h
        if region:
            png = frames.crop(frame.png, region[0], region[1], region[2], region[3])
            # Mirror crop()'s clamping so the offset math matches what was sent.
            x0 = max(0, min(int(region[0]), orig_w))
            y0 = max(0, min(int(region[1]), orig_h))
            x1 = max(0, min(int(region[2]), orig_w))
            y1 = max(0, min(int(region[3]), orig_h))
            x_off, y_off, crop_w, crop_h = x0, y0, x1 - x0, y1 - y0
        else:
            png = frame.png
        try:
            prepared, w, h = self._prepare(png)
        except (RuntimeError, ValueError) as e:
            return LocateResult(
                found=False,
                presence=0.0,
                confidence=0.0,
                box=None,
                center=None,
                grounding="none",
                usage=VisionUsage(fallbacks=[f"prepare_image: {e}"]),
            )
        ref = uuid4().hex
        questions = {
            "presence": Noul(instructions=f"Is there {target} in `{ref}`?"),
            "target": Box(instructions=f"{target} in `{ref}`"),
        }
        state = f"Screenshot `{ref}`."
        try:
            answers, usage = self.ask({ref: prepared}, state, questions)
        except VisionError as e:
            return LocateResult(
                found=False,
                presence=0.0,
                confidence=0.0,
                box=None,
                center=None,
                grounding="none",
                usage=e.usage or self._failed_usage([str(e)]),
            )

        presence = float(getattr(answers.get("presence"), "noul", 0.0))
        box_ans = answers.get("target")
        confidence = float(getattr(box_ans, "confidence", 0.0) or 0.0)
        found = (
            presence >= self.thresholds["presence"]
            and confidence >= self.thresholds["box_confidence"]
            and isinstance(box_ans, BoxAnswer)
        )
        grounding = "none"
        box_fractions: tuple[float, float, float, float] | None = None
        center: tuple[int, int] | None = None
        if isinstance(box_ans, BoxAnswer):
            box_fractions = (box_ans.left, box_ans.top, box_ans.right, box_ans.bottom)
            # Fractions are relative to the image the model saw and survive
            # downscaling, so map them against the ORIGINAL frame (or crop)
            # pixel dims, not the prepared ones.
            grounding = "box" if usage.backend == "typesafe" else "fallback"
            if found:
                cx = x_off + (box_ans.left + box_ans.right) / 2.0 * crop_w
                cy = y_off + (box_ans.top + box_ans.bottom) / 2.0 * crop_h
                center = (int(round(cx)), int(round(cy)))
        return LocateResult(
            found=found,
            presence=presence,
            confidence=confidence,
            box=box_fractions,
            center=center,
            grounding=grounding,
            usage=usage,
        )

    def verify(self, after: Any, expected: str, *, before: Any | None = None) -> VerifyResult:
        try:
            after_ref = uuid4().hex
            images = {after_ref: self._prepare(after.png)[0]}
            state = f"Screenshot `{after_ref}`."
            if before is not None:
                before_ref = uuid4().hex
                images[before_ref] = self._prepare(before.png)[0]
                state = f"Before: `{before_ref}`. After: `{after_ref}`."
            questions = {"ok": Noul(instructions=f"Does `{after_ref}` show: {expected}?")}
            answers, usage = self.ask(images, state, questions)
        except VisionError as e:
            return VerifyResult(passed=False, yes=0.0, usage=e.usage or self._failed_usage([str(e)]))
        except (RuntimeError, ValueError) as e:  # image prep failed
            return VerifyResult(passed=False, yes=0.0, usage=VisionUsage(fallbacks=[f"prepare_image: {e}"]))
        yes = float(getattr(answers.get("ok"), "noul", 0.0))
        return VerifyResult(passed=yes >= self.thresholds["verify"], yes=yes, usage=usage)

    def changed(self, before: Any, after: Any) -> ChangeResult:
        try:
            distance = frames.hamming(frames.dhash(before.png), frames.dhash(after.png))
        except (RuntimeError, ValueError) as e:
            distance = -1  # hashing unavailable: treat as ambiguous, ask the model
        if 0 <= distance <= self.hash_cfg["unchanged_max"]:
            return ChangeResult(changed=False, distance=distance, path="hash", usage=None)
        if distance >= self.hash_cfg["changed_min"]:
            return ChangeResult(changed=True, distance=distance, path="hash", usage=None)
        try:
            before_ref = uuid4().hex
            after_ref = uuid4().hex
            images = {
                before_ref: self._prepare(before.png)[0],
                after_ref: self._prepare(after.png)[0],
            }
            questions = {
                "changed": Noul(
                    instructions=(
                        f"Did anything meaningful change between `{before_ref}` and "
                        f"`{after_ref}`, ignoring cursor blink and clocks?"
                    )
                )
            }
            answers, usage = self.ask(images, f"Before: `{before_ref}`. After: `{after_ref}`.", questions)
        except VisionError as e:
            return ChangeResult(changed=None, distance=distance, path="vision", usage=e.usage)
        yes = float(getattr(answers.get("changed"), "noul", 0.0))
        return ChangeResult(changed=yes >= 0.5, distance=distance, path="vision", usage=usage)

    def safety(self, frame: Any) -> SafetyResult:
        try:
            ref = uuid4().hex
            prepared, _w, _h = self._prepare(frame.png)
            questions = {
                "category": Choice(
                    instructions=(
                        "A computer agent is about to act on this screen. Which category best "
                        f"describes the RISK LEVEL of this screen for `{ref}`: would acting here "
                        "spend money, delete data, send or publish content for others to see, "
                        "change account or security settings, or is it safe (`none`)? A plain "
                        "data-entry form that only fills in local fields is `none`."
                    ),
                    criteria={c: None for c in SAFETY_CATEGORIES},
                ),
                "confirm": Noul(instructions=f"Is a confirmation dialog visible in `{ref}`?"),
            }
            answers, usage = self.ask({ref: prepared}, f"Screenshot `{ref}`.", questions)
        except VisionError as e:
            return SafetyResult(
                category="none",
                probs={},
                confirm_dialog=0.0,
                pause=True,
                usage=e.usage or self._failed_usage([str(e)]),
                error=str(e),
            )
        except (RuntimeError, ValueError) as e:  # image prep failed
            return SafetyResult(
                category="none",
                probs={},
                confirm_dialog=0.0,
                pause=True,
                usage=VisionUsage(fallbacks=[f"prepare_image: {e}"]),
                error=str(e),
            )
        choice = answers.get("category")
        probs = dict(getattr(choice, "probabilities", {}) or {})
        confirm = float(getattr(answers.get("confirm"), "noul", 0.0))
        risky = [p for c, p in probs.items() if c != "none"]
        pause = any(p >= self.thresholds["safety"] for p in risky)
        category = max(probs, key=lambda c: probs[c]) if probs else "none"
        return SafetyResult(
            category=category,
            probs=probs,
            confirm_dialog=confirm,
            pause=pause,
            usage=usage,
        )

    def safety_action(self, frame: Any, action: str, target: str) -> SafetyActionResult:
        """Stage 2: ONE action-level request about the exact step about to run.

        Runs only when stage 1 left a risky category in the ambiguous band
        [thresholds.safety, thresholds.safety_hard).  Asks one Noul per risky
        category ("Would clicking 'Dark mode' in `<ref>` spend money?") on the
        SAME screenshot; the caller's policy pauses when the Noul for the
        ambiguous category crosses thresholds.safety.
        """
        try:
            ref = uuid4().hex
            prepared, _w, _h = self._prepare(frame.png)
            doing = _STAGE2_GERUND.get(
                str(action or "").strip().lower(), "acting on"
            )
            label = str(target or "").strip() or "this element"
            questions = {
                category: Noul(
                    instructions=f"Would {doing} '{label}' in `{ref}` {phrase}?"
                )
                for category, phrase in STAGE2_PHRASES.items()
            }
            answers, usage = self.ask({ref: prepared}, f"Screenshot `{ref}`.", questions)
        except VisionError as e:
            return SafetyActionResult(
                probs={}, usage=e.usage or self._failed_usage([str(e)]), error=str(e)
            )
        except (RuntimeError, ValueError) as e:  # image prep failed
            return SafetyActionResult(
                probs={},
                usage=VisionUsage(fallbacks=[f"prepare_image: {e}"]),
                error=str(e),
            )
        probs = {
            category: float(getattr(answers.get(category), "noul", 0.0))
            for category in STAGE2_PHRASES
        }
        return SafetyActionResult(probs=probs, usage=usage)
