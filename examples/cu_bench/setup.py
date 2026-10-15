#!/usr/bin/env python3
"""Execute a task's `setup` block against the running lg-cu-stack.

    python3 setup.py <task-id-or-yaml-path>

Setup verbs (all launched INSIDE the isolated session via the launcher socket):
  mkdir: /path                     create a directory (parents ok)
  write: {path, content}           create a file
  rm: /path                        remove a file/dir if present
  open_url: url                    open the URL in a fresh chrome window
  launch: [argv...]                spawn an app inside the session
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOCKET = "/tmp/lg-cu/launcher.sock"
CHROME = "google-chrome"
CHROME_ARGS = [
    "--user-data-dir=/tmp/lg-cu/chrome-profile",
    "--no-first-run",
    "--no-default-browser-check",
    "--force-renderer-accessibility",
    "--disable-gpu",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--new-window",
]


def launch(argv: list[str]) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCKET)
    s.sendall((json.dumps({"argv": argv}) + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    out = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        out += chunk
    s.close()
    return json.loads(out.decode().strip() or "{}")


def open_url(url: str) -> dict:
    return launch([CHROME, *CHROME_ARGS, url])


def run_setup(task_id: str) -> int:
    import yaml

    task_path = Path(task_id)
    if task_path.suffix != ".yaml":
        task_path = HERE / "tasks" / f"{task_id}.yaml"
    task = yaml.safe_load(task_path.read_text())
    steps = task.get("setup", [])
    print(f"setup for {task['id']}:")
    for step in steps:
        if isinstance(step, str):  # e.g. "open_url: http://..."
            step = shlex.split(step)
        if "mkdir" in step:
            path = Path(step["mkdir"])
            path.mkdir(parents=True, exist_ok=True)
            print(f"  mkdir {path}")
        elif "write" in step:
            spec = step["write"]
            path = Path(spec["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(spec.get("content", ""))
            print(f"  write {path} ({len(spec.get('content', ''))} bytes)")
        elif "rm" in step:
            path = Path(step["rm"])
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
            print(f"  rm {path}")
        elif "open_url" in step:
            res = open_url(step["open_url"])
            print(f"  open_url {step['open_url']} -> {res}")
        elif "launch" in step:
            argv = step["launch"]
            res = launch(argv)
            print(f"  launch {argv[0]} -> {res}")
        else:
            print(f"  UNKNOWN STEP: {step}", file=sys.stderr)
            return 2
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return run_setup(sys.argv[1])


if __name__ == "__main__":
    sys.exit(main())
