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


BENCH_DISPLAY = ":97"


def _display(pid: str) -> str | None:
    """DISPLAY of a pid, or None when unreadable (another user's process:
    never touch those) or unset."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for kv in raw.split(b"\0"):
        if kv.startswith(b"DISPLAY="):
            return kv.decode(errors="replace").split("=", 1)[1]
    return None


def _is_bench_app(pid: str) -> bool:
    """Bench app = bench DISPLAY + a bench-app comm (case-insensitive: the
    daemonized mousepad reports comm 'mousepad', not 'Mousepad') — or the
    bench settings app, whose comm is just 'python3' (cmdline match)."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace")
    except OSError:
        return False
    if "settings_app.py" in cmd:
        return _display(pid) == BENCH_DISPLAY
    if _display(pid) != BENCH_DISPLAY:
        return False
    return comm in ("thunar", "mousepad", "chrome", "chrome_crashpad")


def kill_scoped() -> None:
    """Kill bench apps on the bench display ONLY (DISPLAY=:97 + comm match).
    Never touches the user's desktop, other displays, or other users."""
    out = subprocess.run(
        ["pgrep", "-f", "thunar|mousepad|settings_app|google-chrome"],
        capture_output=True, text=True,
    ).stdout.split()
    for pid in out:
        if _is_bench_app(pid):
            try:
                subprocess.run(["kill", pid], capture_output=True)
            except OSError:
                continue
    for _ in range(15):  # wait until really gone (chrome writes its profile)
        alive = [p for p in subprocess.run(
            ["pgrep", "-f", "thunar|mousepad|settings_app|google-chrome"],
            capture_output=True, text=True).stdout.split() if _is_bench_app(p)]
        if not alive:
            break
        time.sleep(1)
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

    # mousepad: empty, then typed (click the text area first: type_text goes
    # into the focused element, and a fresh window is not focused)
    launch(["mousepad"])
    time.sleep(3)
    save("mousepad")
    gt["mousepad"] = {"elements": dump_elements(driver, "Mousepad")}
    # type via a TARGETED driver: with app=None type_text has no target
    # window and the keystrokes land nowhere (verified 2026-10-15)
    mp = CuaDriver(socket=SOCKET, app="Mousepad")
    mp.type_text(TYPED_TEXT)
    mp.close()
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
