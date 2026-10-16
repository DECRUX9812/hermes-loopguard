"""cua-driver adapter: subprocess transport, no native wheels imported.

Every call runs ``cua-driver call --socket <socket> <tool> '<json>'`` (the JSON
payload is passed via argv, never stdin) against a ``cua-driver serve`` daemon.
Element bounds come back in WINDOW-local pixels and are converted to DESKTOP
pixels here (window origin added); clicks at desktop x,y are converted back to
window-local pixels by finding the window under the point.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from hermes_loopguard.cu.driver import (
    ActionResult,
    DriverError,
    Element,
    Frame,
    parse_action_result,
    register_driver,
)

__all__ = [
    "CuaDriver",
    "best_match",
    "desktop_to_window_local",
    "match_score",
    "window_local_to_desktop",
]

_MOUSE_BUTTONS = {"left", "right", "middle"}


def match_score(want: str, label: str) -> int:
    """Specificity of a label match (higher = better; -1 = no match).

    First-substring-match picks the WRONG element when one label merely
    contains the target ("Search…" input) and another IS the target
    ("Search" button): the substring click landed on the input and the
    step did nothing.  Exact beats prefix beats substring; a label fully
    contained in the target ("Dark mode" for target "Dark mode switch")
    still matches — planners over-specify switch/checkbox labels — but
    only on a WORD BOUNDARY: target "Username field" must not match the
    label "Name" hiding inside "Username" (X8: real near-tie that picked
    the wrong field).
    """
    if not want or not label:
        return -1
    want = want.strip().lower()  # callers may pass the raw planner target
    lbl = label.lower()
    if lbl == want:
        return 4
    if lbl.startswith(want):
        return 3
    if want in lbl:
        return 2
    if re.search(rf"(?<!\w){re.escape(lbl)}(?!\w)", want):
        return 1
    return -1


def best_match(
    elements: list[Element], want: str, *, role: str | None = None
) -> Element | None:
    """Best-specificity case-insensitive label match over an in-memory
    element list (ONE AT-SPI walk, no retries — see ground()).

    Ties are broken toward the LONGER label: with target "Username field",
    "Name" and "Username" would otherwise both score 1 and walk order (the
    shorter "Name" first) won (X8).  The longer label is the more specific
    match.
    """
    best: Element | None = None
    best_score = 0
    for el in elements:
        score = match_score(want, el.label)
        if score < 0:
            continue
        if role is not None and role.lower() not in el.role.lower():
            continue
        if score > best_score or (
            score == best_score and best is not None and len(el.label) > len(best.label)
        ):
            best, best_score = el, score
    return best


def desktop_to_window_local(x: int, y: int, window_bounds: dict) -> tuple[int, int] | None:
    """DESKTOP px -> WINDOW-local px.  None when the point is outside the window."""
    if not window_bounds:
        return None
    wx = int(window_bounds.get("x", 0))
    wy = int(window_bounds.get("y", 0))
    ww = int(window_bounds.get("width", 0))
    wh = int(window_bounds.get("height", 0))
    lx, ly = int(x) - wx, int(y) - wy
    if not (0 <= lx < ww and 0 <= ly < wh):
        return None
    return (lx, ly)


def window_local_to_desktop(lx: int, ly: int, window_bounds: dict) -> tuple[int, int]:
    """WINDOW-local px -> DESKTOP px (window origin added)."""
    if not window_bounds:
        return (int(lx), int(ly))
    return (int(lx) + int(window_bounds.get("x", 0)), int(ly) + int(window_bounds.get("y", 0)))


@register_driver("cua")
class CuaDriver:
    """Driver that shells out to the cua-driver CLI against its daemon socket."""

    name = "cua"

    def __init__(self, socket: str, app: str | None = None, cua_bin: str = "cua-driver", timeout_s: int = 20):
        self.socket = socket
        self.app = app
        self.cua_bin = cua_bin
        self.timeout_s = timeout_s
        self._tmpdir: str | None = None
        self._frame_no = 0
        self._windows: list[dict] = []
        self._token_windows: dict[str, dict] = {}  # element_token -> owning window
        self.calls: list[dict] = []  # every tool call: {tool, payload, result}

    # -- transport ----------------------------------------------------------

    def _call(self, tool: str, payload: dict) -> dict:
        """Run one CLI call.  NEVER raises; failures come back as {"_cua_error": ...}."""
        cmd = [self.cua_bin, "call", "--socket", self.socket, tool, json.dumps(payload)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"_cua_error": f"timeout after {self.timeout_s}s: {tool}"}
        except FileNotFoundError:
            return {"_cua_error": f"cua-driver binary not found: {self.cua_bin}"}
        except OSError as exc:
            return {"_cua_error": f"os error running {tool}: {exc}"}
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return {"_cua_error": f"{tool} exited {proc.returncode}: {detail}"}
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            snippet = proc.stdout.strip()[:300]
            return {"_cua_error": f"{tool}: non-JSON stdout: {snippet!r}"}
        self.calls.append({"tool": tool, "payload": payload, "result": result})
        return result

    @staticmethod
    def _unwrap(result: dict) -> dict:
        """Get the structured payload out of an MCP-style tool result."""
        if not isinstance(result, dict):
            return {}
        if "_cua_error" in result:
            return result
        inner = result.get("structuredContent")
        if isinstance(inner, dict):
            merged = dict(inner)
            merged.setdefault("_raw", result)
            return merged
        return result

    # -- capture ------------------------------------------------------------

    def screenshot(self) -> Frame:
        if self._tmpdir is None:
            self._tmpdir = tempfile.mkdtemp(prefix="lg-cua-frames-")
        self._frame_no += 1
        path = os.path.join(self._tmpdir, f"frame-{self._frame_no:06d}.png")
        # One transient capture error (a real cua-daemon "Capture err..." with
        # non-JSON stdout killed a whole bench run) should not go terminal:
        # retry once after a short settle before giving up.
        last_error: str | None = None
        for attempt in range(2):
            result = self._unwrap(
                self._call("get_desktop_state", {"screenshot_out_file": path})
            )
            if not result.get("_cua_error") and os.path.exists(path):
                break
            last_error = (
                f"get_desktop_state failed: {result.get('_cua_error')}"
                if result.get("_cua_error")
                else "get_desktop_state did not write screenshot_out_file"
            )
            if attempt == 0:
                time.sleep(1.0)
        else:
            raise DriverError(last_error or "get_desktop_state failed")
        with open(path, "rb") as fh:
            png = fh.read()
        width = int(result.get("screen_width") or result.get("screenshot_width") or 0)
        height = int(result.get("screen_height") or result.get("screenshot_height") or 0)
        if not width or not height:
            raise DriverError("get_desktop_state returned no screen dimensions")
        return Frame(png=png, width=width, height=height, ts=time.time(), path=path)

    # -- accessibility ------------------------------------------------------

    def _list_windows(self, refresh: bool = True) -> list[dict]:
        if refresh or not self._windows:
            result = self._unwrap(self._call("list_windows", {"on_screen_only": True}))
            windows = result.get("windows") if isinstance(result, dict) else None
            self._windows = [w for w in windows if isinstance(w, dict)] if isinstance(windows, list) else []
        return self._windows

    @staticmethod
    def _app_matches(window_app: str, wanted: str) -> bool:
        a = (window_app or "").lower().replace("_", "-")
        b = (wanted or "").lower().replace("_", "-")
        return bool(a) and (b in a or a in b)

    def elements(self, app: str | None = None) -> list[Element]:
        """AT-SPI elements of the target app's windows, in DESKTOP pixels."""
        wanted = app or self.app
        out: list[Element] = []
        for window in self._list_windows():
            if wanted and not self._app_matches(window.get("app_name", ""), wanted):
                continue
            out.extend(self._window_elements(window))
        return out

    def _window_elements(self, window: dict) -> list[Element]:
        # Single walk per call (contract semantics).  Chrome exposes its
        # web-content tree only from the second walk on — find_element()
        # retries with fresh walks, which also keeps element tokens fresh
        # (cua-driver invalidates tokens from superseded snapshots).
        result = self._unwrap(
            self._call(
                "get_window_state",
                {
                    "pid": window.get("pid"),
                    "window_id": window.get("window_id"),
                    "include_screenshot": False,
                },
            )
        )
        result.pop("_raw", None)
        if result.get("_cua_error") or result.get("degraded"):
            return []
        return self._parse_elements(result, window)

    def _parse_elements(self, result: dict, window: dict | None = None) -> list[Element]:
        # Real cua-driver 0.28.2 element frames are already DESKTOP-absolute
        # (verified: root frame == window_bounds origin; clicks land).  No
        # window-origin shift is applied here.
        elements: list[Element] = []
        for el in result.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            frame = el.get("frame") or {}
            if not all(k in frame for k in ("x", "y", "w", "h")):
                continue
            token = str(el.get("element_token", ""))
            if window is not None and token:
                self._token_windows[token] = window
            elements.append(
                Element(
                    token=token,
                    role=str(el.get("role", "")),
                    label=str(el.get("label", "") or el.get("value", "") or ""),
                    x=int(frame["x"]),
                    y=int(frame["y"]),
                    w=int(frame["w"]),
                    h=int(frame["h"]),
                    app=str(result.get("app_name") or ""),
                )
            )
        return elements

    # Matching lives in module-level match_score/best_match (shared with the
    # agent's in-memory grounding filter); kept as a staticmethod because
    # tests and callers reference it.
    @staticmethod
    def _match_score(want: str, label: str) -> int:
        return match_score(want, label)

    def find_element(self, label: str, role: str | None = None, retries: int = 3) -> Element | None:
        """Best-specificity case-insensitive label match, with fresh AT-SPI
        walks.

        Retries matter for Chrome: the web-content tree is exposed only from
        the second walk on, and each walk returns fresh element tokens.
        """
        want = label.strip().lower()
        for attempt in range(max(1, retries)):
            best = best_match(self.elements(), want, role=role)
            if best is not None:
                return best
            time.sleep(0.8)
        return None

    # -- actions ------------------------------------------------------------

    def _window_at(self, x: int, y: int) -> dict | None:
        """The on-screen window covering a DESKTOP point (topmost z first).

        Skips windows without a pid (cua-driver's own cursor overlay is
        pid-less, full-screen and topmost; it can never be clicked).
        """
        windows = [
            w for w in self._list_windows()
            if isinstance(w.get("bounds"), dict) and w.get("pid")
        ]
        windows.sort(key=lambda w: w.get("z_index") if isinstance(w.get("z_index"), int) else -1, reverse=True)
        for window in windows:
            if desktop_to_window_local(x, y, window["bounds"]) is not None:
                return window
        return None

    def _window_target(self, window: dict) -> dict:
        return {"kind": "window", "pid": window["pid"], "window_id": window["window_id"]}

    def _parse(self, result: dict) -> ActionResult:
        return parse_action_result(result, ok=None if "_cua_error" not in result else False)

    def _act(self, tool: str, payload: dict, escalate: bool = True) -> ActionResult:
        """One action call, auto-escalating to foreground when needed.

        Two shapes mean the background attempt was NOT delivered:
        - code "background_unavailable" (explicit refusal), and
        - route "global_input" with background delivery: cua-driver 0.28.2
          queues window-targeted global input in background mode and this
          stack silently DROPS it (verified live: thunar clicks and chrome
          <select> popup menu items change nothing on screen; route
          "accessibility" clicks DO work in background).  No error code is
          returned, so the route is the only signal.
        """
        result = self._call(tool, payload)
        code = result.get("code") if isinstance(result, dict) else None
        route = result.get("route") if isinstance(result, dict) else None
        delivery_mode = ((result.get("delivery") or {}).get("mode")
                         if isinstance(result, dict) else None)
        if escalate and (
            code == "background_unavailable"
            or (route == "global_input" and delivery_mode == "background")
        ):
            retry = dict(payload)
            retry["delivery_mode"] = "foreground"
            result = self._call(tool, retry)
        return self._parse(result)

    def click(self, x: int, y: int, *, button: str = "left", count: int = 1) -> ActionResult:
        button = button if button in _MOUSE_BUTTONS else "left"
        window = self._window_at(x, y)
        if window is not None:
            # REAL cua-driver 0.28.2 takes WINDOW-LOCAL pixels for window
            # targets (verified by discriminating click test, 2026-10-15).
            local = desktop_to_window_local(x, y, window["bounds"])
            payload: dict[str, Any] = {
                "pid": window["pid"],
                "window_id": window["window_id"],
                "x": local[0],
                "y": local[1],
                "button": button,
                "count": count,
            }
        else:
            payload = {
                "target": {"kind": "desktop", "display_id": "primary"},
                "x": x,
                "y": y,
                "button": button,
                "count": count,
            }
        return self._act("click", payload)

    def click_element(self, el: Element) -> ActionResult:
        payload: dict[str, Any] = {"element_token": el.token}
        # cua 0.28.2 requires pid alongside element_token.
        window = self._token_windows.get(el.token) or self._app_window()
        if window is not None:
            payload["pid"] = window["pid"]
        return self._act("click", payload)

    @staticmethod
    def _z(window: dict | None) -> int:
        z = (window or {}).get("z_index")
        return z if isinstance(z, int) else -1

    def _app_window(self) -> dict | None:
        """The app's TOPMOST window (a Save As dialog outranks its editor).

        list_windows order is bottom-up (z_index 0 first), so the first match
        can be a background window; typing into it would steal focus from the
        dialog the user (or the agent) is actually working in.
        """
        if not self.app:
            return None
        matches = [
            w for w in self._list_windows()
            if self._app_matches(w.get("app_name", ""), self.app)
        ]
        if not matches:
            return None
        return max(matches, key=self._z)

    def _topmost_window(self) -> dict | None:
        windows = [w for w in self._list_windows() if w.get("pid")]
        if not windows:
            return None
        return max(windows, key=self._z)

    def _target_window(self) -> dict | None:
        """The window keyboard delivery should go to: the app's TOPMOST
        window.  A modal dialog (Save As) outranks its editor; typing must
        never activate the background window — that would STEAL focus from
        the dialog the agent just opened."""
        return self._app_window()

    def _activate(self, window: dict) -> None:
        """Best-effort window activation so XTEST keyboard delivery lands.
        Skipped when the window is already topmost (activating a background
        window would STEAL focus from the dialog the agent just clicked)."""
        top = self._topmost_window()
        if top is not None and top.get("window_id") == window.get("window_id"):
            return
        self._call(
            "bring_to_front",
            {"pid": window["pid"], "window_id": window["window_id"]},
        )
        time.sleep(0.3)

    def type_text(self, text: str) -> ActionResult:
        # Keyboard delivery needs an active window (XTEST): target the app's
        # topmost window (a modal dialog outranks its editor), activate it
        # only when needed, then type in the foreground.
        payload: dict[str, Any] = {"text": str(text), "delivery_mode": "foreground"}
        window = self._target_window()
        if window is not None:
            payload["pid"] = window["pid"]
            payload["window_id"] = window["window_id"]
            self._activate(window)
        return self._act("type_text", payload, escalate=False)

    def set_value(self, element_token: str, text: str) -> ActionResult:
        """AT-SPI EditableText SetValue on one element (cua-driver 0.28.2
        ``set_value`` tool).  Replaces the element's WHOLE value: no
        keystrokes, no focus change, no preselection to lose — the reliable
        delivery into GTK entries whose XSendEvent typing is flaky
        (thunar-rename's inline edit, mousepad-save's Save-As field).

        Not routed through ``_act()``: set_value is an accessibility action
        (never background global input), so there is no foreground escalation
        to make.  cua 0.28.2 requires ``pid`` alongside ``element_token``.
        """
        payload: dict[str, Any] = {"element_token": element_token, "value": str(text)}
        window = self._token_windows.get(element_token) or self._app_window()
        if window is not None:
            payload["pid"] = window["pid"]
        return self._parse(self._call("set_value", payload))

    def key(self, keys: str) -> ActionResult:
        """``"Return"``, ``"Tab"`` or combos like ``"ctrl+a"`` / ``"ctrl+shift+s"``."""
        parts = [p for p in keys.strip().split("+") if p]
        if len(parts) > 1:
            payload: dict[str, Any] = {"keys": parts, "delivery_mode": "foreground"}
            tool = "hotkey"
        else:
            payload = {"key": parts[0] if parts else keys, "delivery_mode": "foreground"}
            tool = "press_key"
        window = self._target_window()
        if window is not None:
            payload["pid"] = window["pid"]
            payload["window_id"] = window["window_id"]
            self._activate(window)
        return self._act(tool, payload, escalate=False)

    def scroll(self, x: int, y: int, direction: str, amount: int = 3) -> ActionResult:
        direction = direction if direction in ("up", "down", "left", "right") else "down"
        window = self._window_at(x, y)
        if window is not None:
            local = desktop_to_window_local(x, y, window["bounds"])
            payload = {
                "pid": window["pid"],
                "window_id": window["window_id"],
                "x": local[0],
                "y": local[1],
                "direction": direction,
                "amount": max(1, min(int(amount), 50)),
            }
        else:
            payload = {
                "target": {"kind": "desktop", "display_id": "primary"},
                "x": x,
                "y": y,
                "direction": direction,
                "amount": max(1, min(int(amount), 50)),
            }
        return self._act("scroll", payload)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Drop the frame temp dir.  Never kills the daemon (the stack owns it)."""
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
