"""X5: computer-use dashboard routes + panel.

Route behaviour is tested against a temp store seeded through
``cu/log.py`` (the real writer), including the frame-serving containment
rules: ONLY files the cu_steps row points to, resolved inside the loopguard
data dir, are served.  The overlay math is extracted from the hand-written
IIFE and evaluated under node when node is available.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from hermes_loopguard.cu.log import CuRunLogger
from hermes_loopguard.dashboard import plugin_api
from hermes_loopguard.db import Store

JS_PATH = Path("src/hermes_loopguard/dashboard/dist/index.js")


def _png_bytes(width: int = 40, height: int = 30) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), (180, 40, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _rec(**overrides):
    base = dict(
        idx=0,
        action="click",
        target="Submit",
        grounding="box",
        box=(0.05, 0.57, 0.12, 0.63),
        point=(109, 480),
        verdict="pass",
        noul=0.98,
        effect="confirmed",
        stall="",
        safety="none",
        usages=[],
        frame_before=None,
        frame_after=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def seeded(tmp_path):
    """One finished run (2 steps: one grounded+verified, one safety pause)
    and one run with no steps, written through CuRunLogger."""
    store = Store.open_default()  # conftest points LOOPGUARD_DB at tmp_path
    logger = CuRunLogger(store)
    run_id = logger.start_run("jev_computer", "fill the form, then submit")
    png = _png_bytes()
    frame_before = logger.save_frame(run_id, 0, "before", SimpleNamespace(png=png))
    frame_after = logger.save_frame(run_id, 0, "after", SimpleNamespace(png=png))

    rec1 = _rec(
        idx=0,
        frame_before=frame_before,
        frame_after=frame_after,
    )
    logger.record_step(run_id, rec1)
    rec2 = _rec(
        idx=1,
        action="click",
        target="Pay",
        grounding="none",
        box=None,
        point=None,
        verdict="paused",
        noul=None,
        effect="",
        stall="repeat_screen",
        safety="payment",
    )
    logger.record_step(run_id, rec2)
    logger.finish_run(
        run_id,
        SimpleNamespace(
            state="paused_for_approval",
            steps=[rec1, rec2],
            calls_per_model={},
            input_tokens=1000,
            output_tokens=100,
            usd=0.0123,
            wall_s=12.5,
            big_model_calls=0,
            big_model_avoided=3,
            reason="payment risk 0.95 >= hard threshold 0.60 on this screen",
            done_check=None,
        ),
    )
    # A second run that never recorded a step
    run2 = logger.start_run("jev_computer", "aborted immediately")
    logger.finish_run(
        run2,
        SimpleNamespace(
            state="blocked", steps=[], calls_per_model={}, input_tokens=10,
            output_tokens=2, usd=0.0001, wall_s=1.0, big_model_calls=0,
            big_model_avoided=0, reason="no driver", done_check=None,
        ),
    )
    yield SimpleNamespace(store=store, logger=logger, run_id=run_id, run2=run2)
    store.close()


def _frame_result(step_id: int, kind: str):
    """Works with fastapi Response objects and with the no-fastapi fallback dict."""
    res = plugin_api.get_cu_frame(step_id, kind=kind)
    if isinstance(res, dict):
        return res["body"], res["media_type"]
    return res.body, res.media_type


def test_cu_runs_route_lists_runs_newest_first(seeded):
    runs = plugin_api.get_cu_runs()
    assert [r["id"] for r in runs] == [seeded.run2, seeded.run_id]

    finished = next(r for r in runs if r["id"] == seeded.run_id)
    assert finished["task"] == "jev_computer"
    assert finished["state"] == "paused_for_approval"
    assert finished["steps"] == 2
    assert finished["tokens"] == 1100  # tokens_in + tokens_out
    assert finished["tokens_in"] == 1000 and finished["tokens_out"] == 100
    assert finished["usd"] == pytest.approx(0.0123)
    assert finished["wall_s"] == pytest.approx(12.5)
    assert finished["big_model_calls"] == 0
    assert finished["big_model_avoided"] == 3
    assert finished["reason"].startswith("payment risk")
    assert finished["created_at"] and finished["finished_at"]
    assert finished["shadow"] is True

    # limit is honoured and clamped
    assert len(plugin_api.get_cu_runs(limit=1)) == 1


def test_cu_steps_route_shape(seeded):
    steps = plugin_api.get_cu_steps(run=seeded.run_id)
    assert [s["idx"] for s in steps] == [0, 1]

    first = steps[0]
    assert first["run_id"] == seeded.run_id
    assert first["action"] == "click" and first["target"] == "Submit"
    assert first["grounding"] == "box"
    # box comes back as fractions, not pixels
    assert first["box"] == pytest.approx([0.05, 0.57, 0.12, 0.63])
    # click point in desktop pixels
    assert first["point"] == pytest.approx([109.0, 480.0])
    assert first["verdict"] == "pass" and first["noul"] == pytest.approx(0.98)
    assert first["effect"] == "confirmed"
    assert first["safety"] == "none" and first["stall"] == ""
    assert first["tokens"] == 0  # no usages recorded on the fixture rec
    assert first["usd"] == 0.0
    # frames are referenced by STEP id, never by path
    assert first["frame_before"] == first["id"]
    assert first["frame_after"] == first["id"]
    assert "/" not in str(first["frame_before"]) and "\\" not in str(first["frame_before"])

    paused = steps[1]
    assert paused["verdict"] == "paused"
    assert paused["safety"] == "payment"
    assert paused["stall"] == "repeat_screen"
    assert paused["box"] is None and paused["point"] is None
    assert paused["frame_before"] is None and paused["frame_after"] is None


def test_cu_runs_route_empty_store():
    """No cu runs yet -> empty list (the panel renders its friendly empty state)."""
    assert plugin_api.get_cu_runs() == []


def test_cu_steps_route_errors(seeded):
    from hermes_loopguard.dashboard.plugin_api import HTTPException

    with pytest.raises(HTTPException) as exc:
        plugin_api.get_cu_steps(run=99999)
    assert exc.value.status_code == 404

    # A run that exists but has no steps yields an empty list, not an error
    assert plugin_api.get_cu_steps(run=seeded.run2) == []


def test_cu_frame_serves_files_pointed_to_by_the_row(seeded):
    steps = plugin_api.get_cu_steps(run=seeded.run_id)
    step_id = steps[0]["id"]

    body, media = _frame_result(step_id, "before")
    assert media == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(body) == len(_png_bytes())

    _, media = _frame_result(step_id, "after")
    assert media == "image/png"

    # thumb is the JPEG sibling the logger wrote next to the PNG
    _, media = _frame_result(step_id, "thumb")
    assert media == "image/jpeg"


def test_cu_frame_refuses_everything_outside_the_data_dir(seeded, tmp_path):
    store = seeded.store
    # Smuggled rows: absolute outside path, traversal, and a real file that
    # exists but outside the loopguard data dir.
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    for bad_path in (
        "/etc/passwd",
        "../../etc/passwd",
        str(outside),
        "",
        None,
    ):
        cur = store.conn.execute(
            "INSERT INTO cu_steps (run_id, idx, action, frame_before) VALUES (?, ?, ?, ?)",
            (seeded.run_id, 99, "click", bad_path),
        )
        step_id = int(cur.lastrowid)

        from hermes_loopguard.dashboard.plugin_api import HTTPException

        with pytest.raises(HTTPException) as exc:
            plugin_api._cu_frame_payload(step_id, "before")
        assert exc.value.status_code == 404, bad_path


def test_cu_frame_errors(seeded):
    from hermes_loopguard.dashboard.plugin_api import HTTPException

    steps = plugin_api.get_cu_steps(run=seeded.run_id)
    step_id = steps[0]["id"]
    no_frame_id = steps[1]["id"]

    with pytest.raises(HTTPException) as exc:
        plugin_api._cu_frame_payload(step_id, "sideways")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        plugin_api._cu_frame_payload(no_frame_id, "before")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        plugin_api._cu_frame_payload(999999, "before")
    assert exc.value.status_code == 404


# --- overlay math (extracted from the IIFE, evaluated under node) ------------


def _extract_fn(src: str, name: str) -> str:
    marker = "function " + name + "("
    start = src.index(marker)
    depth = 0
    for j in range(src.index("{", start), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError(f"unbalanced braces while extracting {name}")


def _overlay_geometry(box, point, w, h):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    code = (
        _extract_fn(JS_PATH.read_text(encoding="utf-8"), "overlayGeometry")
        + "\nvar a = process.argv.slice(1);"
        + "\nvar box = a[0] ? JSON.parse(a[0]) : null;"
        + "\nvar point = a[1] ? JSON.parse(a[1]) : null;"
        + "\nprocess.stdout.write('LGRESULT' + JSON.stringify("
        + "overlayGeometry(box, point, Number(a[2]), Number(a[3]))));"
    )
    r = subprocess.run(
        [node, "-e", code,
         json.dumps(box) if box else "",
         json.dumps(point) if point else "",
         str(w), str(h)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"node failed:\n{r.stderr}"
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("LGRESULT")][-1]
    return json.loads(line[len("LGRESULT"):])


def test_overlay_geometry_box_fractions_become_percent():
    out = _overlay_geometry([0.05, 0.57, 0.12, 0.63], None, 0, 0)
    assert out["box"] == {
        "left": "5%", "top": "57%", "width": "7%", "height": "6%",
    }
    assert out["point"] is None

    # corners given in reverse order still normalize
    out = _overlay_geometry([0.9, 0.8, 0.4, 0.3], None, 0, 0)
    assert out["box"] == {
        "left": "40%", "top": "30%", "width": "50%", "height": "50%",
    }


def test_overlay_geometry_point_pixels_use_natural_size():
    out = _overlay_geometry(None, [109, 480], 1280, 960)
    assert out["box"] is None
    assert out["point"] == {"left": "8.52%", "top": "50%"}

    # unknown natural size (image not loaded yet) -> no point marker, no crash
    out = _overlay_geometry(None, [109, 480], 0, 0)
    assert out["point"] is None


def test_panel_assets_are_wired():
    """The Computer use view exists, is reachable from the page switch, and the
    routes it fetches are the ones plugin_api defines."""
    text = JS_PATH.read_text(encoding="utf-8")
    assert "function CuPanel(" in text
    assert "function FrameModal(" in text
    assert "'Computer use'" in text, "page switch must offer the Computer use view"
    assert "/api/plugins/loopguard/cu/runs" in text
    assert "/api/plugins/loopguard/cu/steps" in text
    assert "/api/plugins/loopguard/cu/frame/" in text

    from hermes_loopguard.dashboard import plugin_api

    mounted = {path for _, path, _ in plugin_api.router.routes}
    assert "/cu/runs" in mounted and "/cu/steps" in mounted
    assert "/cu/frame/{step_id}" in mounted
