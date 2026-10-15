from __future__ import annotations

from typing import Any

from hermes_loopguard.backends.base import Backend, BackendError, GateResult, Usage
from hermes_loopguard.backends.hermes_llm import HermesLlmBackend, get_llm, set_llm
from hermes_loopguard.backends.local import LocalBackend
from hermes_loopguard.backends.openai_compat import OpenAICompatBackend
from hermes_loopguard.backends.typesafe import TypeSafeBackend
from hermes_loopguard.questions import Question

__all__ = [
    "Backend",
    "BackendError",
    "GateResult",
    "Usage",
    "LocalBackend",
    "OpenAICompatBackend",
    "TypeSafeBackend",
    "HermesLlmBackend",
    "make_backend",
    "backend_chain",
    "ask_with_fallback",
    "set_llm",
    "get_llm",
]


def make_backend(name: str, cfg: dict) -> Backend:
    if name == "typesafe":
        return TypeSafeBackend(cfg)
    elif name == "openai":
        return OpenAICompatBackend(cfg)
    elif name == "hermes":
        return HermesLlmBackend(cfg)
    elif name == "local":
        return LocalBackend(cfg)
    else:
        raise ValueError(f"Unknown backend: {name}")


def backend_chain(cfg: dict) -> list[Backend]:
    target = cfg.get("backend", "auto")
    if target == "auto":
        candidates = [
            make_backend("typesafe", cfg),
            make_backend("openai", cfg),
            make_backend("hermes", cfg),
        ]
        chain = [b for b in candidates if b.available()]
        chain.append(make_backend("local", cfg))
        return chain
    else:
        return [make_backend(target, cfg)]


def ask_with_fallback(
    chain: list[Backend],
    state: str | dict,
    questions: dict[str, Question],
) -> GateResult:
    errors: list[str] = []
    for backend in chain:
        try:
            res = backend.ask(state, questions)
            if errors:
                res.error = "; ".join(errors)
            return res
        except Exception as e:
            errors.append(f"{backend.name}: {e}")

    raise BackendError(f"All backends in chain failed: {'; '.join(errors)}")
