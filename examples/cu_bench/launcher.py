#!/usr/bin/env python3
"""App launcher for the isolated bench session.

Runs INSIDE the ``dbus-run-session`` of the lg-cu-stack unit so that every app
it spawns inherits the session bus, DISPLAY=:97 and the AT-SPI environment.
The stack exposes it on a unix socket; the bench launches apps by sending one
JSON line per request::

    {"argv": ["thunar", "/tmp/lg-cu/files/fm-task"], "env": {}}

Response: one JSON line ``{"pid": 1234}`` or ``{"error": "..."}``.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/lg-cu/launcher.sock")
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
                env = dict(os.environ)
                env.update({str(k): str(v) for k, v in (req.get("env") or {}).items()})
                log = open("/tmp/lg-cu/apps.log", "ab")
                proc = subprocess.Popen(
                    argv,
                    env=env,
                    stdout=log,
                    stderr=log,
                    cwd=req.get("cwd") or os.getcwd(),
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
