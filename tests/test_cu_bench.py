"""X4 bench unit tests: pricing guards, JSON action parsing, screenshot->desktop
coordinate mapping, and summary math.

The driver in these tests is the FakeDriver replay fixture (UNIT-TEST FAKE,
never used outside tests) and the model transport is monkeypatched — no
network, no screenshots, no spend.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BENCH = REPO / "examples" / "cu_bench"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(REPO / "src"))

import baseline  # noqa: E402
from hermes_loopguard.cu import pricing  # noqa: E402
from run_bench import aggregate, flag_fallback_rows, non_jev_vision, preflight_jev  # noqa: E402


# --- pricing -------------------------------------------------------------------


def test_usd_known_model_math():
    # (in $/Mtok, out $/Mtok) applied to tokens
    assert pricing.usd("claude-opus-4-6-thinking", 1_000_000, 1_000_000) == pytest.approx(30.0)
    assert pricing.usd("jev-1.14.0", 1_000_000, 999_999_999) == pytest.approx(0.042)


def test_usd_existing_entries_unchanged():
    assert pricing.USD_PER_MTOK["jev-1.14.0"] == (0.042, 0.0)
    assert pricing.USD_PER_MTOK["gemini-3.8-flash-high"] == (0.75, 3.75)


def test_planner_model_is_priced():
    from hermes_loopguard.config import DEFAULTS

    planner = DEFAULTS["computer_use"]["planner_model"]
    assert pricing.is_priced(planner), f"planner model {planner!r} has no price row"


def test_unpriced_models_flagged():
    assert pricing.unpriced_models(["jev-1.14.0", "claude-opus-4-6-thinking"]) == []
    assert pricing.unpriced_models(["totally-unknown-model", "jev-1.14.0"]) == [
        "totally-unknown-model"
    ]
    # zero-token calls to unpriced models must still be flagged (the point of
    # the guard: unknown must not read as free)
    assert pricing.usd("totally-unknown-model", 5000, 1000) == 0.0
    assert not pricing.is_priced("totally-unknown-model")


# --- baseline JSON action parsing -----------------------------------------------


def test_parse_action_plain_json():
    action = baseline.parse_action('{"action": "click", "x": 10, "y": 20, "reason": "ok"}')
    assert action == {"action": "click", "x": 10, "y": 20, "reason": "ok"}


def test_parse_action_prose_and_fence():
    wrapped = 'Thinking...\n```json\n{"action": "done", "reason": "form submitted"}\n```'
    assert baseline.parse_action(wrapped)["action"] == "done"


def test_parse_action_invalid():
    assert baseline.parse_action("") is None
    assert baseline.parse_action("no json at all") is None
    assert baseline.parse_action('{"action": "rm_rf_everything"}') is None
    assert baseline.parse_action('{"action": "click"') is None  # truncated


# --- screenshot px -> desktop px mapping -----------------------------------------


def test_map_point_identity_when_sizes_match():
    assert baseline.map_point(640, 400, 1280, 800, 1280, 800) == (640, 400)


def test_map_point_downscaled_screenshot():
    # 1280x800 desktop shown at 1024x640: factor 1.25 both axes
    assert baseline.map_point(512, 320, 1024, 640, 1280, 800) == (640, 400)


def test_map_point_clamps_to_desktop():
    assert baseline.map_point(9999, 9999, 1024, 640, 1280, 800) == (1279, 799)
    assert baseline.map_point(-5, -5, 1024, 640, 1280, 800) == (0, 0)


# --- one baseline turn on the FakeDriver (UNIT-TEST FAKE) -------------------------


def _png(width: int = 512, height: int = 300) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def fake_driver():
    from hermes_loopguard.cu.drivers.fake import FakeDriver

    fixture = {
        "frames": [{"png_b64": base64_of(_png()), "width": 512, "height": 300}],
        "elements": {},
        "actions": {"click": [{"ok": True, "effect": "unverifiable"}]},
    }
    return FakeDriver(fixture)


def base64_of(png: bytes) -> str:
    import base64

    return base64.b64encode(png).decode("ascii")


def test_baseline_runs_click_then_done(fake_driver, monkeypatch):
    replies = [
        ('{"action": "click", "x": 100, "y": 50, "reason": "the button"}', 1000, 40),
        ('{"action": "done", "reason": "task complete"}', 800, 20),
    ]

    def fake_chat(model, system, png, history):
        content, inp, out = replies.pop(0)
        return content, {"prompt_tokens": inp, "completion_tokens": out}, {}

    monkeypatch.setattr(baseline, "chat_with_image", fake_chat)
    result = baseline.run_baseline("click the button", fake_driver, max_steps=5)

    assert result.state == "completed"
    assert result.steps == 1
    clicks = [c for c in fake_driver.calls if c["method"] == "click"]
    assert len(clicks) == 1
    assert clicks[0]["args"]["x"] == 100 and clicks[0]["args"]["y"] == 50  # 1:1 sizes
    assert result.input_tokens == 1800 and result.output_tokens == 60
    assert result.calls_per_model == {"claude-opus-4-6-thinking": 2}
    assert result.big_model_calls == 2
    assert result.usd == pytest.approx(pricing.usd("claude-opus-4-6-thinking", 1800, 60))


def test_baseline_maps_downscaled_coords(fake_driver, monkeypatch):
    """Desktop 1024x600, model saw it at half size (512x300) -> coords double."""
    from hermes_loopguard.cu import frames as frames_mod
    from hermes_loopguard.cu.drivers.fake import FakeDriver

    prepared, _w, _h = frames_mod.prepare_image(_png(1024, 600))  # <=1568, unchanged
    seen_w, seen_h = 512, 300
    driver = FakeDriver(
        {
            "frames": [{"png_b64": base64_of(_png(1024, 600)), "width": 1024, "height": 600}],
            "elements": {},
            "actions": {},
        }
    )

    def fake_chat(model, system, png, history):
        return '{"action": "click", "x": 256, "y": 150, "reason": "r"}', {
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }, {}

    monkeypatch.setattr(baseline, "chat_with_image", fake_chat)
    monkeypatch.setattr(baseline.frames, "prepare_image", lambda png: (prepared, seen_w, seen_h))
    result = baseline.run_baseline("click", driver, max_steps=1)
    click = next(c for c in driver.calls if c["method"] == "click")
    assert click["args"]["x"] == 512 and click["args"]["y"] == 300
    assert result.state == "max_steps"  # one turn, model kept going (no done)


def test_baseline_ask_human_pauses(fake_driver, monkeypatch):
    def fake_chat(model, system, png, history):
        return '{"action": "ask_human", "reason": "confirm payment needed"}', {
            "prompt_tokens": 5,
            "completion_tokens": 5,
        }, {}

    monkeypatch.setattr(baseline, "chat_with_image", fake_chat)
    result = baseline.run_baseline("task", fake_driver, max_steps=3)
    assert result.state == "paused_for_approval"
    assert result.steps == 0  # ask_human is not an executed action


def test_baseline_budget_stops_the_loop(fake_driver, monkeypatch):
    def fake_chat(model, system, png, history):
        # every turn wants another click; cost per call: 1M in-tok = $5
        return '{"action": "click", "x": 1, "y": 1, "reason": "r"}', {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
        }, {}

    monkeypatch.setattr(baseline, "chat_with_image", fake_chat)
    result = baseline.run_baseline("task", fake_driver, max_steps=10, budget_usd=10.0)
    assert result.state == "budget_exhausted"
    assert len(result.turns) == 2  # two $5 turns cross the $10 cap before turn 3


# --- summary math -----------------------------------------------------------------


def _row(task, arm, success, usd, steps=5, wall=10.0, big=1):
    return {
        "task": task,
        "arm": arm,
        "repeat": 1,
        "success": success,
        "steps": steps,
        "wall_s": wall,
        "input_tokens": 100,
        "output_tokens": 10,
        "usd": usd,
        "big_model_calls": big,
        "calls_per_model": {"m": 1},
    }


def test_aggregate_counts_and_averages():
    rows = [
        _row("a", "baseline", True, 2.0),
        _row("b", "baseline", False, 1.0),
        _row("a", "jev", True, 0.02),
        _row("b", "jev", True, 0.03),
    ]
    agg = aggregate(rows)
    base, jev = agg["baseline"], agg["jev"]
    assert base["attempts"] == 2 and base["successes"] == 1
    assert base["usd"] == pytest.approx(3.0)
    assert base["usd_per_success"] == pytest.approx(3.0)
    assert base["avg_steps"] == pytest.approx(5.0)
    assert jev["attempts"] == 2 and jev["successes"] == 2
    assert jev["usd"] == pytest.approx(0.05)
    assert jev["usd_per_success"] == pytest.approx(0.025)
    # per-(task, arm) cells
    assert base["tasks"]["a"]["successes"] == 1
    assert base["tasks"]["b"]["successes"] == 0
    assert jev["tasks"]["b"]["successes"] == 1


def test_aggregate_no_success_is_none_not_divzero():
    agg = aggregate([_row("a", "baseline", False, 1.0)])
    assert agg["baseline"]["usd_per_success"] is None


def test_aggregate_success_bar_math():
    """The BENCH bar: jev cost <= 10% of baseline with >= successes."""
    rows = [
        _row(f"t{i}", "baseline", True, 0.5) for i in range(4)
    ] + [
        _row("t0", "jev", True, 0.01),
        _row("t1", "jev", True, 0.01),
        _row("t2", "jev", False, 0.01),  # one jev failure: fewer successes
        _row("t3", "jev", True, 0.01),
    ]
    agg = aggregate(rows)
    ratio = agg["jev"]["usd"] / agg["baseline"]["usd"]
    assert ratio == pytest.approx(0.02)  # cheap enough
    assert agg["jev"]["successes"] < agg["baseline"]["successes"]  # ...but bar NOT met


# --- Jev-arm preflight (silent fallback must be impossible) ---------------------


def _ok_import():
    return object()


def _ok_ping(model="jev-1.14.0", timeout=45.0):
    return model


def test_preflight_jev_happy_path_does_not_exit():
    # No SystemExit: sdk importable, key present, tiny call serves jev-1.14.0.
    preflight_jev(_import_sdk=_ok_import, _ping=_ok_ping, _key="sk-test")


def test_preflight_jev_fails_without_sdk():
    def no_sdk():
        raise ImportError("No module named 'typesafe_sdk'")

    with pytest.raises(SystemExit) as exc:
        preflight_jev(_import_sdk=no_sdk, _ping=_ok_ping, _key="sk-test")
    assert "typesafe-sdk is not importable" in str(exc.value)
    assert "typesafe-sdk, pillow" in str(exc.value)  # names the fix (extras + PYTHONPATH)


def test_preflight_jev_fails_without_key():
    with pytest.raises(SystemExit) as exc:
        preflight_jev(_import_sdk=_ok_import, _ping=_ok_ping, _key="")
    assert "TYPESAFE_API_KEY" in str(exc.value)


def test_preflight_jev_fails_when_tiny_call_fails():
    def broken_ping(model="jev-1.14.0", timeout=45.0):
        raise RuntimeError("TypeSafe System One call failed: 401")

    with pytest.raises(SystemExit) as exc:
        preflight_jev(_import_sdk=_ok_import, _ping=broken_ping, _key="sk-test")
    assert "jev-1.14.0 call did not succeed" in str(exc.value)


def test_preflight_jev_fails_when_fallback_model_answers():
    def fallback_ping(model="jev-1.14.0", timeout=45.0):
        return "gemini-3.8-flash-high"  # the silent-fallback signature

    with pytest.raises(SystemExit) as exc:
        preflight_jev(_import_sdk=_ok_import, _ping=fallback_ping, _key="sk-test")
    assert "served by" in str(exc.value)
    assert "gemini-3.8-flash-high" in str(exc.value)


def test_comm_match_is_case_insensitive():
    """REAL failure: mousepad's /proc comm is 'mousepad', so a case-sensitive
    startswith('Mousepad') never killed it — the editor and its modal Save As
    dialog survived every task reset and haunted later tasks."""
    from run_bench import _comm_matches

    assert _comm_matches("mousepad", "Mousepad")
    assert _comm_matches("Mousepad", "mousepad")
    assert _comm_matches("MOUSEPAD", "mousepad")
    assert not _comm_matches("chrome", "mousepad")


# --- --summary fallback flags ----------------------------------------------------


def test_non_jev_vision_only_counts_non_jev_models():
    row = {
        "vision_calls_by_model": {"jev-1.14.0": 7, "gemini-3.8-flash-high": 3},
        "fallbacks": ["jev-1.14.0: 400 bad image"],
    }
    assert non_jev_vision(row) == {"gemini-3.8-flash-high": 3}


def test_non_jev_vision_empty_for_baseline_rows():
    # Baseline rows carry no vision_calls_by_model at all: nothing to flag.
    assert non_jev_vision({"calls_per_model": {"claude-opus-4-6-thinking": 5}}) == {}


def test_flag_fallback_rows_prints_and_counts():
    rows = [
        {
            "arm": "jev",
            "task": "contact-form-simple",
            "repeat": 1,
            "vision_calls_by_model": {"gemini-3.8-flash-high": 9},
            "fallbacks": ["jev-1.14.0: 401 unauthorized"],
            "big_model_calls": 0,
        },
        {
            "arm": "jev",
            "task": "form-widgets",
            "repeat": 1,
            "vision_calls_by_model": {"jev-1.14.0": 8},
            "fallbacks": [],
            "big_model_calls": 0,
        },
    ]
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        flagged = flag_fallback_rows(rows)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert flagged == 1
    assert "NON-JEV vision call(s)" in out
    assert "gemini-3.8-flash-high" in out
    assert "silent fallback" in out
    assert "contact-form-simple" in out
    assert "form-widgets" not in out  # clean row is not flagged


def test_flag_fallback_rows_clean_run_reports_zero():
    rows = [
        {
            "arm": "jev",
            "task": "t",
            "repeat": 1,
            "vision_calls_by_model": {"jev-1.14.0": 5},
            "fallbacks": [],
        }
    ]
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        assert flag_fallback_rows(rows) == 0
    finally:
        sys.stdout = old
    assert buf.getvalue() == ""


def test_agent_runresult_records_vision_calls_and_fallbacks():
    """The jev arm's row fields come from RunResult: vision calls per model
    (planner excluded) and fallback reasons from VisionUsage.fallbacks."""
    from hermes_loopguard.cu.agent import _Totals
    from hermes_loopguard.cu.vision import VisionUsage

    totals = _Totals()
    planner = VisionUsage(backend="openai", model="gemini-3.1-flash-lite", input_tokens=100, output_tokens=5)
    gate = VisionUsage(backend="typesafe", model="jev-1.14.0", input_tokens=900, output_tokens=10)
    fell_back = VisionUsage(
        backend="typesafe",
        model="gemini-3.8-flash-high",
        input_tokens=200,
        output_tokens=4,
        fallbacks=["jev-1.14.0: 500 oops"],
    )
    totals.add(planner, vision=False)  # planner text call: not a vision call
    totals.add(gate)
    totals.add(fell_back)
    assert totals.vision_calls == {"jev-1.14.0": 1, "gemini-3.8-flash-high": 1}
    assert totals.calls == {
        "gemini-3.1-flash-lite": 1,
        "jev-1.14.0": 1,
        "gemini-3.8-flash-high": 1,
    }
    assert totals.fallbacks == ["jev-1.14.0: 500 oops"]
    failed = VisionUsage(backend="", model="", fallbacks=["jev-1.14.0: timeout"])
    totals.add(failed)
    assert totals.fallbacks[-1] == "jev-1.14.0: timeout"
    assert "failed" not in totals.vision_calls  # no model answered: not a call


# --- launcher disposable HOME (X7 privacy) -------------------------------------


def test_launcher_env_forces_disposable_identity():
    """REAL bench bug: the apps ran with the REAL home, so Thunar's sidebar
    showed the account name and Mousepad's Save As listed the real home
    directory — on screenshots sent to the vision models.  The launcher must
    force a disposable HOME/identity that the request cannot override."""
    import launcher

    base = {
        "DISPLAY": ":97",
        "HOME": "/home/bench",
        "USER": "bench",
        "LOGNAME": "bench",
        "XDG_RUNTIME_DIR": "/tmp/lg-cu/runtime",  # private session bus runtime
    }
    env = launcher.build_bench_env(
        base, "/tmp/lg-cu/home", {"FOO": "bar", "HOME": "/home/bench", "USER": "bench"}
    )
    assert env["HOME"] == "/tmp/lg-cu/home"
    assert env["XDG_CONFIG_HOME"] == "/tmp/lg-cu/home/.config"
    assert env["XDG_DATA_HOME"] == "/tmp/lg-cu/home/.local/share"
    assert env["XDG_CACHE_HOME"] == "/tmp/lg-cu/home/.cache"
    assert env["USER"] == "bench" and env["LOGNAME"] == "bench"
    assert env["FOO"] == "bar"  # non-privacy request env still applies
    assert env["DISPLAY"] == ":97" and env["XDG_RUNTIME_DIR"] == "/tmp/lg-cu/runtime"


def test_launcher_reset_disposable_home_wipes_and_recreates(tmp_path):
    import launcher

    root = tmp_path / "home"
    (root / "stale-task-leftover").mkdir(parents=True)
    (root / "stale-file.txt").write_text("old")
    launcher.reset_disposable_home(str(root))
    for sub in ("", ".config", ".local/share", ".cache"):
        assert (root / sub).is_dir()
    assert not (root / "stale-task-leftover").exists()
    assert not (root / "stale-file.txt").exists()


def test_launcher_cwd_defaults_to_disposable_home():
    """REAL bench bug (found in the live privacy proof): the launcher inherited
    systemd's cwd (/home/bench) and GTK's Save As dialog opens at the process
    cwd — the real home directory (with real file names) appeared on screen
    even though HOME was disposable."""
    import launcher

    assert launcher.resolve_cwd(None, "/tmp/lg-cu/home") == "/tmp/lg-cu/home"
    assert launcher.resolve_cwd("/tmp/lg-cu/files/fm-task", "/tmp/lg-cu/home") == (
        "/tmp/lg-cu/files/fm-task"
    )


# --- X8: per-task reset kills every tracked app; HOME resets once per task ---


def test_launcher_kill_tracked_kills_the_whole_process_group(tmp_path):
    """X7 leak: a Mousepad stuck in its modal Save-As dialog ignored SIGTERM,
    and the dialog haunted later tasks' screenshots.  The launcher must track
    PIDs/process GROUPS and escalate to SIGKILL — the group kill takes child
    processes (renderers, dialogs) with the parent."""
    import os
    import subprocess

    import launcher

    proc = subprocess.Popen(
        ["bash", "-c", "sleep 60 & wait"],  # a child sharing the process group
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
        tracked = {proc.pid: proc}
        killed, alive = launcher.kill_tracked(tracked, grace_s=2.0)
        assert killed == [proc.pid]
        assert alive == []
        assert tracked == {}  # reaped and dropped
        # the whole group (parent + child sleep) is gone
        assert subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True).returncode != 0
    finally:
        proc.kill()


def test_launcher_spawn_keeps_home_and_tracks_but_reset_kills_and_wipes(tmp_path):
    """X8: reset_disposable_home must run ONCE PER TASK (on the reset
    request), not per spawn — a second app launched in the same task used to
    lose the first app's files.  The reset request also kills every tracked
    app."""
    import launcher

    home_root = tmp_path / "home"
    log_path = tmp_path / "apps.log"
    launcher.reset_disposable_home(str(home_root))
    (home_root / "first-app-file.txt").write_text("app one wrote this")
    tracked: dict = {}
    resp = launcher.handle_request(
        {"argv": ["sleep", "30"]},
        base_env={"DISPLAY": ":97", "HOME": "/home/bench"},
        home_root=str(home_root),
        log_path=str(log_path),
        tracked=tracked,
    )
    try:
        assert "pid" in resp
        assert resp["pid"] in tracked  # the app is tracked for the task reset
        # spawning did NOT wipe the first app's files
        assert (home_root / "first-app-file.txt").exists()
        # and the app really runs with the disposable identity
        with open(f"/proc/{resp['pid']}/environ", "rb") as fh:
            env = fh.read().split(b"\0")
        assert b"HOME=" + str(home_root).encode() in env
        assert b"USER=bench" in env

        resp = launcher.handle_request(
            {"reset": True},
            base_env={},
            home_root=str(home_root),
            log_path=str(log_path),
            tracked=tracked,
        )
        assert resp["reset"] == "ok"
        assert resp["killed"] and not resp["alive"]
        assert tracked == {}
        assert not (home_root / "first-app-file.txt").exists()  # wiped once per task
        for sub in ("", ".config", ".local/share", ".cache"):
            assert (home_root / sub).is_dir()
    finally:
        for proc in tracked.values():
            proc.kill()


def test_launcher_handle_request_survives_bad_requests(tmp_path):
    import launcher

    tracked: dict = {}
    for req in ({}, {"argv": []}, {"argv": "not-a-list"}):
        resp = launcher.handle_request(
            req,
            base_env={},
            home_root=str(tmp_path / "home"),
            log_path=str(tmp_path / "apps.log"),
            tracked=tracked,
        )
        assert "error" in resp
    assert tracked == {}


# --- X8: app readiness + infra-error bookkeeping ------------------------------


def test_frame_is_blank_distinguishes_painted_from_uniform_frames():
    from io import BytesIO

    from PIL import Image

    import run_bench

    def png(color, rect=False):
        img = Image.new("RGB", (320, 200), color)
        if rect:
            for x in range(40, 280):
                for y in range(40, 160):
                    img.putpixel((x, y), (255, 255, 255))
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    assert run_bench.frame_is_blank(png((0, 0, 0)))  # the P6B black frame
    assert run_bench.frame_is_blank(png((40, 40, 60)))
    assert not run_bench.frame_is_blank(png((40, 40, 60), rect=True))


def test_wait_app_ready_waits_for_map_a11y_and_paint(tmp_path):
    """X8: the bench must not start an arm on a half-mapped desktop — wait
    until the app window exposes a11y elements AND the frame is painted."""
    import types

    from io import BytesIO

    from PIL import Image

    import run_bench

    def png(color, rect=False):
        img = Image.new("RGB", (80, 60), color)
        if rect:
            for x in range(8, 72):
                for y in range(8, 52):
                    img.putpixel((x, y), (255, 255, 255))
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    class FakeReadyDriver:
        def __init__(self):
            self.probes = 0
            self.screens = 0

        def elements(self):
            self.probes += 1
            return [object()] if self.probes >= 2 else []  # mapped on poll 2

        def screenshot(self):
            self.screens += 1
            blank = self.probes < 3  # first a11y exposure still paints blank
            return types.SimpleNamespace(png=png((0, 0, 0), rect=not blank))

        def close(self):
            pass

    drv = FakeReadyDriver()
    assert run_bench.wait_app_ready("Settings_app", driver=drv, timeout_s=10) is None
    assert drv.probes >= 3 and drv.screens >= 2


def test_wait_app_ready_times_out_with_a_clear_error():
    import run_bench

    class NeverReady:
        def elements(self):
            return []

        def screenshot(self):  # pragma: no cover - never reached
            raise AssertionError("should not screenshot without elements")

        def close(self):
            pass

    err = run_bench.wait_app_ready("Mousepad", driver=NeverReady(), timeout_s=2.0, poll_s=0.2)
    assert err is not None
    assert "Mousepad" in err and "not ready" in err and "a11y" in err


def test_display_empty_check_ignores_wm_and_cursor_overlay():
    """The display-empty check must not treat the window manager or cua-driver's
    own pid-less cursor overlay as leaked bench apps (X8: every reset
    otherwise reports a bogus leftover)."""
    import run_bench

    assert not run_bench._is_bench_app_window({"app_name": "xfwm4", "pid": 100, "title": "Desktop"})
    assert not run_bench._is_bench_app_window({"app_name": "", "pid": None, "title": "Cua.AgentCursorOverlay.default"})
    assert not run_bench._is_bench_app_window({"app_name": "Cua", "pid": None, "title": "cursor"})
    assert run_bench._is_bench_app_window({"app_name": "Mousepad", "pid": 222, "title": "Untitled"})
    assert run_bench._is_bench_app_window({"app_name": "Thunar", "pid": 223, "title": "fm-task"})


def test_infra_error_reason_marks_infra_not_task_failures():
    import run_bench

    # baseline: every endpoint failed -> infra
    row = {"terminal_state": "error", "reason": "every model failed: gemini-3.8-flash-high: timed out"}
    assert "every model failed" in run_bench.infra_error_reason(row)
    # baseline: the run died on the last model call (no action taken)
    row = {
        "terminal_state": "max_steps",
        "reason": "max_steps=10 reached",
        "trace": [
            {"turn": 9, "action": "click", "error": "claude-opus-4-6-thinking: HTTP Error 429"},
            {"turn": 10, "action": "", "error": "gemini-3.8-flash-high: timed out"},
        ],
    }
    assert "timed out" in run_bench.infra_error_reason(row)
    # driver capture error, either terminal shape
    assert run_bench.infra_error_reason(
        {"terminal_state": "error", "reason": "driver screenshot failed: get_desktop_state failed"}
    )
    assert run_bench.infra_error_reason(
        {"terminal_state": "blocked", "reason": "driver screenshot failed: boom"}
    )
    # genuine task failure: no infra marker
    assert run_bench.infra_error_reason(
        {"terminal_state": "max_steps", "reason": "max_steps=12 reached",
         "trace": [{"turn": 12, "action": "click", "error": ""}]}
    ) == ""
    # a row that never ran (app not ready)
    assert run_bench.infra_error_reason({"terminal_state": "infra_error", "reason": "app not ready"})
    # jev arm: the failing step never got a locate answer (TypeSafe 400s +
    # fallback timeouts) -> infrastructure, not agent behavior
    row = {
        "terminal_state": "blocked",
        "reason": "step type on 'Name' failed twice",
        "fallbacks": [
            "jev-1.14.0: TypeSafe System One call failed: 400 Invalid image dimensions (retrying once)",
            "jev-1.14.0: TypeSafe System One call failed: 400 Invalid image dimensions",
            "openai:gemini-3.8-flash-high: OpenAICompat request failed: timed out",
        ],
        "trace": [
            {"idx": 0, "action": "type", "verdict": "pass", "effect": "unverifiable"},
            {"idx": 1, "action": "type", "verdict": "fail", "effect": "grounding failed"},
        ],
    }
    infra = run_bench.infra_error_reason(row)
    assert "image dimensions" in infra and "timeout" in infra
    assert "gemini-3.8-flash-high" in infra
    # the same endpoint noise on a row that ultimately PASSED is not infra
    assert run_bench.infra_error_reason({**row, "success": True}) == ""
    # ...but a grounding failure with NO endpoint errors is a task failure
    row_no_fb = {**row, "fallbacks": []}
    assert run_bench.infra_error_reason(row_no_fb) == ""


def test_aggregate_reports_infra_errors_separately():
    rows = [
        _row("a", "baseline", False, 1.0),
        {**_row("b", "baseline", False, 1.0), "infra_error": "model endpoint failed: timed out"},
        _row("a", "jev", True, 0.01),
    ]
    agg = aggregate(rows)
    assert agg["baseline"]["infra_errors"] == 1
    assert agg["baseline"]["successes"] == 0
    assert agg["jev"]["infra_errors"] == 0
