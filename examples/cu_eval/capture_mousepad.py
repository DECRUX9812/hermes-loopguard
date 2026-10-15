#!/usr/bin/env python3
"""Re-capture the mousepad frames (empty + typed) on the bench stack.

MUST run under flock /tmp/lg-cu/stack.lock with the stack up:

    flock /tmp/lg-cu/stack.lock python3 capture_mousepad.py

Kills ONLY the bench chrome profile + mousepad (scoped), waits until they are
really gone, then launches mousepad, clicks the text area to focus it, types
the marker text and screenshots both states.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

TYPED = "loopground eval typed text"
CHROME_NEEDLE = "user-data-dir=/tmp/lg-cu/chrome-profile"


def launch(argv: list[str]) -> None:
    """Spawn-and-go: the launcher may not reply until the app exits, so do
    NOT wait for a reply."""
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/lg-cu/launcher.sock")
    s.sendall((json.dumps({"argv": argv}) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    s.close()


def kill_scoped() -> None:
    for needle, comm in [
        (CHROME_NEEDLE, "chrome"),
        ("mousepad", "Mousepad"),
        ("thunar /tmp/lg-cu/files", "thunar"),
        ("settings_app.py", "python3"),
    ]:
        out = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True).stdout.split()
        for pid in out:
            try:
                if Path(f"/proc/{pid}/comm").read_text().startswith(comm):
                    subprocess.run(["kill", pid], capture_output=True)
            except OSError:
                continue
    # wait until they are REALLY gone (chrome writes its profile on exit)
    for _ in range(60):
        alive = subprocess.run(
            ["pgrep", "-f",
             f"{CHROME_NEEDLE}|mousepad|thunar /tmp/lg-cu/files|settings_app.py"],
            capture_output=True
        ).returncode
        if alive != 0:
            break
        time.sleep(1)
    time.sleep(2)


def main() -> int:
    from hermes_loopguard.cu.drivers.cua import CuaDriver

    kill_scoped()
    driver = CuaDriver(socket="/tmp/lg-cu/cua.sock", app=None)
    launch(["mousepad"])
    time.sleep(4)
    frame = driver.screenshot()
    (HERE / "frames" / "mousepad.png").write_bytes(frame.png)
    elements: list = []
    for _ in range(12):  # window + a11y tree can lag the spawn
        elements = driver.elements("Mousepad")
        if elements:
            break
        time.sleep(1.5)
    els = [e for e in elements if e.role == "text"]
    if els:
        res = driver.click(els[0].x + els[0].w // 2, els[0].y + els[0].h // 2)
        print(f"click textarea: ok={res.ok} effect={res.effect}")
        time.sleep(0.5)
    driver.type_text(TYPED)
    time.sleep(1)
    frame = driver.screenshot()
    (HERE / "frames" / "mousepad_typed.png").write_bytes(frame.png)
    # rect dump for the NEW window position (positions move between launches)
    fresh = driver.elements("Mousepad")
    rows = [
        {"role": el.role, "label": el.label, "x": el.x, "y": el.y, "w": el.w, "h": el.h}
        for el in (fresh or elements)
    ]
    (HERE / "gt_mousepad.json").write_text(json.dumps({"elements": rows}, indent=2) + "\n")
    kill_scoped()
    driver.close()
    print("mousepad frames captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
