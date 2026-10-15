from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from hermes_loopguard.questions import Answer, Question


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class GateResult:
    answers: dict[str, Answer]
    backend: str
    model: str
    usage: Usage
    error: str | None = None
    raw: Any = None


class Backend(Protocol):
    name: str

    def available(self) -> bool: ...

    def ask(self, state: str | dict, questions: dict[str, Question]) -> GateResult: ...


class BackendError(RuntimeError):
    pass
