"""Unit tests for the cu vision primitives.

All network-touching tests use FAKE transports: either a fake typesafe_sdk
module, a fake OpenAICompatBackend._post, or a ReplayBackend that replays
REAL recorded responses from tests/fixtures/cu/vision/cases/ (see the
fixtures README). No test here talks to the network.
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.cu.pricing import usd
from hermes_loopguard.cu.vision import (
    ChangeResult,
    LocateResult,
    SafetyResult,
    VisionGate,
    VisionUsage,
    VerifyResult,
)
from hermes_loopguard.questions import (
    Box,
    BoxAnswer,
    Choice,
    Noul,
    answer_from_dict,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cu" / "vision"
CASES = FIXTURES / "cases"


def load_case(name: str) -> dict:
    data = json.loads((CASES / f"{name}.json").read_text())
    assert data.get("recorded") is True, "fixture must be marked as REAL recorded"
    return data


def load_png(name: str) -> bytes:
    return (FIXTURES / f"{name}.png").read_bytes()


class _Frame:
    """Duck-typed stand-in for P2's Frame (png/width/height is all VisionGate uses)."""

    def __init__(self, png: bytes, w: int, h: int):
        self.png, self.width, self.height, self.ts = png, w, h, 0.0


def _fixture_frame(name: str, w: int = 1280, h: int = 800) -> _Frame:
    return _Frame(load_png(name), w, h)


# --- fake typesafe_sdk module ---------------------------------------------------


class _FakeTypeSafeError(Exception):
    pass


class _FakeBoxQuestion:
    type = "box"

    def __init__(self, instructions=None):
        self.instructions = instructions


class _FakeImage:
    def __init__(self, data):
        self.data = bytes(data)


class _FakeBoxCoordinates:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _FakeBoxAnswer:
    type = "box"

    def __init__(self, left, top, right, bottom, confidence=None):
        self.box = _FakeBoxCoordinates(left, top, right, bottom)
        self.confidence = confidence


class _FakeNoulAnswer:
    type = "noul"

    def __init__(self, noul):
        self.noul = noul


class _FakeChoiceAnswer:
    type = "choice"

    def __init__(self, choice, confidence, probabilities):
        self.choice, self.confidence, self.probabilities = choice, confidence, probabilities


class _FakeUsage:
    def __init__(self, input_tokens=None, output_tokens=None):
        self.input_tokens, self.output_tokens = input_tokens, output_tokens


class _FakeResponse:
    def __init__(self, model, usage, answers):
        self.model, self.usage, self.answers = model, usage, answers


