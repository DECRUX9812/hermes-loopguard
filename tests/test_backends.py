from __future__ import annotations

import json
import pytest

from hermes_loopguard.backends import ask_with_fallback, backend_chain, make_backend
from hermes_loopguard.backends.base import BackendError, GateResult, Usage
from hermes_loopguard.backends.local import LocalBackend
from hermes_loopguard.backends.openai_compat import OpenAICompatBackend
from hermes_loopguard.questions import Choice, Noul, Score


def test_local_backend():
    cfg = {
        "local": {
            "keywords": {
                "bug": ["crash", "error", "traceback"],
                "actionable": {"yes": ["steps", "reproduce"], "no": ["vague", "help"]},
            }
        }
    }
    backend = LocalBackend(cfg)
    assert backend.available() is True

    qs = {
        "kind": Choice(instructions="Category", criteria={"bug": "defect", "feature": "request"}),
        "actionable": Noul(instructions="Is it actionable?"),
        "urgency": Score(instructions="Urgency level", criteria=["low", "medium", "high"]),
    }

    state = {"title": "App crashes with error traceback", "body": "Steps to reproduce: run command"}
    res = backend.ask(state, qs)
    assert res.backend == "local"
    assert res.model == "heuristic-v1"
    assert res.usage.cost_usd == 0.0

    assert "kind" in res.answers
    assert res.answers["kind"].choice == "bug"
    assert res.answers["kind"].probabilities["bug"] > res.answers["kind"].probabilities["feature"]

    assert "actionable" in res.answers
    assert res.answers["actionable"].noul > 0.5

    assert "urgency" in res.answers
    assert res.answers["urgency"].score == 0.0
    assert pytest.approx(res.answers["urgency"].probabilities[0]) == 1.0 / 3.0


def test_local_backend_uniform_when_no_hits():
    backend = LocalBackend({})
    qs = {"kind": Choice(instructions="Cat", criteria={"x": None, "y": None})}
    res = backend.ask({"msg": "nothing relevant"}, qs)
    assert pytest.approx(res.answers["kind"].probabilities["x"]) == 0.5
    assert pytest.approx(res.answers["kind"].probabilities["y"]) == 0.5


def test_openai_compat_backend_json_mode(monkeypatch):
    # FAKE BACKEND (unit test only)
    cfg = {
        "openai": {
            "base_url": "http://mock-llm.local/v1",
            "model": "mock-gpt",
            "api_key_env": "OPENAI_API_KEY",
            "usd_per_mtok_in": 1.5,
            "usd_per_mtok_out": 2.0,
        }
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-mock")

    backend = OpenAICompatBackend(cfg)
    assert backend.available() is True

    canned_reply = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "kind": {"probabilities": {"bug": 0.85, "feature": 0.15}},
                            "actionable": {"yes": 0.95},
                        }
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    def fake_post(payload):
        # FAKE BACKEND (unit test only)
        return canned_reply

    monkeypatch.setattr(backend, "_post", fake_post)

    qs = {
        "kind": Choice(instructions="Category", criteria={"bug": "defect", "feature": "request"}),
        "actionable": Noul(instructions="Actionable?"),
    }
    res = backend.ask({"title": "login bug"}, qs)
    assert res.answers["kind"].choice == "bug"
    assert pytest.approx(res.answers["kind"].confidence) == 0.85
    assert pytest.approx(res.answers["actionable"].noul) == 0.95
    assert res.usage.input_tokens == 100
    assert res.usage.output_tokens == 50
    # 100 * 1.5 / 1e6 + 50 * 2.0 / 1e6 = 0.00015 + 0.00010 = 0.00025
    assert pytest.approx(res.usage.cost_usd) == 0.00025


class RaisingBackend:
    # FAKE BACKEND (unit test only)
    name = "raising_backend"

    def available(self):
        return True

    def ask(self, state, questions):
        raise BackendError("Backend connection failed")


def test_fallback_chain_skips_raising_backend():
    # FAKE BACKEND (unit test only)
    failing = RaisingBackend()
    working = LocalBackend({})
    chain = [failing, working]

    qs = {"kind": Choice(instructions="Cat", criteria={"a": None, "b": None})}
    res = ask_with_fallback(chain, {"title": "item"}, qs)
    assert res.backend == "local"
    assert "raising_backend" in res.error

    # When all fail
    with pytest.raises(BackendError) as exc_info:
        ask_with_fallback([RaisingBackend()], {"title": "item"}, qs)
    assert "All backends in chain failed" in str(exc_info.value)
