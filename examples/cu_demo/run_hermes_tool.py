#!/usr/bin/env python3
"""Drive the `jev_computer` tool through a REAL stock Hermes agent run
(hermes chat oneshot, CLIProxy gemini-3.1-flash-lite) on the contact-form
task, then print the code-judge verdict.

The hermes binary is resolved from $HERMES_BIN or $PATH; keys are loaded from
~/.hermes/.env by this script and never printed.

Recipe (see README.md): run under `flock /tmp/lg-cu/stack.lock` with the
Python that has hermes-loopguard and typesafe-sdk.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "examples" / "cu_bench"
STACK = BENCH / "stack.sh"

HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes")
HOME = Path("/tmp/lg-cu/demo-hermes-home")
JUDGE = "http://127.0.0.1:8793"
SOCKET = "/tmp/lg-cu/cua.sock"


def say(line: str = "") -> None:
    print(line, flush=True)


def launch(argv: list[str]) -> dict:
    import socket as s

    sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    sock.connect("/tmp/lg-cu/launcher.sock")
    sock.sendall((json.dumps({"argv": argv}) + "\n").encode())
    sock.shutdown(s.SHUT_WR)
    out = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        out += chunk
    sock.close()
    return json.loads(out.decode().strip() or "{}")


def kill_chrome() -> None:
    out = subprocess.run(
        ["pgrep", "-f", "user-data-dir=/tmp/lg-cu/chrome-profile"],
        capture_output=True, text=True,
    ).stdout.split()
    for pid in out:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            continue
        if comm.startswith("chrome"):
            try:
                subprocess.run(["kill", pid], capture_output=True)
            except ProcessLookupError:
                pass
    time.sleep(2.5)


def setup_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "    - loopguard\n"
        "  disabled: []\n"
        "providers:\n"
        "  cliproxy:\n"
        "    name: cliproxy\n"
        "    api: http://127.0.0.1:8317/v1\n"
        "    key_env: CLIPROXY_API_KEY\n"
        "    transport: openai_chat\n"
    )
    lg_dir = HOME / "loopguard"
    lg_dir.mkdir(exist_ok=True)
    (lg_dir / "config.yaml").write_text(
        "computer_use:\n"
        "  enabled: true\n"
        "  driver:\n"
        "    name: cua\n"
        "    socket: /tmp/lg-cu/cua.sock\n"
        "    app: chrome\n"
        "  planner_model: gemini-3.1-flash-lite\n"
        "  models: [jev-1.14.0, jev-1.13.0, openai:gemini-3.8-flash-high]\n"
        "  openai: {base_url: http://127.0.0.1:8317/v1, api_key_env: CLIPROXY_API_KEY}\n"
        "  max_steps: 12\n"
        "  budget_usd: 0.50\n"
    )


def main() -> int:
    if not HERMES:
        say("hermes binary not found; set HERMES_BIN or put hermes on $PATH")
        return 2

    say("=== cu_demo: jev_computer via a stock Hermes agent run ===")
    # Workaround for a stack.sh bug (reported to P2): a stale a11y socket file
    # from a previous session makes a11y_bus.py skip the daemon start, so the
    # whole session runs with a dead AT-SPI bus (no elements, no keyboard).
    subprocess.run(["rm", "-f", "/tmp/lg-cu/a11y/bus", "/tmp/lg-cu/a11y/addr"])
    subprocess.run([str(STACK), "start"], check=True)
    try:
        setup_home()
        kill_chrome()
        res = launch([
            "google-chrome",
            "--user-data-dir=/tmp/lg-cu/chrome-profile",
            "--no-first-run", "--no-default-browser-check",
            "--force-renderer-accessibility", "--disable-gpu",
            "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
            "--new-window", f"{JUDGE}/form_contact.html",
        ])
        say(f"contact form open: pid={res.get('pid')}")
        time.sleep(8)

        env = dict(os.environ)
        env["HERMES_HOME"] = str(HOME)
        env["PATH"] = f"{Path(HERMES).parent}{os.pathsep}{env.get('PATH', '')}"
        say(f"running: {HERMES} chat -q <query> --oneshot -Q --yolo "
            "--provider cliproxy -m gemini-3.1-flash-lite")
        proc = subprocess.run(
            [HERMES, "chat",
             "-q",
             "Use the jev_computer tool to complete this task on the computer: fill in the "
             "contact form open in the browser with Name \"Ada Lovelace\", Email "
             "\"ada@analytical.engine\", Message \"Hello from LoopGuard\", then click Submit. "
             "Report the tool's summary back verbatim.",
             "--oneshot", "-Q", "--yolo",
             "--provider", "cliproxy", "-m", "gemini-3.1-flash-lite"],
            capture_output=True, text=True, timeout=900, env=env,
        )
        say(f"hermes exit={proc.returncode}")
        say("--- hermes stdout ---")
        say(proc.stdout)
        if proc.stderr.strip():
            say("--- hermes stderr (tail) ---")
            say("\n".join(proc.stderr.strip().splitlines()[-15:]))
    finally:
        subprocess.run([str(STACK), "stop"], check=False)

    say("--- judge verdict ---")
    proc = subprocess.run(
        [sys.executable, str(BENCH / "judge_check.py"),
         str(BENCH / "tasks" / "contact-form-simple.yaml")],
        capture_output=True, text=True,
    )
    say(proc.stdout.strip())
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
