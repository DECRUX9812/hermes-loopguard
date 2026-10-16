"""Driver contract for computer use (P2).

All coordinates crossing this interface are full-resolution DESKTOP pixels.
Adapters convert to window-local pixels for tools that need them.

Action methods never raise for action failures; they return
``ActionResult(ok=False, effect="error", error=...)``.  Only ``screenshot()``
may raise :class:`DriverError`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

__all__ = [
    "DriverError",
    "Frame",
    "Element",
    "ActionResult",
    "EFFECTS",
    "parse_action_result",
    "ComputerDriver",
    "register_driver",
    "get_driver",
]

#: Canonical effect values (contract: confirmed | unverifiable | suspected_noop | error).
EFFECTS = ("confirmed", "unverifiable", "suspected_noop", "error")


class DriverError(RuntimeError):
    """Raised only where the contract allows it (e.g. a failed screenshot)."""


@dataclass(frozen=True)
class Frame:
    """A full-resolution screenshot of the whole desktop."""

    png: bytes
    width: int
    height: int
    ts: float
    path: str | None = None


@dataclass(frozen=True)
class Element:
    """One actionable element from the accessibility tree, in DESKTOP pixels."""

    token: str
    role: str
    label: str
    x: int
    y: int
    w: int
    h: int
    app: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


@dataclass
class ActionResult:
    """Outcome of one driver action.  ``effect`` is one of :data:`EFFECTS`."""

    ok: bool
    effect: str = "unverifiable"
    verified: bool | None = None
    escalation: str | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


def _dig(raw: dict, key: str):
    """Fetch ``key`` from a raw tool result, unwrapping MCP-style envelopes."""
    if key in raw:
        return raw[key]
    for envelope in ("structuredContent", "result", "state"):
        inner = raw.get(envelope)
        if isinstance(inner, dict) and key in inner:
            return inner[key]
    return None


def parse_action_result(raw: dict, ok: bool | None = None) -> ActionResult:
    """Parse ``effect``/``verified``/``escalation`` out of a REAL cua tool result.

    Tolerates MCP envelopes (``structuredContent``/``content``) and error
    shapes.  Real cua 0.28.2 action results look like
    ``{"effect": "unverifiable", "route": "accessibility", "delivery": {...}}``
    and failures like ``{"code": "background_unavailable", "detail": ...,
    "escalation": {"reason": ..., "recommended": "foreground"}}``.
    ``ok`` defaults to "no error/code field and effect != error".
    """
    raw = raw if isinstance(raw, dict) else {}
    error = _dig(raw, "error") or raw.get("_cua_error")
    detail = _dig(raw, "detail")
    code = _dig(raw, "code")
    effect = _dig(raw, "effect")
    verified = _dig(raw, "verified")
    escalation = _dig(raw, "escalation")

    if isinstance(verified, str):
        verified = {"true": True, "false": False}.get(verified.lower())
    if isinstance(error, dict):
        error = error.get("message") or str(error)
    if code and not error:
        # cua structured failure: code + human detail (+ escalation advice)
        error = f"{code}: {detail}" if detail else str(code)
    if isinstance(escalation, dict):
        escalation = escalation.get("recommended") or escalation.get("target") or escalation.get("reason")
    if effect not in EFFECTS:
        effect = "error" if error else "unverifiable"
    if ok is None:
        ok = error is None and effect != "error"
    return ActionResult(
        ok=bool(ok),
        effect=effect,
        verified=verified if isinstance(verified, bool) or verified is None else None,
        escalation=escalation if isinstance(escalation, str) else None,
        error=str(error) if error else None,
        raw=raw,
    )


@runtime_checkable
class ComputerDriver(Protocol):
    name: str

    def screenshot(self) -> Frame: ...

    def elements(self, app: str | None = None) -> list[Element]:
        """[] when the accessibility tree is unavailable."""
        ...

    def find_element(self, label: str, role: str | None = None) -> Element | None: ...

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> ActionResult:
        """Desktop pixels."""
        ...

    def click_element(self, el: Element) -> ActionResult: ...

    def type_text(self, text: str) -> ActionResult: ...

    def set_value(self, element_token: str, text: str) -> ActionResult:
        """Set an a11y element's value via AT-SPI EditableText: REPLACES the
        whole value with no keystrokes and no focus change (the reliable
        path into GTK entries, where XSendEvent typing is flaky)."""
        ...

    def key(self, keys: str) -> ActionResult: ...

    def scroll(self, x: int, y: int, direction: str, amount: int = 3) -> ActionResult: ...

    def close(self) -> None: ...


_DRIVERS: dict[str, Callable[..., ComputerDriver]] = {}


def register_driver(name: str) -> Callable[[Callable[..., ComputerDriver]], Callable[..., ComputerDriver]]:
    """Register a driver factory under ``name`` (built-ins are pre-registered)."""

    def deco(factory: Callable[..., ComputerDriver]) -> Callable[..., ComputerDriver]:
        _DRIVERS[name] = factory
        return factory

    return deco


def _builtin_factory(name: str):
    if name == "cua":
        from hermes_loopguard.cu.drivers.cua import CuaDriver

        return CuaDriver
    if name == "fake":
        from hermes_loopguard.cu.drivers.fake import FakeDriver

        return FakeDriver
    return None


def get_driver(name: str, **kwargs) -> ComputerDriver:
    """Build a driver by name from the registry (built-ins: "cua", "fake")."""
    factory = _DRIVERS.get(name) or _builtin_factory(name)
    if factory is None:
        raise DriverError(f"unknown driver {name!r}; registered: {sorted(set(_DRIVERS) | {'cua', 'fake'})}")
    return factory(**kwargs)


def known_drivers() -> list[str]:
    return sorted({"cua", "fake"} | set(_DRIVERS))
