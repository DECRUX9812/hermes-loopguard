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


def _bench_pids() -> list[str]:
    """Bench apps = DISPLAY=:97 + a bench-app comm (case-insensitive: a
    daemonized mousepad reports comm 'mousepad', not 'Mousepad') — or the
    bench settings app, whose comm is just 'python3' (cmdline match)."""
    pids = subprocess.run(
        ["pgrep", "-f", "thunar|mousepad|settings_app|google-chrome|soffice|oosplash"],
        capture_output=True, text=True,
    ).stdout.split()
    out = []
    for pid in pids:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace")
        except OSError:
            continue
        if "settings_app.py" in cmd:
            if _display(pid) == BENCH_DISPLAY:
                out.append(pid)
            continue
        if _display(pid) != BENCH_DISPLAY:
            continue
        # soffice/oosplash: the bench's LibreOffice arm leaks windows across
        # lock holds; under the lock nothing of X7's is RUNNING, leftovers only
        if comm in ("thunar", "mousepad", "chrome", "chrome_crashpad",
                    "soffice", "soffice.bin", "oosplash"):
            out.append(pid)
    return out


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
    """Kill bench apps on the bench display ONLY (DISPLAY=:97 + comm match).
    Never touches the user's desktop, other displays, or other users."""
    for pid in _bench_pids():
        try:
            subprocess.run(["kill", pid], capture_output=True)
        except OSError:
            continue
    # wait until they are REALLY gone (chrome writes its profile on exit)
    for _ in range(60):
        if not _bench_pids():
            break
        time.sleep(1)
    time.sleep(2)


def main() -> int:
    from hermes_loopguard.cu.drivers.cua import CuaDriver

    kill_scoped()
    # app="Mousepad" so type_text targets + activates the app's own window
    # (with app=None the typed input has no target window and lands nowhere)
    driver = CuaDriver(socket="/tmp/lg-cu/cua.sock", app="Mousepad")
    launch(["mousepad"])
    # wait for a REAL mousepad window (File menu = the editor, not some
    # leftover dialog); a Save As dialog alone must never pass
    elements: list = []
    for _ in range(60):
        elements = driver.elements("Mousepad")
        if any(e.role == "menu" and e.label == "File" for e in elements):
            break
        time.sleep(1.5)
    else:
        print("FAIL: no mousepad editor window (File menu) after 90s; "
              f"elements: {elements}", file=sys.stderr)
        kill_scoped()
        driver.close()
        return 1
    frame = driver.screenshot()
    (HERE / "frames" / "mousepad.png").write_bytes(frame.png)
    els = [e for e in elements if e.role == "text"]
    if els:
        res = driver.click(els[0].x + els[0].w // 2, els[0].y + els[0].h // 2)
        print(f"click textarea: ok={res.ok} effect={res.effect}")
        time.sleep(0.5)
    driver.type_text(TYPED)
    time.sleep(1)
    frame = driver.screenshot()
    (HERE / "frames" / "mousepad_typed.png").write_bytes(frame.png)
    if frame.png == (HERE / "frames" / "mousepad.png").read_bytes():
        print("FAIL: typed frame identical to the empty frame; typing did not land",
              file=sys.stderr)
        kill_scoped()
        driver.close()
        return 1
    # rect dump for the NEW window position (positions move between launches)
    fresh = driver.elements("Mousepad")
    rows = [
        {"role": el.role, "label": el.label, "x": el.x, "y": el.y, "w": el.w, "h": el.h}
        for el in (fresh or elements)
    ]
    if not rows:
        print("FAIL: empty AT-SPI dump; gt would be empty", file=sys.stderr)
        kill_scoped()
        driver.close()
        return 1
    (HERE / "gt_mousepad.json").write_text(json.dumps({"elements": rows}, indent=2) + "\n")
    kill_scoped()
    driver.close()
    print("mousepad frames captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
