#!/usr/bin/env python3
"""Capture GROUND-TRUTH target rects for the bench tasks into ground_truth/.

- HTML pages: open each page in the isolated chrome, let the DOM beacon post
  pixel rects to the judge, read the judge log, save per-page JSON.
- Native apps (GTK settings, thunar, mousepad): dump AT-SPI elements via the
  CuaDriver and save them as element lists with desktop-px frames.

Run with the stack up:  python3 capture_gt.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from hermes_loopguard.cu.drivers.cua import CuaDriver  # noqa: E402

HERE = Path(__file__).resolve().parent
GT = HERE / "ground_truth"
JUDGE = "http://127.0.0.1:8793"
SOCKET = "/tmp/lg-cu/cua.sock"

PAGES = [
    "form_contact.html",
    "form_widgets.html",
    "form_scroll.html",
    "form_validation.html",
    "search.html",
    "results.html?q=quickstart",
    "confirm.html?item=quickstart&q=quickstart",
    "flow1.html",
    "flow2.html",
    "shop.html",
]


def judge_log() -> list[dict]:
    with urllib.request.urlopen(f"{JUDGE}/log") as resp:
        return json.loads(resp.read())


def kill_chrome() -> None:
    out = subprocess.run(
        ["pgrep", "-f", "user-data-dir=/tmp/lg-cu/chrome-profile"],
        capture_output=True, text=True,
    ).stdout.split()
    for pid in out:
        try:
            if Path(f"/proc/{pid}/comm").read_text().startswith("chrome"):
                subprocess.run(["kill", pid], capture_output=True)
        except OSError:
            continue
    time.sleep(2)


def launch(url: str) -> None:
    import socket

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/lg-cu/launcher.sock")
    argv = [
        "google-chrome",
        "--user-data-dir=/tmp/lg-cu/chrome-profile",
        "--no-first-run", "--no-default-browser-check",
        "--force-renderer-accessibility", "--disable-gpu",
        "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
        "--new-window", url,
    ]
    s.sendall((json.dumps({"argv": argv}) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    s.recv(65536)
    s.close()


def capture_pages() -> None:
    import time as _t

    start_ts = _t.time()
    GT.mkdir(exist_ok=True)
    for page in PAGES:
        kill_chrome()
        launch(f"{JUDGE}/{page}")
        name = page.split("?")[0].removesuffix(".html")
        # wait for THIS page's beacon from THIS capture run (first load after
        # relaunch can be slow)
        latest = None
        for _ in range(80):
            time.sleep(0.5)
            entries = [
                e for e in judge_log()
                if e.get("type") == "rects"
                and e.get("page", "").startswith(name)
                and e.get("ts", 0) > start_ts
            ]
            if entries:
                latest = entries[-1]
                break
        if latest is None:
            print(f"  !! no beacon rects for {page} after 40s")
            continue
        out = {
            "page": latest.get("page"),
            "url": latest.get("url"),
            "viewport": latest.get("viewport"),
            "offset": latest.get("offset"),
            "rects": latest.get("rects"),
            "captured_at": latest.get("ts"),
        }
        (GT / f"{name}.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(f"  {name}: {len(out['rects'])} rects")


def dump_app_elements(driver: CuaDriver, app: str, out_name: str, tries: int = 6) -> None:
    els: list = []
    for _ in range(tries):
        els = driver.elements(app)
        if els:
            break
        time.sleep(1.2)
    rows = [
        {"role": el.role, "label": el.label, "x": el.x, "y": el.y, "w": el.w, "h": el.h, "app": el.app}
        for el in els
    ]
    (GT / out_name).write_text(json.dumps({"app": app, "elements": rows}, indent=2) + "\n")
    print(f"  {out_name}: {len(rows)} elements")


def capture_apps() -> None:
    import socket

    def launch_app(argv: list[str]) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("/tmp/lg-cu/launcher.sock")
        s.sendall((json.dumps({"argv": argv}) + "\n").encode())
        s.shutdown(socket.SHUT_WR)
        s.recv(65536)
        s.close()

    driver = CuaDriver(socket=SOCKET, app=None)

    kill_chrome()
    launch_app(["python3", str(HERE / "apps" / "settings_app.py")])
    time.sleep(3)
    dump_app_elements(driver, "Settings_app", "settings_app.json")

    subprocess.run(["mkdir", "-p", "/tmp/lg-cu/files/fm-task"], capture_output=True)
    launch_app(["thunar", "/tmp/lg-cu/files/fm-task"])
    time.sleep(4)
    dump_app_elements(driver, "thunar", "thunar.json")

    launch_app(["mousepad"])
    time.sleep(3)
    dump_app_elements(driver, "Mousepad", "mousepad.json")
    driver.close()


def main() -> int:
    print("capturing page ground truth (DOM beacons):")
    capture_pages()
    print("capturing app ground truth (AT-SPI):")
    capture_apps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
