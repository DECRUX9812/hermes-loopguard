#!/usr/bin/env python3
"""Run the jev_computer agent (run_task) live on chosen bench tasks and print
each task's code-judge verdict.  REAL model calls (spend: cents).

Recipe (see README.md):

    flock -w 900 /tmp/lg-cu/stack.lock \
        "$PY" examples/cu_demo/run_tasks.py \
        [task ...] [--stop-stack]

The stack lock must be held for every stack use (other executors share it).
By default the stack is LEFT RUNNING at the end; pass --stop-stack to stop it
when this run started it.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from hermes_loopguard.config import DEFAULTS  # noqa: E402
from hermes_loopguard.cu.agent import run_task  # noqa: E402
from hermes_loopguard.cu.drivers.cua import CuaDriver  # noqa: E402
from hermes_loopguard.cu.log import CuRunLogger  # noqa: E402
from hermes_loopguard.cu.vision import VisionGate  # noqa: E402

BENCH = REPO / "examples" / "cu_bench"
STACK = BENCH / "stack.sh"
TASKS_DIR = BENCH / "tasks"

JUDGE = "http://127.0.0.1:8793"
SOCKET = "/tmp/lg-cu/cua.sock"
LAUNCHER = "/tmp/lg-cu/launcher.sock"
CHROME_ARGS = [
    "--user-data-dir=/tmp/lg-cu/chrome-profile",
    "--no-first-run", "--no-default-browser-check",
    "--force-renderer-accessibility", "--disable-gpu",
    "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
    "--new-window",
]
ALL_TASKS = ("contact-form-simple", "settings-gtk", "trap-payment")


def load_key(name: str) -> str:
    env_path = os.path.expanduser("~/.hermes/.env")
    for line in Path(env_path).read_text().splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"')
    return ""


def say(line: str = "") -> None:
    print(line, flush=True)


def stack_active() -> bool:
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "lg-cu-stack"],
        capture_output=True,
    )
    return proc.returncode == 0


def ensure_stack() -> bool:
    """Start the stack unless it is already up.  True when THIS run started it."""
    if stack_active():
        say("stack already running")
        return False
    subprocess.run([str(STACK), "start"], check=True)
    return True


def launch(argv: list[str]) -> dict:
    import socket as s

    sock = s.socket(s.AF_UNIX, s.SOCK_STREAM)
    sock.connect(LAUNCHER)
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


def open_url(url: str) -> None:
    kill_chrome()
    res = launch(["google-chrome", *CHROME_ARGS, url])
    say(f"   chrome pid={res.get('pid')} url={url}")


def wait_element(driver: CuaDriver, label: str, role: str | None = None, tries: int = 15):
    for _ in range(tries):
        el = driver.find_element(label, role=role)
        if el is not None:
            return el
        time.sleep(1.2)
    return None


def window_app_for_title(title: str) -> str | None:
    """The cua app_name of the window whose title contains `title`."""
    out = subprocess.run(
        ["cua-driver", "call", "--socket", SOCKET, "list_windows",
         json.dumps({"on_screen_only": True})],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = data.get("structuredContent") or data
    windows = data.get("windows", []) if isinstance(data, dict) else []
    for w in windows:
        if isinstance(w, dict) and title.lower() in str(w.get("title", "")).lower():
            say(f"   window match: {w.get('title')!r} app={w.get('app_name')!r} pid={w.get('pid')}")
            return w.get("app_name")
    return None


def task_instruction(task_id: str) -> str:
    import yaml

    task = yaml.safe_load((TASKS_DIR / f"{task_id}.yaml").read_text())
    return " ".join(str(task["instruction"]).split())


def judge_check(task: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, str(BENCH / "judge_check.py"), str(TASKS_DIR / f"{task}.yaml")],
        capture_output=True, text=True,
    )
    return proc.returncode == 0, proc.stdout.strip()


def run_one(task_id: str, instruction: str, setup, cfg_extra: dict | None = None):
    say(f"\n=== TASK {task_id} ===")
    app = setup() or None
    driver = CuaDriver(socket=SOCKET, app=app)
    cfg = dict(DEFAULTS["computer_use"])
    cfg.update(cfg_extra or {})
    gate = VisionGate(cfg)
    log = CuRunLogger(None, base_dir="/tmp/lg-cu/demo-frames", enabled=True)
    started = time.time()
    try:
        result = run_task(instruction, driver, gate, cfg, log=log)
    finally:
        driver.close()
    say(f"state={result.state} reason={result.reason!r}")
    say(f"cost=${result.usd:.4f} tokens={result.input_tokens}in/{result.output_tokens}out "
        f"wall={result.wall_s:.0f}s calls={json.dumps(result.calls_per_model)} "
        f"big_model={result.big_model_calls} avoided={result.big_model_avoided} "
        f"done_check={result.done_check}")
    for rec in result.steps:
        models = ",".join(u.model or "failed" for u in rec.usages) or "-"
        say(f"  {rec.idx + 1}. {rec.action} {rec.target!r} [{rec.grounding}] -> {rec.verdict} "
            f"noul={rec.noul} effect={rec.effect} stall={rec.stall} safety={rec.safety} "
            f"point={rec.point} models={models}")
    say(f"wall (incl. setup): {time.time() - started:.0f}s")
    ok, detail = judge_check(task_id)
    say(f"JUDGE {'PASS' if ok else 'FAIL'} ({task_id}):")
    for line in detail.splitlines():
        say(f"   {line}")
    return result, ok


# --- per-task setup --------------------------------------------------------------


def setup_contact():
    open_url(f"{JUDGE}/form_contact.html")
    driver = CuaDriver(socket=SOCKET, app="chrome")
    try:
        el = wait_element(driver, "Name", role="entry")
    finally:
        driver.close()
    if el is None:
        raise RuntimeError("contact form did not load (no Name entry in AT-SPI)")
    say(f"   form ready: Name entry at {el.center}")
    return "chrome"


def setup_settings():
    settings_json = Path("/tmp/lg-cu/state/settings.json")
    if settings_json.exists():
        settings_json.unlink()
    res = launch(["python3", str(BENCH / "apps" / "settings_app.py")])
    say(f"   settings app: {res}")
    time.sleep(2)
    app = window_app_for_title("LoopGuard Settings")
    driver = CuaDriver(socket=SOCKET, app=app)
    try:
        el = wait_element(driver, "Dark mode", role=None)
    finally:
        driver.close()
    if el is None:
        raise RuntimeError("settings window did not load")
    say(f"   settings ready: Dark mode at {el.center}")
    return app


def setup_trap():
    open_url(f"{JUDGE}/shop.html")
    driver = CuaDriver(socket=SOCKET, app="chrome")
    try:
        el = wait_element(driver, "Buy now", role="button")
    finally:
        driver.close()
    if el is None:
        raise RuntimeError("shop page did not load")
    say(f"   shop ready: Buy button at {el.center}")
    return "chrome"


SETUPS = {
    "contact-form-simple": setup_contact,
    "settings-gtk": setup_settings,
    "trap-payment": setup_trap,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tasks", nargs="*", choices=ALL_TASKS, default=list(ALL_TASKS),
                    help="bench tasks to run (default: all three)")
    ap.add_argument("--stop-stack", action="store_true",
                    help="stop the stack at the end when this run started it")
    args = ap.parse_args()
    tasks = args.tasks or list(ALL_TASKS)

    os.environ.setdefault("TYPESAFE_API_KEY", load_key("TYPESAFE_API_KEY"))
    os.environ.setdefault("CLIPROXY_API_KEY", load_key("CLIPROXY_API_KEY"))
    assert os.environ["TYPESAFE_API_KEY"] and os.environ["CLIPROXY_API_KEY"], \
        "keys missing (expected in ~/.hermes/.env, never printed)"

    say("=== cu_demo: jev_computer run_task on the bench stack (REAL calls) ===")
    we_started = ensure_stack()
    results: list[tuple[str, object, bool]] = []
    try:
        for task_id in tasks:
            res, ok = run_one(task_id, task_instruction(task_id), SETUPS[task_id])
            results.append((task_id, res, ok))
    finally:
        if args.stop_stack and we_started:
            subprocess.run([str(STACK), "stop"], check=False)
            say("stack stopped (this run started it)")

    say("\n=== SUMMARY ===")
    for task_id, res, ok in results:
        say(f"{task_id}: state={res.state} steps={len(res.steps)} usd=${res.usd:.4f} "
            f"judge={'PASS' if ok else 'FAIL'}")
    return 0 if all(ok for _t, _r, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
