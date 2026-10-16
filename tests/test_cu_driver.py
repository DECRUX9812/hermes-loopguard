"""Unit tests for the P2 driver layer (ComputerDriver contract, cua adapter,
FakeDriver).

EVERYTHING here is a unit-test fake: subprocess is patched with recorded
fixtures (tests/fixtures/cu/driver/real_cua_results.json = REAL cua-driver
0.28.2 outputs captured on the bench pages, no secrets) and no live daemon,
display or model is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_loopguard.cu.driver import (
    ActionResult,
    DriverError,
    Element,
    Frame,
    get_driver,
    known_drivers,
    parse_action_result,
    register_driver,
)
from hermes_loopguard.cu.drivers.cua import (
    CuaDriver,
    best_match,
    desktop_to_window_local,
    window_local_to_desktop,
)
from hermes_loopguard.cu.drivers.fake import FakeDriver

FIX = Path(__file__).parent / "fixtures" / "cu" / "driver"
REAL = json.loads((FIX / "real_cua_results.json").read_text())


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_registry_builtins_and_unknown():
    assert "cua" in known_drivers() and "fake" in known_drivers()
    fake = get_driver("fake", fixture={"frames": [], "elements": {}})
    assert fake.name == "fake"
    cua = get_driver("cua", socket="/tmp/does-not-matter.sock")
    assert isinstance(cua, CuaDriver)
    with pytest.raises(DriverError):
        get_driver("definitely-not-a-driver")


def test_registry_register_decorator():
    @register_driver("test-widget")
    class Widget:
        name = "test-widget"

    try:
        assert get_driver("test-widget").name == "test-widget"
    finally:
        from hermes_loopguard.cu import driver as driver_mod

        driver_mod._DRIVERS.pop("test-widget", None)


# --------------------------------------------------------------------------
# coordinate conversion (window-local <-> desktop)
# --------------------------------------------------------------------------

WB = {"x": 20, "y": 24, "width": 1050, "height": 780}


def test_desktop_to_window_local_roundtrip():
    lx, ly = desktop_to_window_local(100, 200, WB)
    assert (lx, ly) == (80, 176)
    assert window_local_to_desktop(lx, ly, WB) == (100, 200)


def test_desktop_to_window_local_outside_is_none():
    assert desktop_to_window_local(19, 100, WB) is None  # left of window
    assert desktop_to_window_local(100, 24 + WB["height"] + 5, WB) is None  # below
    assert desktop_to_window_local(100, 100, {}) is None


def test_window_local_to_desktop_without_bounds():
    assert window_local_to_desktop(5, 6, {}) == (5, 6)
    assert window_local_to_desktop(5, 6, None) == (5, 6)


# --------------------------------------------------------------------------
# effect parsing from REAL recorded cua outputs
# --------------------------------------------------------------------------


def test_parse_click_element_token_real():
    res = parse_action_result(REAL["click_element_token_ok"])
    assert res.ok is True
    assert res.effect == "unverifiable"
    assert res.error is None
    assert res.raw["route"] == "accessibility"


def test_parse_background_unavailable_real():
    """REAL cua failure shape: code+detail, escalation dict -> string."""
    res = parse_action_result(REAL["background_unavailable"])
    assert res.ok is False
    assert res.effect == "error"
    assert res.error.startswith("background_unavailable:")
    assert res.escalation == "foreground"


def test_parse_foreground_delivery_failed_real():
    res = parse_action_result(REAL["foreground_delivery_failed"])
    assert res.effect == "unverifiable"
    # the actionable advice from cua is the delivery mode to retry with
    assert res.escalation == "foreground"


def test_parse_cli_error_envelope():
    res = parse_action_result({"_cua_error": "click exited 1: stale element"})
    assert res.ok is False
    assert res.effect == "error"
    assert "stale" in res.error


def test_parse_non_json_cli_output():
    """REAL: set_value on a chrome entry returns a NON-JSON string on stdout."""
    res = parse_action_result({"_cua_error": "set_value: non-JSON stdout: 'element 75 exposes neither EditableText nor Value'"})
    assert res.ok is False
    assert res.effect == "error"


def test_parse_empty_and_garbage():
    assert parse_action_result({}).effect == "unverifiable"
    garbage = parse_action_result({"effect": "weird"})
    assert garbage.effect == "unverifiable"  # not in EFFECTS -> unverifiable


# --------------------------------------------------------------------------
# CuaDriver subprocess shapes (patched)
# --------------------------------------------------------------------------


class FakeProc:
    """Patched subprocess.run returning queued REAL outputs, recording argv."""

    def __init__(self, script: dict[str, object]):
        self.script = script  # tool name -> stdout string (or callable)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(list(cmd))
        tool = cmd[4]  # [cua-driver, call, --socket, SOCK, TOOL, '<json>']
        entry = self.script.get(tool, "{}")
        stdout = entry(self, cmd) if callable(entry) else entry

        class P:
            returncode = 0
            stderr = ""

        p = P()
        p.stdout = stdout
        return p


@pytest.fixture()
def cua(monkeypatch):
    driver = CuaDriver(socket="/tmp/lg-cu/cua.sock", app="chrome")
    png = (FIX / "frame_form.png").read_bytes()

    def fake_desktop_state(fake, cmd):
        # honour screenshot_out_file like the real CLI: write the PNG there
        payload = json.loads(cmd[5])
        out = payload.get("screenshot_out_file")
        if out:
            Path(out).write_bytes(png)
        return json.dumps(REAL["desktop_state"])

    fake = FakeProc(
        {
            "list_windows": json.dumps(REAL["list_windows"]),
            "get_window_state": json.dumps(REAL["get_window_state_chrome_form"]),
            "get_desktop_state": fake_desktop_state,
            "click": json.dumps(REAL["click_element_token_ok"]),
            "type_text": json.dumps(REAL["click_element_token_ok"]),
            "press_key": json.dumps(REAL["click_element_token_ok"]),
            "hotkey": json.dumps(REAL["click_element_token_ok"]),
            "scroll": json.dumps(REAL["click_element_token_ok"]),
        }
    )
    monkeypatch.setattr(
        "hermes_loopguard.cu.drivers.cua.subprocess.run", fake
    )
    driver._fake_proc = fake
    yield driver


def test_cli_argv_shape(cua):
    """argv must be [cua, call, --socket, SOCK, TOOL, '<json>'] (json via argv)."""
    cua.screenshot()
    cmd = cua._fake_proc.calls[0]
    assert cmd[:3] == ["cua-driver", "call", "--socket"]
    assert cmd[3] == "/tmp/lg-cu/cua.sock"
    assert cmd[4] == "get_desktop_state"
    payload = json.loads(cmd[5])  # 6th arg IS the JSON payload
    assert isinstance(payload, dict)


def test_screenshot_returns_frame(cua):
    frame = cua.screenshot()
    assert isinstance(frame, Frame)
    assert (frame.width, frame.height) == (1280, 800)
    assert frame.png.startswith(b"\x89PNG")  # real bench-page PNG from the fixture
    assert frame.ts > 0


def test_elements_desktop_pixels_from_real_walk(cua):
    els = cua.elements("Google-chrome")
    assert els, "real walk fixture must yield elements"
    window = REAL["list_windows"]["windows"][0]
    wb = window["bounds"]
    for el in els:
        # frames are already desktop-absolute; allow the window scrollbar and
        # other real-world overflow to stick out slightly
        assert wb["x"] - 8 <= el.x and el.x + el.w <= wb["x"] + wb["width"] + 20
        assert wb["y"] - 8 <= el.y and el.y + el.h <= wb["y"] + wb["height"] + 20
    name = cua.find_element("Name")
    assert name is not None and name.role == "entry"
    assert name.center == (name.x + name.w // 2, name.y + name.h // 2)


def test_elements_app_filter(cua):
    assert cua.elements("thunar") == []  # fixture only has Google-chrome windows


def test_click_element_adds_pid(cua):
    el = cua.find_element("Name")
    cua.click_element(el)
    cmd = cua._fake_proc.calls[-1]
    assert cmd[4] == "click"
    payload = json.loads(cmd[5])
    assert payload["element_token"] == el.token
    assert payload["pid"] == REAL["list_windows"]["windows"][0]["pid"]


def test_click_xy_converts_to_window_local(cua):
    """Real fixture window sits at (10,10): desktop (30,34) -> local (20,24)."""
    cua.click(30, 34)
    payload = json.loads(cua._fake_proc.calls[-1][5])
    wb = REAL["list_windows"]["windows"][0]["bounds"]
    assert payload["x"] == 30 - wb["x"] and payload["y"] == 34 - wb["y"]
    assert payload["pid"] == REAL["list_windows"]["windows"][0]["pid"]
    assert payload["window_id"] == REAL["list_windows"]["windows"][0]["window_id"]
    assert payload["button"] == "left"


def test_type_text_targets_window_foreground(cua):
    cua.type_text("Ada Lovelace")
    payload = json.loads(cua._fake_proc.calls[-1][5])
    assert payload["text"] == "Ada Lovelace"
    assert payload["delivery_mode"] == "foreground"
    assert payload["pid"] == REAL["list_windows"]["windows"][0]["pid"]


def test_key_single_vs_combo(cua):
    cua.key("Return")
    assert cua._fake_proc.calls[-1][4] == "press_key"
    assert json.loads(cua._fake_proc.calls[-1][5])["key"] == "Return"
    cua.key("ctrl+a")
    assert cua._fake_proc.calls[-1][4] == "hotkey"
    assert json.loads(cua._fake_proc.calls[-1][5])["keys"] == ["ctrl", "a"]


def test_scroll_clamps_amount(cua):
    cua.scroll(100, 100, "down", amount=99)
    payload = json.loads(cua._fake_proc.calls[-1][5])
    assert payload["amount"] == 50
    assert payload["direction"] == "down"


def test_background_unavailable_auto_escalates(cua, monkeypatch):
    """REAL recorded flow: background refused -> retry with foreground."""
    script = cua._fake_proc.script
    bg = json.dumps(REAL["background_unavailable"])
    fg = json.dumps(REAL["foreground_delivery_failed"])
    seen_modes = []

    def respond(fake, cmd):
        payload = json.loads(cmd[5])
        mode = payload.get("delivery_mode", "background")
        seen_modes.append(mode)
        return fg if mode == "foreground" else bg

    script["click"] = respond
    res = cua.click(100, 100)
    assert seen_modes == ["background", "foreground"]
    assert isinstance(res, ActionResult)


def test_driver_never_raises_on_cli_failure(cua, monkeypatch):
    def boom(cmd, capture_output=True, text=True, timeout=None):
        raise FileNotFoundError("cua-driver binary not found")

    monkeypatch.setattr("hermes_loopguard.cu.drivers.cua.subprocess.run", boom)
    res = cua.click(10, 10)
    assert res.ok is False
    assert res.effect == "error"
    assert "not found" in res.error


def test_screenshot_raises_driver_error_on_failure(cua, monkeypatch):
    def boom(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess_timeout()

    def subprocess_timeout():
        raise AssertionError

    import subprocess as sp

    def boom2(cmd, capture_output=True, text=True, timeout=None):
        raise sp.TimeoutExpired(cmd, 20)

    monkeypatch.setattr("hermes_loopguard.cu.drivers.cua.subprocess.run", boom2)
    with pytest.raises(DriverError):
        cua.screenshot()


# --------------------------------------------------------------------------
# FakeDriver replay
# --------------------------------------------------------------------------


def test_fake_driver_replays_fixture():
    driver = FakeDriver.from_fixture(FIX / "fake_script.json")
    frame = driver.screenshot()
    assert frame.width == 1280 and frame.height == 800
    assert frame.png.startswith(b"\x89PNG")

    els = driver.elements("Google-chrome")
    assert [e.label for e in els] == ["Name", "Email", "Submit"]
    name = driver.find_element("name", role="entry")
    assert name is not None and name.token == "s0000000e:75"

    # recorded action results replay in order and cycle the last one
    el = driver.find_element("Submit")
    r1 = driver.click_element(el)
    assert r1.ok and r1.effect == "confirmed" and r1.verified is True
    r2 = driver.click_element(el)
    assert r2.effect == "confirmed"  # cycles the last recorded row
    r3 = driver.click(10, 20)
    assert r3.ok is True  # first recorded click row
    r4 = driver.click(10, 20)
    assert r4.ok is False and r4.effect == "error"  # recorded failure row
    r5 = driver.click(10, 20)
    assert r5.ok is False  # cycles the failure row

    r6 = driver.type_text("Ada Lovelace")
    assert r6.effect == "unverifiable"
    r7 = driver.key("Return")
    assert r7.effect == "confirmed"

    driver.scroll(5, 5, "down")
    driver.close()
    assert driver.closed

    methods = [c["method"] for c in driver.calls]
    assert methods == [
        "screenshot",
        "elements",   # elements("Google-chrome")
        "elements",   # find_element("name", role="entry")
        "elements",   # find_element("Submit")
        "click_element",
        "click_element",
        "click",
        "click",
        "click",
        "type_text",
        "key",
        "scroll",
        "close",
    ]
    click_args = [c for c in driver.calls if c["method"] == "click"][0]["args"]
    assert click_args == {"x": 10, "y": 20, "button": "left", "count": 1}


def test_fake_driver_missing_frame_raises():
    driver = FakeDriver(fixture={"frames": [], "elements": {}})
    with pytest.raises(ValueError):
        driver.screenshot()


def test_fake_driver_frames_cycle():
    driver = FakeDriver(
        fixture={
            "frames": [
                {"file": "frame_form.png", "width": 1280, "height": 800},
            ],
            "elements": {},
        },
        base_dir=FIX,
    )
    f1 = driver.screenshot()
    f2 = driver.screenshot()  # repeats the last frame
    assert f1.width == f2.width and f2.png == f1.png


# --------------------------------------------------------------------------
# keyboard targeting: type/key must land in the window the agent works in
# (REAL failure: an editor at z0 with its Save As dialog at z1 — typing went
# to the editor because _app_window returned the FIRST window list match)
# --------------------------------------------------------------------------


def _mousepad_windows() -> dict:
    return {
        "windows": [
            {
                "app_name": "Org.xfce.mousepad", "pid": 11, "window_id": 100,
                "title": "*Untitled - Mousepad", "z_index": 0,
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
            },
            {
                "app_name": "Org.xfce.mousepad", "pid": 11, "window_id": 200,
                "title": "Save As", "z_index": 1,
                "bounds": {"x": 100, "y": 100, "width": 900, "height": 600},
            },
        ]
    }


def _driver_over_windows(monkeypatch, windows: dict):
    driver = CuaDriver(socket="/tmp/lg-cu/cua.sock", app="Mousepad")
    fake = FakeProc(
        {
            "list_windows": json.dumps(windows),
            "click": json.dumps(REAL["click_element_token_ok"]),
            "type_text": json.dumps(REAL["click_element_token_ok"]),
            "press_key": json.dumps(REAL["click_element_token_ok"]),
            "hotkey": json.dumps(REAL["click_element_token_ok"]),
            "bring_to_front": json.dumps(REAL["click_element_token_ok"]),
        }
    )
    monkeypatch.setattr("hermes_loopguard.cu.drivers.cua.subprocess.run", fake)
    driver._fake_proc = fake
    return driver


def test_app_window_returns_topmost_match(monkeypatch):
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    win = driver._app_window()
    assert win["window_id"] == 200  # the Save As dialog, not the editor


def test_type_text_after_click_targets_clicked_dialog(monkeypatch):
    """click into the dialog -> type lands in the DIALOG (window-local click
    was recorded), and no activate runs (the dialog is already topmost)."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    driver.click(600, 300)  # covered by BOTH windows: the dialog is topmost
    driver.type_text("/tmp/lg-cu/files/editor-task/hello.txt")
    payload = json.loads(driver._fake_proc.calls[-1][5])
    assert payload["pid"] == 11
    assert payload["window_id"] == 200
    # the dialog is already topmost: activating would steal focus, none ran
    tools = [c[4] for c in driver._fake_proc.calls]
    assert "bring_to_front" not in tools


