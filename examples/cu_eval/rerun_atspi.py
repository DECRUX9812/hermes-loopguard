#!/usr/bin/env python3
"""Re-run the eval's AT-SPI arm with the native apps actually UP.

The canonical run executed after the capture had killed the native apps, so
every live AT-SPI lookup returned [] (0% hit is an artifact, not a result).
This script relaunches settings/thunar/mousepad in the capture order under
flock /tmp/lg-cu/stack.lock, checks the fresh element dumps against the
ground truth captured with the frames (gt_native.json / gt_mousepad.json),
and only proceeds to the atspi-arm eval merge when the geometry matches.

    flock -w 3600 /tmp/lg-cu/stack.lock python3 rerun_atspi.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cu_bench"))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from hermes_loopguard.cu.drivers.cua import CuaDriver  # noqa: E402
from capture_native import launch, kill_scoped, dump_elements  # noqa: E402

SOCKET = "/tmp/lg-cu/cua.sock"


def _sig(elements: list[dict]) -> set:
    return {(e["role"], e["label"], e["x"], e["y"], e["w"], e["h"])
            for e in elements}


def main() -> int:
    from hermes_loopguard.cu import eval_cu

    kill_scoped()
    driver = CuaDriver(socket=SOCKET, app=None)

    # Window placement is not reproducible across spawns (WM cascade), so
    # relaunch each app until the live AT-SPI dump EXACTLY matches the dump
    # captured with the frames.  Mousepad also gets the marker text typed
    # first: the gt dump was taken in the typed state (the text element's
    # a11y label carries it).
    for name, argv, app, tries, typed in [
        ("settings_gtk", ["python3", str(HERE.parent / "cu_bench" / "apps" / "settings_app.py")],
         "Settings_app", 6, False),
        ("thunar", ["thunar", "/tmp/lg-cu/files/lg-eval"], "thunar", 6, False),
        ("mousepad", ["mousepad"], "Mousepad", 8, True),
    ]:
        want = None
        if name == "mousepad":
            want = _sig(json.loads((HERE / "gt_mousepad.json").read_text())["elements"])
        else:
            gt_native = json.loads((HERE / "gt_native.json").read_text())
            want = _sig(gt_native[name]["elements"])
        got = None
        for attempt in range(tries):
            kill_scoped()
            launch(argv)
            time.sleep(4 if name != "mousepad" else 3)
            els = dump_elements(driver, app, tries=20)
            if name == "mousepad" and any(e["role"] == "menu" and e["label"] == "File" for e in els):
                mp = CuaDriver(socket=SOCKET, app="Mousepad")
                mp.type_text("loopground eval typed text")
                mp.close()
                time.sleep(1)
                els = dump_elements(driver, app, tries=6)
            got = _sig(els)
            if got == want:
                print(f"geometry match (attempt {attempt + 1}): {name} ({len(got)} elements)")
                break
        if got != want:
            print(f"GEOMETRY MISMATCH {name} after {tries} spawns: "
                  f"{len(want)} gt vs {len(got)} live")
            kill_scoped()
            driver.close()
            return 2

    report = eval_cu.run_eval(
        HERE / "cases.jsonl", repeat=3, arms=["atspi"], driver=driver,
        out_dir=HERE / "results" / "final",
    )
    atspi = report["locate"].get("atspi", {})
    print(eval_cu.format_report({"counts": report["counts"], "repeat": report["repeat"],
                                 "arms": ["atspi"], "cases_file": str(HERE / "cases.jsonl"),
                                 "locate": {"atspi": atspi}, "verify": {}, "safety": {},
                                 "spend": report["spend"], "thumbs": []}))
    kill_scoped()
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
