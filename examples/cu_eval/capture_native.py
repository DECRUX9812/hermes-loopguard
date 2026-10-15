#!/usr/bin/env python3
"""Capture the NATIVE-app eval frames + AT-SPI ground truth.

MUST run with the bench stack up and ONLY under flock /tmp/lg-cu/stack.lock:

    flock /tmp/lg-cu/stack.lock python3 capture_native.py

Captures frames/*.png (1280x800 desktop) for the GTK settings window, a
thunar window (dense toolbar) and mousepad (empty + typed), dumps the AT-SPI
element lists (desktop px) next to them, and appends the native cases to
gt_native.json.  Only touches apps launched by THIS script (scoped kills).
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

SOCKET = "/tmp/lg-cu/cua.sock"
FRAMES = HERE / "frames"
FILES_DIR = Path("/tmp/lg-cu/files/lg-eval")
TYPED_TEXT = "loopground eval typed text"


def launch(argv: list[str]) -> None:
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/lg-cu/launcher.sock")
    s.sendall((json.dumps({"argv": argv}) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    s.recv(65536)
    s.close()


def kill_scoped() -> None:
    """Kill ONLY the apps this capture launched (never pkill -f broad)."""
    for needle, comm in [
        ("mousepad", "Mousepad"),
        ("thunar /tmp/lg-cu/files/lg-eval", "thunar"),
        ("settings_app.py", "python3"),
    ]:
        out = subprocess.run(["pgrep", "-f", needle], capture_output=True, text=True).stdout.split()
        for pid in out:
            try:
                if Path(f"/proc/{pid}/comm").read_text().startswith(comm):
                    subprocess.run(["kill", pid], capture_output=True)
            except OSError:
                continue
    time.sleep(2)


def dump_elements(driver: CuaDriver, app: str, tries: int = 8) -> list[dict]:
    els: list = []
    for _ in range(tries):
        els = driver.elements(app)
        if els:
            break
        time.sleep(1.2)
    return [
        {"role": el.role, "label": el.label, "x": el.x, "y": el.y, "w": el.w, "h": el.h, "app": el.app}
        for el in els
    ]


def main() -> int:
    from hermes_loopguard.cu import eval_cu

    FRAMES.mkdir(exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    (FILES_DIR / "report-q3.txt").write_text("scratch\n")
    (FILES_DIR / "notes.txt").write_text("scratch\n")
    kill_scoped()
    driver = CuaDriver(socket=SOCKET, app=None)
    gt: dict[str, dict] = {}

    def save(frame_name: str) -> None:
        frame = driver.screenshot()
        (FRAMES / f"{frame_name}.png").write_bytes(frame.png)
        print(f"  {frame_name}.png {frame.width}x{frame.height}")

    # GTK settings window
    launch(["python3", str(HERE.parent / "cu_bench" / "apps" / "settings_app.py")])
    time.sleep(4)
    save("settings_gtk")
    gt["settings_gtk"] = {"elements": dump_elements(driver, "Settings_app")}

    # thunar (dense toolbar)
    launch(["thunar", str(FILES_DIR)])
    time.sleep(4)
    save("thunar")
    gt["thunar"] = {"elements": dump_elements(driver, "thunar")}

    # mousepad: empty, then typed
    launch(["mousepad"])
    time.sleep(3)
    save("mousepad")
    gt["mousepad"] = {"elements": dump_elements(driver, "Mousepad")}
    driver.type_text(TYPED_TEXT)
    time.sleep(1)
    save("mousepad_typed")

    kill_scoped()
    driver.close()
    out = HERE / "gt_native.json"
    existing = json.loads(out.read_text()) if out.exists() else {}
    existing.update(gt)
    out.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    counts = {k: len(v["elements"]) for k, v in gt.items()}
    print(f"wrote {len(gt)} frames; elements: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