def test_type_text_goes_to_topmost_window_even_after_background_click(monkeypatch):
    """Clicking into a covered background window must NOT aim the typing
    there: the app's topmost window (the modal Save As dialog) owns the
    input, and activating the background editor would steal its focus."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    driver.click(50, 50)  # only the editor (z0) covers this point
    driver.type_text("Hello from LoopGuard")
    payload = json.loads(driver._fake_proc.calls[-1][5])
    assert payload["window_id"] == 200  # the dialog, not the clicked editor


def test_key_targets_clicked_window_too(monkeypatch):
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    driver.click(600, 300)
    driver.key("ctrl+a")
    payload = json.loads(driver._fake_proc.calls[-1][5])
    assert payload["window_id"] == 200


def test_global_input_background_click_retries_foreground(monkeypatch):
    """REAL failure: cua-driver 0.28.2 queues window-targeted clicks as
    route 'global_input' in background delivery and the stack SILENTLY DROPS
    them (thunar icon clicks, chrome <select> popup items) — no error code,
    nothing changes on screen.  The adapter must retry in foreground."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    script = driver._fake_proc.script

    def respond(fake, cmd):
        payload = json.loads(cmd[5])
        if payload.get("delivery_mode") == "foreground":
            return json.dumps(
                {"delivery": {"mode": "foreground"}, "effect": "unverifiable",
                 "route": "global_input"}
            )
        return json.dumps(
            {"delivery": {"mode": "background"}, "effect": "unverifiable",
             "route": "global_input"}
        )

    script["click"] = respond
    driver.click(600, 300)
    clicks = [c for c in driver._fake_proc.calls if c[4] == "click"]
    assert len(clicks) == 2  # background attempt + foreground retry
    first = json.loads(clicks[0][5])
    second = json.loads(clicks[1][5])
    assert "delivery_mode" not in first or first["delivery_mode"] != "foreground"
    assert second["delivery_mode"] == "foreground"