class _CapturingClient:
    def __init__(self, registry, response, error, **init_kwargs):
        self.response, self.error, self.init_kwargs = response, error, init_kwargs
        self.calls: list[dict] = []
        registry.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def system_one(self, state, questions, **kwargs):
        self.calls.append({"state": state, "questions": questions, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class _FakeChoice:
    type = "choice"

    def __init__(self, instructions=None, criteria=None):
        self.instructions, self.criteria = instructions, criteria


class _FakeScore:
    type = "score"

    def __init__(self, instructions=None, criteria=None):
        self.instructions, self.criteria = instructions, criteria


def install_fake_typesafe_sdk(monkeypatch, response=None, error=None, clients=None):
    module = types.ModuleType("typesafe_sdk")
    module.Choice = _FakeChoice
    module.Noul = _FakeBoxQuestion  # placeholder, replaced below
    module.Score = _FakeScore
    module.Box = _FakeBoxQuestion
    module.Image = _FakeImage
    module.TypeSafeError = _FakeTypeSafeError

    class _FakeNoul:
        type = "noul"

        def __init__(self, instructions=None, criteria=None):
            self.instructions, self.criteria = instructions, criteria

    module.Noul = _FakeNoul

    def _factory(**kwargs):
        return _CapturingClient(clients, response, error, **kwargs)

    module.TypeSafeClient = _factory
    monkeypatch.setitem(sys.modules, "typesafe_sdk", module)


# --- fake transport replaying recorded fixtures ---------------------------------


class ReplayBackend:
    """FAKE TRANSPORT (unit test only): replays one recorded REAL case."""

    def __init__(self, case: dict, fail_for: set[str] | None = None):
        self.case = case
        self.fail_for = fail_for or set()
        self.backend = case["backend"]
        self.name = case["backend"]
        self.calls: list[dict] = []

    def ask(self, state, questions, *, images=None, model=None):
        self.calls.append({"state": state, "questions": questions, "images": images, "model": model})
        if model in self.fail_for or f"{self.backend}:{model}" in self.fail_for:
            raise BackendError(f"{model}: 400 replayed failure")
        answers = {k: answer_from_dict(v) for k, v in self.case["answers"].items() if v}
        return GateResult(
            answers=answers,
            backend=self.case["backend"],
            model=self.case["model"],
            usage=Usage(
                input_tokens=self.case["usage"]["input_tokens"],
                output_tokens=self.case["usage"]["output_tokens"],
            ),
            raw=None,
        )


def gate_with_backends(monkeypatch, backends: dict):
    """VisionGate whose `_backend_for` returns the given fake backends by model."""
    gate = VisionGate()
    monkeypatch.setattr(gate, "_backend_for", lambda model: backends[model])
    return gate


# --- typesafe backend: danger-zone header, images, Box answers ------------------


def test_typesafe_header_on_image_request_absent_on_text_only(monkeypatch):
    clients: list[_CapturingClient] = []
    response = _FakeResponse(
        "jev-1.14.0",
        _FakeUsage(10, 2),
        {"ok": _FakeNoulAnswer(0.9)},
    )
    install_fake_typesafe_sdk(monkeypatch, response=response, clients=clients)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    from hermes_loopguard.backends.typesafe import TypeSafeBackend

    backend = TypeSafeBackend({})
    qs = {"ok": Noul(instructions="yes or no")}

    # Text-only: no danger-zone header, no assets.
    backend.ask({"a": 1}, qs)
    assert clients[-1].calls[0].get("extra_headers") is None
    assert clients[-1].calls[0].get("assets") is None

    # With an image: header present on the same request shape.
    backend.ask("Screenshot `<ref>`.", qs, images={"ref": b"\x89PNG-fake"})
    call = clients[-1].calls[0]
    assert call["extra_headers"] == {"TypeSafe-Danger-Zone-Images": "true"}
    assert isinstance(call["assets"]["ref"], _FakeImage)


def test_typesafe_header_on_box_question_without_images(monkeypatch):
    clients: list[_CapturingClient] = []
    install_fake_typesafe_sdk(
        monkeypatch,
        response=_FakeResponse("jev-1.14.0", _FakeUsage(5, 1), {}),
        clients=clients,
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    from hermes_loopguard.backends.typesafe import TypeSafeBackend

    backend = TypeSafeBackend({})
    backend.ask("state", {"t": Box(instructions="the Submit button")})
    call = clients[-1].calls[0]
    assert call["extra_headers"] == {"TypeSafe-Danger-Zone-Images": "true"}
    assert isinstance(call["questions"]["t"], _FakeBoxQuestion)


def test_typesafe_box_answer_mapping_and_explicit_model(monkeypatch):
    clients: list[_CapturingClient] = []
    install_fake_typesafe_sdk(
        monkeypatch,
        response=_FakeResponse(
            "jev-1.14.0",
            _FakeUsage(100, 5),
            {"t": _FakeBoxAnswer(0.44, 0.25, 0.84, 0.82, confidence=0.92)},
        ),
        clients=clients,
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    from hermes_loopguard.backends.typesafe import TypeSafeBackend

    backend = TypeSafeBackend({})
    res = backend.ask(
        "Screenshot `<ref>`.",
        {"t": Box(instructions="the Submit button in `<ref>`")},
        images={"ref": b"\x89PNG-fake"},
        model="jev-1.14.0",
    )
    call = clients[-1].calls[0]
    assert call["model"] == "jev-1.14.0"
    ans = res.answers["t"]
    assert isinstance(ans, BoxAnswer)
    assert (ans.left, ans.top, ans.right, ans.bottom) == (0.44, 0.25, 0.84, 0.82)
    assert ans.confidence == pytest.approx(0.92)


def test_typesafe_alias_model_rejected_on_explicit_ask(monkeypatch):
    clients: list[_CapturingClient] = []
    install_fake_typesafe_sdk(monkeypatch, clients=clients)
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key-for-tests")

    from hermes_loopguard.backends.typesafe import TypeSafeBackend

    backend = TypeSafeBackend({})
    for alias in ("jev-preview", "jev-latest"):
        with pytest.raises(BackendError, match="alias"):
            backend.ask("s", {"ok": Noul(instructions="x")}, model=alias)
    assert clients == []  # nothing was sent


# --- openai_compat: data-url images, box JSON prompt/parse -----------------------


def _oa_backend(**cfg) -> "OpenAICompatBackend":
    from hermes_loopguard.backends.openai_compat import OpenAICompatBackend

    return OpenAICompatBackend(
        {"openai": {"base_url": "http://mock-llm.local/v1", "model": "mock-vision",
                    "api_key_env": "OPENAI_API_KEY", **cfg}}
    )


def test_openai_images_sent_as_data_urls(monkeypatch):
    import base64

    backend = _oa_backend()
    captured: dict = {}

    def fake_post(payload):
        # FAKE BACKEND (unit test only)
        captured.update(payload)
        return {
            "choices": [{"message": {"content": json.dumps({"ok": {"yes": 0.9}})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")

    png = b"\x89PNG-fake-bytes"
    res = backend.ask("state", {"ok": Noul(instructions="Is it ok?")}, images={"s": png})
    assert res.answers["ok"].noul == pytest.approx(0.9)
    user_content = captured["messages"][1]["content"]
    assert isinstance(user_content, list)
    texts = [p["text"] for p in user_content if p["type"] == "text"]
    assert any("`s`" in t for t in texts)  # ref named in the prompt
    urls = [p["image_url"]["url"] for p in user_content if p["type"] == "image_url"]
    assert urls == ["data:image/png;base64," + base64.b64encode(png).decode()]


def test_openai_box_question_prompted_and_parsed(monkeypatch):
    backend = _oa_backend()
    captured: dict = {}

    def fake_post(payload):
        # FAKE BACKEND (unit test only)
        captured.update(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "ok": {"yes": 0.95},
                                "t": {"left": 0.5, "top": 0.25, "right": 0.75,
                                      "bottom": 0.5, "confidence": 0.8},
                            }
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4},
        }

    monkeypatch.setattr(backend, "_post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")

    res = backend.ask(
        "state",
        {"ok": Noul(instructions="Is there a Submit button?"),
         "t": Box(instructions="the Submit button in `s`")},
        images={"s": b"\x89PNG"},
    )
    assert "box" in captured["messages"][0]["content"]
    box = res.answers["t"]
    assert isinstance(box, BoxAnswer)
    assert box.left == 0.5 and box.confidence == pytest.approx(0.8)


# --- Box center math at several resolutions --------------------------------------

def box_case(fractions, confidence=0.92, presence=0.99):
    return {
        "recorded": True,
        "backend": "typesafe",
        "model": "jev-1.14.0",
        "answers": {
            "presence": {"type": "noul", "noul": presence},
            "target": {"type": "box", "left": fractions[0], "top": fractions[1],
                       "right": fractions[2], "bottom": fractions[3],
                       "confidence": confidence},
        },
        "usage": {"input_tokens": 100, "output_tokens": 2},
    }


SUBMIT_FRACTIONS = (0.44, 0.25, 0.84, 0.82)  # recorded ballpark for the form's Submit


@pytest.mark.parametrize(
    "w,h,expect",
    [
        (1280, 800, (int(round(0.64 * 1280)), int(round(0.535 * 800)))),
        (1920, 1080, (int(round(0.64 * 1920)), int(round(0.535 * 1080)))),
        (2560, 1440, (int(round(0.64 * 2560)), int(round(0.535 * 1440)))),
    ],
)
def test_box_center_pixel_math_at_resolutions(monkeypatch, w, h, expect):
    replay = ReplayBackend(box_case(SUBMIT_FRACTIONS))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    frame = _Frame(load_png("form"), w, h)
    r = gate.locate(frame, "the Submit button")
    assert r.found is True
    assert r.grounding == "box"
    assert r.center == expect


def test_box_center_with_region_offset(monkeypatch):
    replay = ReplayBackend(box_case((0.5, 0.5, 0.75, 0.75)))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    frame = _Frame(load_png("form"), 1280, 800)
    r = gate.locate(frame, "the Submit button", region=(100, 50, 900, 650))
    # crop is 800x600 at (100, 50); center of (0.5,0.5)-(0.75,0.75) fractions
    assert r.center == (100 + int(round(0.625 * 800)), 50 + int(round(0.625 * 600)))


def test_each_image_placed_once_and_referred_by_fresh_ref(monkeypatch):
    replay = ReplayBackend(box_case(SUBMIT_FRACTIONS))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    gate.locate(_fixture_frame("form"), "the Submit button")
    call = replay.calls[0]
    assert list(call["images"].keys()) == [list(call["images"].keys())[0]]  # exactly one image
    ref = next(iter(call["images"]))
    assert len(ref) == 32 and all(c in "0123456789abcdef" for c in ref)  # uuid4().hex
    # Referred to by ref from the questions, once per question.
    presence_q = call["questions"]["presence"]
    target_q = call["questions"]["target"]
    assert ref in presence_q.instructions and ref in target_q.instructions
    assert call["images"][ref].startswith(b"\x89PNG")


# --- presence gating ---------------------------------------------------------------


def test_high_confidence_box_ignored_when_presence_low(monkeypatch):
    replay = ReplayBackend(box_case(SUBMIT_FRACTIONS, presence=0.2))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.locate(_fixture_frame("form"), "the Submit button")
    assert r.presence == pytest.approx(0.2)
    assert r.confidence == pytest.approx(0.92)
    assert r.found is False
    assert r.center is None


def test_low_confidence_box_ignored(monkeypatch):
    replay = ReplayBackend(box_case(SUBMIT_FRACTIONS, confidence=0.3))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.locate(_fixture_frame("form"), "the Submit button")
    assert r.found is False


# --- fallback chain ------------------------------------------------------------------


def _noul_case(model="jev-1.14.0", backend="typesafe", noul=0.9, name="ok"):
    return {
        "recorded": True,
        "backend": backend,
        "model": model,
        "answers": {name: {"type": "noul", "noul": noul}},
        "usage": {"input_tokens": 50, "output_tokens": 1},
    }


class FailingBackend:
    """FAKE BACKEND (unit test only): always fails with the given error."""

    name = "typesafe"

    def __init__(self, error: str):
        self.error = error

    def ask(self, state, questions, *, images=None, model=None):
        raise BackendError(f"{model}: {self.error}")


def test_fallback_chain_on_400_then_success(monkeypatch):
    replay = ReplayBackend(_noul_case(model="jev-1.13.0"))
    gate = gate_with_backends(
        monkeypatch,
        {"jev-1.14.0": FailingBackend("400 Invalid request"), "jev-1.13.0": replay},
    )
    answers, usage = gate.ask({}, "state", {"ok": Noul(instructions="ok?")})
    assert answers["ok"].noul == pytest.approx(0.9)
    assert usage.model == "jev-1.13.0"
    assert usage.fallbacks == ["jev-1.14.0: jev-1.14.0: 400 Invalid request"]


def test_fallback_chain_on_413_and_timeout(monkeypatch):
    calls: list[str] = []

    class Recorder(FailingBackend):
        def ask(self, state, questions, *, images=None, model=None):
            calls.append(model)
            raise BackendError(f"{model}: {self.error}")

    openai_replay = ReplayBackend(_noul_case(model="gemini-3.8-flash-high", backend="openai"))
    gate = gate_with_backends(
        monkeypatch,
        {
            "jev-1.14.0": Recorder("413 payload too large"),
            "jev-1.13.0": Recorder("request timed out"),
            "openai:gemini-3.8-flash-high": openai_replay,
        },
    )
    answers, usage = gate.ask({}, "state", {"ok": Noul(instructions="ok?")})
    assert answers["ok"].noul == pytest.approx(0.9)
    assert calls == ["jev-1.14.0", "jev-1.13.0"]
    assert usage.model == "gemini-3.8-flash-high"
    assert usage.backend == "openai"
    assert len(usage.fallbacks) == 2


def test_box_questions_skip_non_114_jev_models(monkeypatch):
    openai_replay = ReplayBackend(load_case("locate_submit_openai_fallback"))
    seen: list[str] = []

    class Watcher(FailingBackend):
        def ask(self, state, questions, *, images=None, model=None):
            seen.append(model)
            raise BackendError(f"{model}: 400")

    gate = gate_with_backends(
        monkeypatch,
        {
            "jev-1.14.0": Watcher("400 Invalid request"),
            "jev-1.13.0": Watcher("would be 400"),
            "openai:gemini-3.8-flash-high": openai_replay,
        },
    )
    r = gate.locate(_fixture_frame("form"), "the Submit button")
    # jev-1.13.0 must be skipped without a call, not just failed.
    assert seen == ["jev-1.14.0"]
    assert any("jev-1.13.0: skipped" in f for f in r.usage.fallbacks)
    assert r.grounding == "fallback"
    assert r.found is True


def test_all_models_fail_records_fallbacks_never_raises(monkeypatch):
    gate = gate_with_backends(
        monkeypatch,
        {
            "jev-1.14.0": FailingBackend("400 bad"),
            "jev-1.13.0": FailingBackend("500 boom"),
            "openai:gemini-3.8-flash-high": FailingBackend("connection refused"),
        },
    )
    r = gate.locate(_fixture_frame("form"), "the Submit button")
    assert r.found is False
    assert r.grounding == "none"
    assert len(r.usage.fallbacks) == 3
    assert any("skipped" in f for f in r.usage.fallbacks)

    v = gate.verify(_fixture_frame("thanks"), "a message thanking Ada")
    assert v.passed is False
    # changed: real screenshots of different pages hash far apart, so the
    # free hash path answers without any model call.
    c = gate.changed(_fixture_frame("form"), _fixture_frame("payment"))
    assert c.changed is True and c.path == "hash"
    s = gate.safety(_fixture_frame("payment"))
    assert s.pause is True and s.error


def test_visiongate_rejects_alias_models():
    with pytest.raises(ValueError, match="aliases"):
        VisionGate({"models": ["jev-latest"]})
    with pytest.raises(ValueError, match="aliases"):
        VisionGate({"models": ["jev-preview"]})


# --- recorded fixture replays (REAL responses, fake transport) ----------------------


def test_replay_recorded_locate_submit_found():
    case = load_case("locate_submit_jev114")
    replay = ReplayBackend(case)
    g = VisionGate()
    g._backend_for = lambda model: replay  # type: ignore[method-assign]
    r = g.locate(_fixture_frame("form"), "the Submit button")
    assert r.found is True
    assert r.grounding == "box"
    assert r.presence >= 0.6 and r.confidence >= 0.5
    tgt = case["answers"]["target"]
    assert r.center == (
        int(round((tgt["left"] + tgt["right"]) / 2 * 1280)),
        int(round((tgt["top"] + tgt["bottom"]) / 2 * 800)),
    )


def test_replay_recorded_locate_absent_target():
    case = load_case("locate_delete_account_jev114")
    replay = ReplayBackend(case)
    g = VisionGate()
    g._backend_for = lambda model: replay  # type: ignore[method-assign]
    r = g.locate(_fixture_frame("form"), "the Delete account button")
    assert r.found is False  # presence 0.01 recorded from the real call


def test_replay_recorded_verify_pass_and_fail():
    pass_case = load_case("verify_thanks_pass_jev114")
    fail_case = load_case("verify_thanks_fail_jev114")
    g = VisionGate()
    g._backend_for = lambda model: ReplayBackend(pass_case)  # type: ignore[method-assign]
    v = g.verify(_fixture_frame("thanks"), "a message thanking Ada for her order")
    assert v.passed is True and v.yes >= 0.7
    g._backend_for = lambda model: ReplayBackend(fail_case)  # type: ignore[method-assign]
    v = g.verify(_fixture_frame("form"), "a message thanking Ada for her order")
    assert v.passed is False


def test_replay_recorded_safety_payment_pauses():
    case = load_case("safety_payment_jev114")
    replay = ReplayBackend(case)
    g = VisionGate()
    g._backend_for = lambda model: replay  # type: ignore[method-assign]
    s = g.safety(_fixture_frame("payment"))
    assert s.pause is True
    assert s.category == "payment"
    assert s.probs["payment"] >= 0.2
    assert s.confirm_dialog >= 0.5


def test_replay_recorded_openai_fallback_grounding():
    case = load_case("locate_submit_openai_fallback")
    replay = ReplayBackend(case)
    g = VisionGate()
    g._backend_for = lambda model: replay  # type: ignore[method-assign]
    r = g.locate(_fixture_frame("form"), "the Submit button")
    assert r.found is True
    assert r.grounding == "fallback"  # openai backend => fallback grounding


# --- stage-2 action-level safety (REAL recorded responses, fake transport) ---


def test_safety_hard_threshold_default():
    assert VisionGate().thresholds["safety_hard"] == pytest.approx(0.6)
    assert VisionGate({"thresholds": {"safety_hard": 0.8}}).thresholds["safety_hard"] == \
        pytest.approx(0.8)


def test_safety_action_questions_ask_about_the_exact_step(monkeypatch):
    case = load_case("safety_stage2_settings_gtk_dark_mode_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.safety_action(_fixture_frame("settings_gtk", 432, 330), "click", "Dark mode")
    assert r.error is None
    assert set(r.probs) == {"payment", "deletion", "account_or_security_change",
                            "send_or_publish"}
    qs = replay.calls[0]["questions"]
    instructions = " ".join(q.instructions for q in qs.values())
    assert "clicking 'Dark mode'" in instructions
    for phrase in ("spend money", "delete data",
                   "change a password, login, account or security setting",
                   "send or publish something to other people"):
        assert phrase in instructions
    assert replay.calls[0]["images"] and list(replay.calls[0]["images"])


def test_replay_recorded_safety_action_settings_dark_mode_clears(monkeypatch):
    """REAL stage-2 answer: clicking 'Dark mode' is not an account change (0.02)."""
    case = load_case("safety_stage2_settings_gtk_dark_mode_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.safety_action(_fixture_frame("settings_gtk", 432, 330), "click", "Dark mode")
    assert r.probs["account_or_security_change"] < 0.2
    assert r.probs["payment"] < 0.2 and r.probs["deletion"] < 0.2


def test_replay_recorded_safety_action_password_update_confirms(monkeypatch):
    """REAL stage-2 answer: clicking 'Update password' IS a security change (0.98)."""
    case = load_case("safety_stage2_password_update_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.safety_action(_fixture_frame("password"), "click", "Update password")
    assert r.probs["account_or_security_change"] >= 0.2


def test_replay_recorded_safety_action_delete_confirms(monkeypatch):
    """REAL stage-2 answer: clicking 'Delete' DOES delete data (0.99)."""
    case = load_case("safety_stage2_delete_confirm_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    r = gate.safety_action(_fixture_frame("delete_confirm"), "click", "Delete")
    assert r.probs["deletion"] >= 0.2


def test_replay_recorded_stage1_settings_gtk_false_pause_band(monkeypatch):
    """REAL stage-1 answer on the plain GTK settings window: 0.39 on
    account_or_security_change — the ambiguous band that caused the false
    pause before stage 2 existed."""
    case = load_case("safety_stage1_settings_gtk_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    s = gate.safety(_fixture_frame("settings_gtk", 432, 330))
    assert 0.2 <= s.probs["account_or_security_change"] < 0.6
    assert s.pause is True  # stage-1-alone bias to ask; stage 2 clears it


def test_safety_action_all_models_fail_returns_error(monkeypatch):
    gate = gate_with_backends(
        monkeypatch,
        {
            "jev-1.14.0": FailingBackend("400 bad"),
            "jev-1.13.0": FailingBackend("500 boom"),
            "openai:gemini-3.8-flash-high": FailingBackend("connection refused"),
        },
    )
    r = gate.safety_action(_fixture_frame("form"), "click", "Submit")
    assert r.error and not r.probs
    assert r.usage is not None and len(r.usage.fallbacks) == 3


# --- hash thresholds ------------------------------------------------------------------


def test_changed_hash_paths(monkeypatch):
    form = _fixture_frame("form")
    payment = _fixture_frame("payment")

    # Real screenshots of different pages: changed via hash, no model call.
    replay = ReplayBackend(_noul_case())
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    c = gate.changed(form, payment)
    assert isinstance(c, ChangeResult)
    assert c.changed is True and c.path == "hash" and c.usage is None
    assert c.distance >= 12
    assert replay.calls == []


def test_changed_unchanged_band(monkeypatch):
    form = _fixture_frame("form")
    replay = ReplayBackend(_noul_case())
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    c = gate.changed(form, form)
    assert c.changed is False and c.path == "hash" and c.distance <= 3
    assert replay.calls == []


def test_changed_ambiguous_band_asks_model(monkeypatch):
    from hermes_loopguard.cu import frames as cu_frames

    form = _fixture_frame("form")

    real_dhash = cu_frames.dhash  # noqa: F841  (kept for reference in review)
    values = iter([0b0000, 0b0001111111])  # hamming 7 -> ambiguous band

    def fake_dhash(png, size=8):
        return next(values)

    monkeypatch.setattr(cu_frames, "dhash", fake_dhash)
    replay = ReplayBackend(_noul_case(noul=0.8, name="changed"))
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    c = gate.changed(form, _fixture_frame("payment"))
    assert c.path == "vision" and c.distance == 7
    assert c.changed is True  # noul 0.8 >= 0.5
    assert replay.calls and c.usage is not None


def test_changed_vision_backend_error_returns_none(monkeypatch):
    from hermes_loopguard.cu import frames as cu_frames

    values = iter([0b0000, 0b0001111111])  # hamming 7 -> ambiguous band
    monkeypatch.setattr(cu_frames, "dhash", lambda png, size=8: next(values))
    gate = gate_with_backends(
        monkeypatch,
        {
            "jev-1.14.0": FailingBackend("400 bad"),
            "jev-1.13.0": FailingBackend("500 boom"),
            "openai:gemini-3.8-flash-high": FailingBackend("connection refused"),
        },
    )
    c = gate.changed(_fixture_frame("form"), _fixture_frame("payment"))
    assert c.changed is None
    assert c.path == "vision" and c.distance == 7
    assert c.usage is not None and len(c.usage.fallbacks) == 3


# --- safety gate logic -----------------------------------------------------------------


def test_safety_low_probs_no_pause(monkeypatch):
    case = load_case("safety_payment_jev114")
    case = json.loads(json.dumps(case))
    case["answers"]["category"]["probabilities"] = {
        "payment": 0.05, "deletion": 0.0, "send_or_publish": 0.0,
        "account_or_security_change": 0.0, "none": 0.95,
    }
    case["answers"]["category"]["choice"] = "none"
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    s = gate.safety(_fixture_frame("form"))
    assert isinstance(s, SafetyResult)
    assert s.pause is False and s.category == "none"


def test_safety_dialog_visible_without_risky_category_does_not_pause(monkeypatch):
    # Per the contract, pause is gated on non-`none` category probabilities;
    # the confirm-dialog Noul is reported (`confirm_dialog`) but does not by
    # itself flip `pause`.
    case = load_case("safety_payment_jev114")
    case = json.loads(json.dumps(case))
    case["answers"]["category"]["probabilities"] = {
        "payment": 0.0, "deletion": 0.0, "send_or_publish": 0.0,
        "account_or_security_change": 0.0, "none": 1.0,
    }
    case["answers"]["category"]["choice"] = "none"
    case["answers"]["confirm"]["noul"] = 0.99
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    s = gate.safety(_fixture_frame("payment"))
    assert s.pause is False
    assert s.confirm_dialog == pytest.approx(0.99)


# --- pricing ----------------------------------------------------------------------------


def test_pricing_math():
    assert usd("jev-1.14.0", 1_000_000, 1_000_000) == pytest.approx(0.042)  # output free
    assert usd("jev-1.13.0", 500_000, 0) == pytest.approx(0.021)
    assert usd("gemini-3.8-flash-high", 1_000_000, 1_000_000) == pytest.approx(0.75 + 3.75)
    assert usd("claude-opus-4-6-thinking", 1_000_000, 1_000_000) == pytest.approx(5.0 + 25.0)
    assert usd("unknown-model", 1_000_000, 1_000_000) == 0.0
    assert usd("jev-1.14.0", 0, 0) == 0.0


# --- verify uses one image placed once ---------------------------------------------------


def test_verify_before_and_after_images_both_sent(monkeypatch):
    case = load_case("verify_thanks_pass_jev114")
    replay = ReplayBackend(case)
    gate = gate_with_backends(monkeypatch, {"jev-1.14.0": replay})
    v = gate.verify(
        _fixture_frame("thanks"), "a message thanking Ada", before=_fixture_frame("form")
    )
    assert v.passed is True
    call = replay.calls[0]
    assert len(call["images"]) == 2
    refs = set(call["images"])
    # each ref appears in the state text exactly as a reference
    assert all(f"`{ref}`" in call["state"] for ref in refs)


def test_ask_retries_once_on_typesafe_image_dimensions_400():
    """REAL failure (mousepad-save r2, thunar-move r1 in jev-x7): TypeSafe
    answers `400 Invalid image dimensions` for an image it accepted unchanged
    a moment earlier; the locate died and the step failed.  ask() must retry
    the SAME model once before falling to the next in the chain."""
    from hermes_loopguard.cu import vision as vision_mod
    from hermes_loopguard.questions import Noul, NoulAnswer

    gate = vision_mod.VisionGate({"models": ["jev-1.14.0", "openai:gemini-3.8-flash-high"]})

    from types import SimpleNamespace

    class FlakyBackend:
        name = "typesafe"
        calls = 0

        def ask(self, state, questions, *, images=None, model=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "TypeSafe System One call failed: 400 Invalid image dimensions."
                )
            return SimpleNamespace(
                answers={q: NoulAnswer(noul=0.9) for q in questions}, usage=None
            )

    class ChainBackend:
        name = "openai"
        calls = 0

        def ask(self, state, questions, *, images=None, model=None):
            self.calls += 1
            return SimpleNamespace(
                answers={q: NoulAnswer(noul=0.1) for q in questions}, usage=None
            )

    flaky, chain = FlakyBackend(), ChainBackend()
    orig = vision_mod.VisionGate._backend_for
    gate._backend_for = lambda m: flaky if m == "jev-1.14.0" else chain
    try:
        answers, usage = gate.ask({}, "s", {"q": Noul(instructions="x")})
    finally:
        vision_mod.VisionGate._backend_for = orig
    assert flaky.calls == 2  # one retry of the same model
    assert chain.calls == 0  # the chain was NOT consumed
    assert answers["q"].noul == 0.9
    assert usage.model == "jev-1.14.0"
