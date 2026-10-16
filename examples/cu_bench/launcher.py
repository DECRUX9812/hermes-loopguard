#!/usr/bin/env python3
"""App launcher for the isolated bench session.

Runs INSIDE the ``dbus-run-session`` of the lg-cu-stack unit so that every app
it spawns inherits the session bus, DISPLAY=:97 and the AT-SPI environment.
The stack exposes it on a unix socket; the bench launches apps by sending one
JSON line per request::

    {"argv": ["thunar", "/tmp/lg-cu/files/fm-task"], "env": {}}

Every spawned app runs with a DISPOSABLE HOME (``/tmp/lg-cu/home``): the bench
apps must never show the real account name or the real home directory — those
screenshots go to the vision models.  Privacy keys (HOME, XDG_*, USER,
LOGNAME) are forced and cannot be overridden by the request; other request
``env`` entries are still applied.

Per-TASK reset (X8): the bench sends ``{"reset": true}`` once per task.  That
kills EVERY app this launcher spawned (tracked PIDs and their process groups,
SIGTERM then SIGKILL — never pkill -f broad patterns) and THEN wipes the
disposable HOME.  Spawning does not wipe the HOME any more: a second app
launched in the same task keeps the first app's files.

Response: one JSON line ``{"pid": 1234}`` / ``{"reset": "ok", "killed": [...],
"alive": [...]}`` or ``{"error": "..."}``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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

    Called ONCE PER TASK (on the reset request, after the tracked apps are
    killed): leftovers from a previous task can never leak into the next
    one's screenshots, while a second app spawned in the same task keeps the
    first app's files."""
    home = Path(home_root)
    shutil.rmtree(home, ignore_errors=True)
    dirs = [home, home / ".config", home / ".local" / "share", home / ".cache"]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return {str(d): "ok" for d in dirs}


def resolve_cwd(request_cwd: str | None, home_root: str) -> str:
    """Where a spawned app runs: the request's cwd if given, else the
    DISPOSABLE home — never the launcher's own cwd (systemd starts the stack
    in the real home, and e.g. GTK's Save As dialog opens at the process
    cwd, which put the real home directory on screen)."""
    return str(request_cwd) if request_cwd else home_root


def spawn_app(
    argv: list[str],
    env: dict,
    cwd: str,
    *,
    log_path: str,
    tracked: dict[int, subprocess.Popen],
) -> int:
    """Spawn one app in its OWN process group (so the per-task reset can kill
    the whole group — chrome renderers, GTK dialogs — without pattern
    matching) and track the Popen object (also reaps zombies)."""
    log = open(log_path, "ab")  # noqa: SIM115 - kept open for the process lifetime
    proc = subprocess.Popen(
        argv,
        env=env,
        stdout=log,
        stderr=log,
        cwd=cwd,
        start_new_session=True,  # process group leader: pgid == pid
    )
    log.close()  # the fd is duplicated into the child; our copy is not needed
    tracked[proc.pid] = proc
    return proc.pid


def _alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def kill_tracked(tracked: dict[int, subprocess.Popen], grace_s: float = 5.0) -> tuple[list[int], list[int]]:
    """Kill EVERY tracked app: SIGTERM to the process group, a bounded grace
    wait, then SIGKILL to whatever is still alive.  Returns (killed, alive).

    REAL leak (X7/P6B): a Mousepad Save-As dialog and a LibreOffice Writer
    window survived the bench's pattern-based kills (SIGTERM has no effect on
    an app stuck in a modal dialog, and a Thunar opened on another folder
    never matched the pattern at all) and haunted later tasks' screenshots.
    Tracking PIDs/process groups instead of guessing patterns ends both.
    """
    # Drop Popen entries whose process is already reaped.
    for pid in [p for p, proc in tracked.items() if not _alive(proc)]:
        del tracked[pid]
    pids = list(tracked)
    if not pids:
        return [], []
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace_s
    while time.time() < deadline and any(_alive(tracked[p]) for p in pids):
        time.sleep(0.1)
    for pid in pids:
        if _alive(tracked[pid]):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    # SIGKILL takes a moment to reap through: give the kernel that moment so
    # the killed/alive classification does not report survivors that are
    # already dying.
    deadline = time.time() + 2.0
    while time.time() < deadline and any(_alive(tracked[p]) for p in pids):
        time.sleep(0.1)
    killed = [p for p in pids if not _alive(tracked[p])]
    alive = [p for p in pids if _alive(tracked[p])]
    for p in killed:
        try:
            tracked[p].wait(timeout=1.0)  # reap the zombie
        except subprocess.TimeoutExpired:
            pass
        del tracked[p]
    return killed, alive


def handle_request(
    req: dict,
    *,
    base_env: dict,
    home_root: str,
    log_path: str,
    tracked: dict[int, subprocess.Popen],
) -> dict:
    """One launcher request: {"reset": true} or {"argv": [...], "env": {...}}.
    Errors come back as {"error": ...}; the launcher itself never dies."""
    try:
        if req.get("reset"):
            killed, alive = kill_tracked(tracked)
            reset_disposable_home(home_root)
            return {"reset": "ok", "killed": killed, "alive": alive}
        argv = [str(a) for a in req.get("argv") or []]
        if not argv:
            raise ValueError("argv is required")
        env = build_bench_env(base_env, home_root, req.get("env"))
        pid = spawn_app(
            argv,
            env,
            resolve_cwd(req.get("cwd"), home_root),
            log_path=log_path,
            tracked=tracked,
        )
        return {"pid": pid}
    except Exception as exc:  # noqa: BLE001 - keep the launcher alive
        return {"error": str(exc)}


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
    tracked: dict[int, subprocess.Popen] = {}
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
                req: dict[str, Any] = json.loads(b"".join(chunks).strip() or b"{}")
                resp = handle_request(
                    req,
                    base_env=os.environ,
                    home_root=args.home_root,
                    log_path="/tmp/lg-cu/apps.log",
                    tracked=tracked,
                )
                conn.sendall((json.dumps(resp) + "\n").encode())
            except Exception as exc:  # noqa: BLE001 - keep the launcher alive
                try:
                    conn.sendall((json.dumps({"error": str(exc)}) + "\n").encode())
                except OSError:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