def test_accessibility_route_background_click_is_not_retried(monkeypatch):
    """Chrome main-tree clicks work in background (route 'accessibility');
    a foreground retry would double-click them."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    script = driver._fake_proc.script
    script["click"] = json.dumps(
        {"delivery": {"mode": "background"}, "effect": "unverifiable",
         "route": "accessibility"}
    )
    driver.click(600, 300)
    clicks = [c for c in driver._fake_proc.calls if c[4] == "click"]
    assert len(clicks) == 1  # no retry: the background click really delivered


# --------------------------------------------------------------------------
# label matching specificity + capture robustness (X7)
# --------------------------------------------------------------------------


def test_match_score_ranks_specificity():
    score = CuaDriver._match_score
    assert score("search", "Search") == 4  # exact
    assert score("search", "Search…") == 3  # label extends the target
    assert score("dark mode switch", "Dark mode") == 1  # target over-specifies
    assert score("name", "Address") == -1  # no containment either way
    assert score("", "anything") == -1
    assert score("x", "") == -1


def test_find_element_prefers_exact_over_substring(monkeypatch):
    """REAL failure (search-pick-confirm r4): find_element('Search') returned
    the INPUT (placeholder 'Search…') because plain substring matching took
    the first walk-order hit — the click went into the input and the step did
    nothing, twice.  The exact label must win."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    els = [
        Element(token="in", role="entry", label="Search…", x=0, y=0, w=200, h=30),
        Element(token="btn", role="push button", label="Search", x=0, y=40, w=90, h=30),
    ]
    monkeypatch.setattr(driver, "elements", lambda app=None: els)
    hit = driver.find_element("Search")
    assert hit is not None and hit.token == "btn"


