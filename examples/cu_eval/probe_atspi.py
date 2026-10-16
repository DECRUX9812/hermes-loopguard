#!/usr/bin/env python3
"""Probe: why does run_locate_atspi's single driver.elements() call return []
when the geometry-dump loop (with retries) succeeds?  Relaunch the native
apps under the stack lock and call elements() exactly like the eval does.

    flock /tmp/lg-cu/stack.lock python3 probe_atspi.py
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "cu_bench"))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from hermes_loopguard.cu.drivers.cua import CuaDriver  # noqa: E402
from capture_native import launch, kill_scoped  # noqa: E402
from hermes_loopguard.cu import eval_cu  # noqa: E402

SOCKET = "/tmp/lg-cu/cua.sock"


def main() -> int:
    kill_scoped()
    driver = CuaDriver(socket=SOCKET, app=None)

    launch(["python3", str(HERE.parent / "cu_bench" / "apps" / "settings_app.py")])
    time.sleep(5)
    launch(["thunar", "/tmp/lg-cu/files/lg-eval"])
    time.sleep(5)
    launch(["mousepad"])
    time.sleep(4)

    windows = driver._list_windows()
    print("list_windows app_names:", [w.get("app_name") for w in windows])

    # single-shot lookups, exactly like run_locate_atspi
    for app in ("Settings_app", "thunar", "Mousepad"):
        for attempt in range(5):
            els = driver.elements(app)
            print(f"elements({app!r}) attempt {attempt}: {len(els)}")
            if els:
                el = eval_cu.match_element(
                    "Dark mode" if app == "Settings_app" else
                    "Open Parent" if app == "thunar" else "File",
                    None, els)
                print(f"  match_element -> {el and (el.label, el.role, el.x, el.y, el.w, el.h)}")
                break
            time.sleep(2)

    # raw get_window_state probe for the settings window: degraded?
    for w in windows:
        if "settings" in str(w.get("app_name", "")).lower() or "python" in str(w.get("app_name", "")).lower():
            result = driver._unwrap(driver._call("get_window_state", {
                "pid": w["pid"], "window_id": w["window_id"],
                "include_screenshot": False}))
            print("settings window_state keys:", sorted(result.keys())[:12])
            print("degraded:", result.get("degraded"), "reason:", result.get("degraded_reason"))
            print("n elements:", len(result.get("elements") or []))
    kill_scoped()
    driver.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
