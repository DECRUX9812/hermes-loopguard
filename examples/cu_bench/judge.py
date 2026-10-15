#!/usr/bin/env python3
"""Bench judge: a tiny stdlib HTTP server (127.0.0.1 only) that serves the task
pages and RECORDS outcomes (form POSTs, payment attempts, ground-truth beacon
rects) to a JSONL log.  The bench reads ground truth ONLY from this log / files
on disk / app state files — never from a model.

Endpoints:
  GET  /<page>.html   static task pages from --root
  GET  /beacon.js     the ground-truth beacon script
  POST /submit        form posts (urlencoded or JSON)  -> {"type": "form_post", ...}
  POST /pay           trap payment attempt             -> {"type": "payment", ...}
  POST /beacon        DOM element rects                -> {"type": "rects", ...}
  GET  /log           full JSONL log (?tail=N for last N lines)
  GET  /healthz       liveness
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent / "pages"
LOG = Path("/tmp/lg-cu/judge.jsonl")
_lock = threading.Lock()


def record(entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    line = json.dumps(entry, sort_keys=True)
    with _lock:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(line + "\n")


def read_log(tail: int | None = None) -> list[dict]:
    if not LOG.exists():
        return []
    lines = LOG.read_text().splitlines()
    if tail is not None:
        lines = lines[-tail:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class JudgeHandler(BaseHTTPRequestHandler):
    server_version = "lg-judge/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet; the JSONL log is the record
        pass

    # -- helpers --------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _page_ref(self) -> str:
        """Referer page name for attributing a POST to a task page."""
        referer = self.headers.get("Referer") or ""
        return Path(urlsplit(referer).path).name or "unknown"

    # -- GET ------------------------------------------------------------

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            return self._send_json(200, {"ok": True, "ts": time.time()})
        if path == "/log":
            qs = parse_qs(urlsplit(self.path).query)
            tail = int(qs.get("tail", ["0"])[0] or 0) or None
            body = json.dumps(read_log(tail)).encode()
            return self._send(200, body, "application/json")
        if path == "/beacon.js":
            beacon = (ROOT / "beacon.js").read_bytes()
            return self._send(200, beacon, "application/javascript")
        name = Path(path).name
        if path != "/" and not name:
            return self._send(404, b"not found", "text/plain")
        candidate = ROOT / (name or "index.html")
        if candidate.suffix == ".html" and candidate.exists():
            html = candidate.read_bytes()
            if b"/beacon.js" not in html:
                html = html.replace(
                    b"<head>", b'<head><script src="/beacon.js"></script>', 1
                )
            return self._send(200, html)
        if candidate.exists() and candidate.suffix in (".css", ".js", ".png", ".svg"):
            ctype = {
                ".css": "text/css",
                ".js": "application/javascript",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }[candidate.suffix]
            return self._send(200, candidate.read_bytes(), ctype)
        if path == "/":
            pages = sorted(p.name for p in ROOT.glob("*.html"))
            links = "<br>".join(f'<a href="/{p}">{p}</a>' for p in pages)
            return self._send(200, f"<h1>LoopGuard bench pages</h1>{links}".encode())
        return self._send(404, b"not found", "text/plain")

    # -- POST -----------------------------------------------------------

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self._read_body()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0]
        page = self._page_ref()

        if path == "/submit":
            fields: dict = {}
            if ctype == "application/json":
                try:
                    fields = json.loads(body or b"{}")
                except json.JSONDecodeError:
                    fields = {"_parse_error": "bad json"}
            else:
                for key, vals in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items():
                    fields[key] = vals[0] if len(vals) == 1 else vals
            record({"type": "form_post", "page": page, "fields": fields})
            thanks = (
                "<!doctype html><html><head><title>Submitted</title></head>"
                f"<body><h1>Thanks, {fields.get('name', 'friend')}</h1>"
                "<p>Your submission was recorded.</p></body></html>"
            )
            return self._send(200, thanks.encode())

        if path == "/pay":
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {"_parse_error": "bad json"}
            record({"type": "payment", "page": page, "payload": payload})
            return self._send_json(200, {"ok": True, "paid": True})

        if path == "/beacon":
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                return self._send_json(400, {"ok": False, "error": "bad json"})
            record({"type": "rects", "page": payload.get("page", page), "url": payload.get("url", ""), "rects": payload.get("rects", {})})
            return self._send_json(200, {"ok": True})

        return self._send_json(404, {"ok": False, "error": "unknown endpoint"})


def main() -> int:
    global ROOT, LOG
    ap = argparse.ArgumentParser(description="LoopGuard bench judge server")
    ap.add_argument("--port", type=int, default=8793)
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--log", default=str(LOG))
    args = ap.parse_args()
    ROOT = Path(args.root)
    LOG = Path(args.log)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), JudgeHandler)
    print(f"judge listening on 127.0.0.1:{args.port}, log={LOG}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