def test_find_element_reverse_containment_matches_over_specified_target(monkeypatch):
    """REAL failure (settings-gtk r1): the planner asked for 'Dark mode
    switch' but the checkbox's label is 'Dark mode' — plain substring
    matching found NOTHING, grounding failed three times and the run
    escalated.  A label fully contained in the target must match."""
    driver = _driver_over_windows(monkeypatch, _mousepad_windows())
    els = [Element(token="sw", role="check box", label="Dark mode", x=0, y=0, w=120, h=30)]
    monkeypatch.setattr(driver, "elements", lambda app=None: els)
    hit = driver.find_element("Dark mode switch")
    assert hit is not None and hit.token == "sw"
    assert driver.find_element("Nothing like this") is None


def test_match_score_reverse_containment_requires_word_boundary():
    """X8: target 'Username field' must not match the label 'Name' hiding
    INSIDE the word 'Username' — a substring-only reverse containment ties
    with the real 'Username' label and walk order picked 'Name'."""
    score = CuaDriver._match_score
    assert score("username field", "Name") == -1  # 'name' inside 'username'
    assert score("username field", "Username") == 1  # whole word: matches
    assert score("dark mode switch", "Dark mode") == 1  # still matches on boundary


def test_best_match_prefers_longer_label_on_tie():
    """X8: two reverse-containment hits ('Username', 'field') must not be
    decided by walk order — the LONGER label is the more specific match."""
    els = [
        Element(token="f", role="entry", label="field", x=0, y=0, w=50, h=30),
        Element(token="u", role="entry", label="Username", x=0, y=40, w=50, h=30),
    ]
    assert best_match(els, "Username field").token == "u"


