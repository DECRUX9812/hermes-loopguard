"""Replay driver for unit tests: frames + elements + per-action results from a
JSON fixture, with every call recorded for assertions.  UNIT-TEST FAKE — never
used outside tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hermes_loopguard.cu.driver import ActionResult, Element, Frame, register_driver

__all__ = ["FakeDriver"]

_ACTION_METHODS = ("click", "click_element", "type_text", "set_value", "key", "scroll")


@register_driver("fake")
class FakeDriver:
    """Replays a recorded script.

    Fixture shape (paths relative to the fixture file)::

        {
          "frames":  [{"file": "frame-1.png", "width": 1280, "height": 800}],
          "elements": {"Google-chrome": [{"token": "e1", "role": "entry",
                        "label": "Name", "x": 10, "y": 20, "w": 300, "h": 30}]},
          "actions": {"click": [{"ok": true, "effect": "confirmed", "verified": true}]}
        }

    ``screenshot()`` walks ``frames`` in order and then repeats the last one;
    ``elements()`` returns the (optionally per-app) recorded list; each action
    pops its method's next recorded result and cycles the last one.  Every
    call lands in ``self.calls`` as ``{"method", "args"}`` dicts.
    """

    name = "fake"

    def __init__(self, fixture: dict | str | Path, base_dir: str | Path | None = None):
        if isinstance(fixture, (str, Path)):
            fixture_path = Path(fixture)
            fixture = json.loads(fixture_path.read_text())
            if base_dir is None:
                base_dir = fixture_path.parent
        else:
            fixture = dict(fixture)
        self._base = Path(base_dir) if base_dir is not None else Path.cwd()
        self._frames: list[dict] = list(fixture.get("frames", []))
        self._elements: dict[str, list[dict]] = {
            k: list(v) for k, v in (fixture.get("elements", {}) or {}).items()
        }
        self._actions: dict[str, list[dict]] = {
            k: list(v) for k, v in (fixture.get("actions", {}) or {}).items()
        }
        self._frame_i = 0
        self._action_i: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    @classmethod
    def from_fixture(cls, path: str | Path) -> "FakeDriver":
        return cls(path)

    # -- capture ------------------------------------------------------------

    def screenshot(self) -> Frame:
        if not self._frames:
            raise ValueError("FakeDriver fixture has no frames")
        spec = self._frames[min(self._frame_i, len(self._frames) - 1)]
        self._frame_i += 1
        png = b""
        if spec.get("file"):
            png = (self._base / spec["file"]).read_bytes()
        elif spec.get("png_b64"):
            import base64

            png = base64.b64decode(spec["png_b64"])
        self._record("screenshot", {"file": spec.get("file")})
        return Frame(
            png=png,
            width=int(spec.get("width", 0)),
            height=int(spec.get("height", 0)),
            ts=time.time(),
            path=str(self._base / spec["file"]) if spec.get("file") else None,
        )

    # -- accessibility ------------------------------------------------------

    def elements(self, app: str | None = None) -> list[Element]:
        if app is not None:
            rows = list(self._elements.get(app, []))
        elif len(self._elements) == 1:
            rows = list(next(iter(self._elements.values())))
        else:
            rows = [row for rows_ in self._elements.values() for row in rows_]
        self._record("elements", {"app": app, "count": len(rows)})
        return [
            Element(
                token=str(r["token"]),
                role=str(r.get("role", "")),
                label=str(r.get("label", "")),
                x=int(r["x"]),
                y=int(r["y"]),
                w=int(r.get("w", 0)),
                h=int(r.get("h", 0)),
                app=str(r.get("app", app or "")),
            )
            for r in rows
        ]

    def find_element(self, label: str, role: str | None = None) -> Element | None:
        want = label.strip().lower()
        for el in self.elements():
            if want in el.label.lower() and (role is None or role.lower() in el.role.lower()):
                return el
        return None

    # -- actions ------------------------------------------------------------

    def _next_result(self, method: str) -> ActionResult:
        results = self._actions.get(method, [])
        if not results:
            return ActionResult(ok=True, effect="confirmed", raw={"replayed": False})
        i = self._action_i.get(method, 0)
        self._action_i[method] = min(i + 1, len(results) - 1)
        return parse_fixture_result(results[i])

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> ActionResult:
        self._record("click", {"x": x, "y": y, "button": button, "count": count})
        return self._next_result("click")

    def click_element(self, el: Element) -> ActionResult:
        self._record("click_element", {"token": el.token})
        return self._next_result("click_element")

    def type_text(self, text: str) -> ActionResult:
        self._record("type_text", {"text": text})
        return self._next_result("type_text")

    def set_value(self, element_token: str, text: str) -> ActionResult:
        self._record("set_value", {"element_token": element_token, "text": text})
        return self._next_result("set_value")

    def key(self, keys: str) -> ActionResult:
        self._record("key", {"keys": keys})
        return self._next_result("key")

    def scroll(self, x: int, y: int, direction: str, amount: int = 3) -> ActionResult:
        self._record("scroll", {"x": x, "y": y, "direction": direction, "amount": amount})
        return self._next_result("scroll")

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self.closed = True
        self._record("close", {})

    # -- helpers ------------------------------------------------------------

    def _record(self, method: str, args: dict) -> None:
        self.calls.append({"method": method, "args": args, "ts": time.time()})


def parse_fixture_result(row: dict) -> ActionResult:
    """Build an ActionResult from one fixture row (already cua-shaped)."""
    return ActionResult(
        ok=bool(row.get("ok", True)),
        effect=str(row.get("effect", "unverifiable")),
        verified=row.get("verified"),
        escalation=row.get("escalation"),
        error=row.get("error"),
        raw=dict(row.get("raw", {})),
    )
