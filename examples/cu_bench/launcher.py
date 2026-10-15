#!/usr/bin/env python3
"""App launcher for the isolated bench session.

Runs INSIDE the ``dbus-run-session`` of the lg-cu-stack unit so that every app
it spawns inherits the session bus, DISPLAY=:97 and the AT-SPI environment.
The stack exposes it on a unix socket; the bench launches apps by sending one
JSON line per request::

    {"argv": ["thunar", "/tmp/lg-cu/files/fm-task"], "env": {}}

Every spawned app runs with a DISPOSABLE HOME (``/tmp/lg-cu/home``, recreated
per spawn): the bench apps must never show the real account name or the real
home directory — those screenshots go to the vision models.  Privacy keys
(HOME, XDG_*, USER, LOGNAME) are forced and cannot be overridden by the
request; other request ``env`` entries are still applied.

Response: one JSON line ``{"pid": 1234}`` or ``{"error": "..."}``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Keys the launcher always forces to the disposable identity: Thunar shows the
# account name from USER/LOGNAME and every GTK dialog lists HOME.
PRIVACY_KEYS = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "USER", "LOGNAME")
NEUTRAL_USER = "bench"


def build_bench_env(base_env: dict, home_root: str, request_env: dict | None = None) -> dict:
    """The env a bench app runs with: the session env (DISPLAY, D-Bus, the
    private XDG_RUNTIME_DIR, AT-SPI) with a disposable HOME/identity forced
    in.  Pure so the unit tests can exercise it without spawning anything."""
    env = dict(base_env)
    home = home_root.rstrip("/")
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = f"{home}/.config"
    env["XDG_DATA_HOME"] = f"{home}/.local/share"
    env["XDG_CACHE_HOME"] = f"{home}/.cache"
    env["USER"] = NEUTRAL_USER
    env["LOGNAME"] = NEUTRAL_USER
    for key, value in (request_env or {}).items():
        key = str(key)
        if key in PRIVACY_KEYS:
            continue  # a request can never re-expose the real identity
        env[key] = str(value)
    return env


def reset_disposable_home(home_root: str) -> dict[str, str]:
    """Wipe and recreate the disposable HOME with its XDG subdirs.

    Called before every spawn (apps are killed between tasks, so this is
    per-task freshness): leftovers from a previous task can never leak into
    the next one's screenshots."""
    home = Path(home_root)
    shutil.rmtree(home, ignore_errors=True)
    dirs = [home, home / ".config", home / ".local" / "share", home / ".cache"]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return {d: "ok" for d in dirs}


def resolve_cwd(request_cwd: str | None, home_root: str) -> str:
    """Where a spawned app runs: the request's cwd if given, else the
    DISPOSABLE home — never the launcher's own cwd (systemd starts the stack
    in the real home, and e.g. GTK's Save As dialog opens at the process
    cwd, which put the real home directory on screen)."""
    return str(request_cwd) if request_cwd else home_root


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/lg-cu/launcher.sock")
    ap.add_argument("--home-root", default="/tmp/lg-cu/home")
    args = ap.parse_args()
    sock_path = Path(args.socket)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    srv.listen(8)
    print(f"launcher ready on {sock_path}", flush=True)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        with conn:
            try:
                chunks = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                req = json.loads(b"".join(chunks).strip() or b"{}")
                argv = [str(a) for a in req.get("argv") or []]
                if not argv:
                    raise ValueError("argv is required")
                reset_disposable_home(args.home_root)
                env = build_bench_env(os.environ, args.home_root, req.get("env"))
                log = open("/tmp/lg-cu/apps.log", "ab")
                proc = subprocess.Popen(
                    argv,
                    env=env,
                    stdout=log,
                    stderr=log,
                    cwd=resolve_cwd(req.get("cwd"), args.home_root),
                )
                conn.sendall((json.dumps({"pid": proc.pid}) + "\n").encode())
            except Exception as exc:  # noqa: BLE001 - keep the launcher alive
                try:
                    conn.sendall((json.dumps({"error": str(exc)}) + "\n").encode())
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