def test_best_match_role_filter():
    els = [
        Element(token="lab", role="label", label="Name", x=0, y=0, w=50, h=30),
        Element(token="ent", role="entry", label="Name", x=0, y=40, w=50, h=30),
    ]
    assert best_match(els, "Name", role="entry").token == "ent"
    assert best_match(els, "Name", role="push button") is None


def test_screenshot_retries_once_on_capture_error(cua):
    """REAL failure (form-widgets r1): one transient cua capture error
    ("get_desktop_state: non-JSON stdout: 'Capture err...'") took the whole
    run down.  One retry after a short settle must absorb it."""
    png = (FIX / "frame_form.png").read_bytes()
    state = {"n": 0}

    def capture(fake, cmd):
        state["n"] += 1
        payload = json.loads(cmd[5])
        if state["n"] == 1:
            return json.dumps(
                {"_cua_error": "get_desktop_state: non-JSON stdout: 'Capture err...'"}
            )
        Path(payload["screenshot_out_file"]).write_bytes(png)
        return json.dumps(REAL["desktop_state"])

    cua._fake_proc.script["get_desktop_state"] = capture
    frame = cua.screenshot()
    assert state["n"] == 2
    assert frame.png.startswith(b"\x89PNG")


def test_screenshot_raises_after_repeated_capture_errors(cua):
    def capture(fake, cmd):
        return json.dumps(
            {"_cua_error": "get_desktop_state: non-JSON stdout: 'Capture err...'"}
        )

    cua._fake_proc.script["get_desktop_state"] = capture
    with pytest.raises(DriverError):
        cua.screenshot()
    attempts = [c for c in cua._fake_proc.calls if c[4] == "get_desktop_state"]
    assert len(attempts) == 2  # exactly one retry, then give up
