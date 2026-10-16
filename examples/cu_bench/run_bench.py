#!/usr/bin/env python3
"""Bench runner: run one arm (baseline big-model loop or the Jev arm) over the
bench tasks, judge success with CODE ONLY (judge log / files / state JSON —
never a model), and write one JSONL row per (task, repeat).

    python3 run_bench.py --arm baseline --tasks all --repeat 1 \
        --out examples/cu_bench/results/baseline-<ts>.jsonl
    python3 run_bench.py --summary          # print the BENCH table from results/

Per task x repeat: reset the stack state (scoped app kills + the task `setup`),
run the arm, evaluate the task's `judge` block against ONLY the judge-log
entries recorded during this run, and record task, arm, success, terminal
state, steps, wall time, model calls per model, input/output tokens, $ (list
prices via cu/pricing.py — the SAME table prices both arms) and big-model
calls.  A loud error names any model that has no price row.

The baseline arm is aborted once its cumulative list-price spend crosses
--spend-cap (default $3.00) and the abort is reported.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(HERE))

import yaml  # noqa: E402

from hermes_loopguard.config import DEFAULTS  # noqa: E402
from hermes_loopguard.cu import pricing  # noqa: E402
from judge import read_log  # noqa: E402
from judge_check import evaluate  # noqa: E402

SOCKET = "/tmp/lg-cu/cua.sock"
STACK_LOCK = "/tmp/lg-cu/stack.lock"
RESULTS = HERE / "results"


def _comm_matches(actual: str, wanted: str) -> bool:
    """Case-insensitive comm check: mousepad's /proc comm is 'mousepad', so a
    case-sensitive startswith('Mousepad') never matched and the editor (plus
    its modal Save As dialog) survived every reset, haunting later tasks."""
    return actual.strip().lower().startswith(wanted.strip().lower())


def kill_bench_apps() -> None:
    """Scoped reset: kill ONLY processes this bench launched (never pkill -f
    broad): our chrome profile, mousepad, thunar on our folder, settings_app.
    Mirrors P2's smoke.py."""
    patterns = [
        ("user-data-dir=/tmp/lg-cu/chrome-profile", "chrome"),
        ("mousepad", "Mousepad"),
        ("thunar /tmp/lg-cu/files/fm-task", "thunar"),
        ("settings_app.py", "python3"),
    ]
    for needle, comm in patterns:
        out = subprocess.run(
            ["pgrep", "-f", needle], capture_output=True, text=True
        ).stdout.split()
        for pid in out:
            try:
                actual = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            if _comm_matches(actual, comm):
                try:
                    subprocess.run(["kill", pid], capture_output=True)
                except ProcessLookupError:
                    pass
    # Wait until the killed apps are really gone (chrome writes its profile on
    # exit; launching a fresh one too early shows a blank/loading page or hits
    # the SingletonLock race, which can stall the next start for minutes).
    import glob as _glob
    import os as _os

    for _ in range(40):
        if subprocess.run(
            ["pgrep", "-f", "user-data-dir=/tmp/lg-cu/chrome-profile|mousepad|thunar /tmp/lg-cu/files/fm-task|settings_app.py"],
            capture_output=True,
        ).returncode != 0:
            break
        time.sleep(0.5)
    # Remove stale chrome singleton links whose owner pid is dead.
    for link in _glob.glob("/tmp/lg-cu/chrome-profile/Singleton*"):
        try:
            target = _os.readlink(link)
            pid = int(target.rsplit("-", 1)[-1])
            if Path(f"/proc/{pid}").exists():
                continue  # owner alive (shouldn't happen after the wait)
        except (OSError, ValueError):
            pass
        try:
            Path(link).unlink()
        except OSError:
            pass
    # Purge chrome form history/autofill so leftover typed values (e.g. P2
    # smoke's "Ada Lovelace") can't pop an autocomplete dropdown mid-task.
    for stale in _glob.glob("/tmp/lg-cu/chrome-profile/*/Web Data") + _glob.glob(
        "/tmp/lg-cu/chrome-profile/*/Form History"
    ):
        try:
            Path(stale).unlink()
        except OSError:
            pass
    time.sleep(1.5)


def launcher_request(req: dict, timeout: float = 30.0) -> dict:
    """Send one request to the app launcher socket ({"reset": true} or
    {"argv": [...]}); returns its JSON response ({} on any failure)."""
    import socket as _socket

    sock_path = "/tmp/lg-cu/launcher.sock"
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock_path)
            s.sendall((json.dumps(req) + "\n").encode())
            s.shutdown(_socket.SHUT_WR)
            out = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        return json.loads(out.decode().strip() or "{}")
    except (OSError, json.JSONDecodeError) as e:
        return {"error": f"launcher request failed: {e}"}


def load_tasks(which: str) -> list[dict]:
    tasks = []
    for path in sorted(HERE.glob("tasks/*.yaml")):
        task = yaml.safe_load(path.read_text())
        task["_path"] = str(path)
        tasks.append(task)
    if which != "all":
        wanted = [t.strip() for t in which.split(",") if t.strip()]
        tasks = [t for t in tasks if t["id"] in wanted]
        missing = set(wanted) - {t["id"] for t in tasks}
        if missing:
            raise SystemExit(f"unknown task ids: {sorted(missing)}")
    return tasks


def check_pricing(arm: str) -> None:
    """Fail loudly before ANY spend when a model this arm may call has no row."""
    cu = DEFAULTS["computer_use"]
    if arm == "baseline":
        import baseline as baseline_mod

        models = list(baseline_mod.MODEL_CHAIN)
    else:
        models = [
            cu.get("planner_model", ""),
            *(cu.get("models", [])),
            (cu.get("escalation") or {}).get("model", ""),
        ]
        models = [m.split(":", 1)[1] if m.startswith("openai:") else m for m in models]
    missing = pricing.unpriced_models([m for m in models if m])
    if missing:
        raise SystemExit(
            f"PRICING ERROR: no price row for {missing} (arm={arm}). "
            f"Add them to hermes_loopguard/cu/pricing.py — refusing to run "
            f"something that would silently price at $0."
        )


def task_app(task: dict) -> str | None:
    """Which app the arm should drive for this task (the CuaDriver needs the
    app filter so foreground type/key can find and activate the window)."""
    for step in task.get("setup", []):
        if isinstance(step, dict):
            if "open_url" in step:
                return "chrome"
            argv = (step.get("launch") or [None])[0] if step.get("launch") else None
            if argv:
                base = Path(str(argv)).name.lower()
                if "mousepad" in base:
                    return "Mousepad"
                if "thunar" in base:
                    return "thunar"
                if "settings_app" in base:
                    return "Settings_app"
    return None


def run_arm(arm: str, task: dict, repeat: int, run_dir: Path, cfg_extra: dict | None = None) -> dict:
    from hermes_loopguard.cu.driver import get_driver

    max_steps = int(task.get("max_steps", 12))
    instruction = " ".join(task["instruction"].split())
    frame_dir = run_dir / "frames" / f"{task['id']}-r{repeat}"
    driver = get_driver("cua", socket=SOCKET, app=task_app(task))
    try:
        if arm == "baseline":
            import baseline as baseline_mod

            result = baseline_mod.run_baseline(
                instruction, driver, max_steps=max_steps, frame_dir=frame_dir
            )
            return {
                "terminal_state": result.state,
                "steps": result.steps,
                "wall_s": round(result.wall_s, 2),
                "calls_per_model": result.calls_per_model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "usd": round(result.usd, 6),
                "big_model_calls": result.big_model_calls,
                "reason": result.reason,
                "frames": result.frame_dir,
                "trace": [
                    {
                        "turn": t.idx,
                        "action": t.action,
                        "point": t.point,
                        "effect": t.effect,
                        "error": t.error,
                        "reason": t.reason,
                        "latency_s": round(t.latency_s, 2),
                        "frame": t.frame,
                    }
                    for t in result.turns
                ],
            }
        # jev arm: X3's run_task
        import copy

        from hermes_loopguard.cu import agent as cu_agent
        from hermes_loopguard.cu.log import CuRunLogger
        from hermes_loopguard.cu.vision import VisionGate

        cfg = copy.deepcopy(DEFAULTS["computer_use"])
        cfg["max_steps"] = max_steps
        if cfg_extra:
            cfg.update(cfg_extra)
        gate = VisionGate(cfg)
        log = CuRunLogger(store=None, base_dir=run_dir / "cu-frames" / f"{task['id']}-r{repeat}")
        res = cu_agent.run_task(instruction, driver, gate, cfg, log=log)
        return {
            "terminal_state": res.state,
            "steps": len(res.steps),
            "wall_s": round(res.wall_s, 2),
            "calls_per_model": res.calls_per_model,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
            "usd": round(res.usd, 6),
            "big_model_calls": res.big_model_calls,
            "reason": res.reason,
            "frames": str(run_dir / "cu-frames" / f"{task['id']}-r{repeat}"),
            # Cheap-path proof: fallback reasons (VisionUsage.fallbacks across
            # the run) and vision calls per model.  A row whose vision calls
            # are not jev-* did NOT drive on the cheap path (see --summary).
            "fallbacks": list(res.fallbacks),
            "fallback_count": len(res.fallbacks),
            "vision_calls_by_model": dict(res.vision_calls_by_model),
            "trace": [
                {
                    "idx": rec.idx,
                    "action": rec.action,
                    "target": rec.target,
                    "grounding": rec.grounding,
                    "verdict": rec.verdict,
                    "noul": rec.noul,
                    "effect": rec.effect,
                    "stall": rec.stall,
                    "safety": rec.safety,
                    "point": rec.point,
                }
                for rec in res.steps
            ],
        }
    finally:
        driver.close()


def judge_task(task: dict, log_index_before: int) -> tuple[bool, list[str]]:
    entries = read_log()[log_index_before:]
    specs = task.get("judge", [])
    if isinstance(specs, dict):
        specs = [specs]
    details = []
    ok_all = True
    for spec in specs:
        ok, detail = evaluate(spec, entries)
        ok_all = ok_all and ok
        details.append(("PASS " if ok else "FAIL ") + detail)
    return ok_all, details


def list_windows_cli(timeout: float = 10.0) -> list[dict]:
    """cua-driver list_windows via the CLI (returns [] on any failure)."""
    try:
        out = subprocess.run(
            ["cua-driver", "call", "--socket", SOCKET, "list_windows", "{}"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
        windows = json.loads(out).get("windows", [])
        return [w for w in windows if isinstance(w, dict)]
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError):
        return []


# Window-manager / desktop shells that are NOT bench apps (the display-empty
# check must not count them as leftovers).
_WM_APPS = ("xfwm", "xfce4", "marco", "mutter", "muffin", "openbox")


def _is_bench_app_window(w: dict) -> bool:
    """True for real app windows; False for the desktop WM and cua-driver's
    own pid-less, full-screen cursor overlay (never clickable, never a leak)."""
    app = str(w.get("app_name", "")).lower()
    if any(name in app for name in _WM_APPS):
        return False
    if not w.get("pid") and ("cua" in app or "cursor" in str(w.get("title", "")).lower()):
        return False
    return bool(app) or bool(w.get("pid"))


def wait_display_empty(timeout_s: float = 30.0, poll_s: float = 1.0) -> str | None:
    """After the per-task reset: verify NO bench app window is left on :97
    before the next task starts.  Returns None when empty, else an error
    naming the leftover windows (REAL leak: a Mousepad Save-As dialog and a
    LibreOffice Writer window survived the old pattern-based kills and sat on
    the display between tasks, ruining the next task's screenshots)."""
    deadline = time.time() + timeout_s
    leftovers: list[dict] = []
    while time.time() < deadline:
        windows = list_windows_cli()
        leftovers = [w for w in windows if _is_bench_app_window(w)]
        if not leftovers:
            return None
        time.sleep(poll_s)
    names = ", ".join(
        f"{w.get('app_name', '?')}/{str(w.get('title', ''))[:40]}" for w in leftovers
    )
    return f"display :97 not empty after the reset; leftover windows: {names}"


def frame_is_blank(png: bytes, threshold: float = 2.0) -> bool:
    """True when the screenshot is (near-)uniform — a window that is mapped
    but has not painted yet (REAL failure: P6B captured a black settings
    frame because the first GTK map under the disposable HOME took >10 s)."""
    from io import BytesIO

    from PIL import Image, ImageStat

    try:
        img = Image.open(BytesIO(png)).convert("L")
    except Exception:
        return True  # unreadable frame: treat as not-ready, keep waiting
    stat = ImageStat.Stat(img)
    return float(stat.stddev[0]) < threshold


def wait_app_ready(
    app: str | None,
    *,
    driver=None,
    timeout_s: float = 120.0,
    poll_s: float = 1.0,
) -> str | None:
    """Block until the task's app window is mapped, exposes a11y elements and
    has actually painted (not a blank frame).  Bounded; returns a clear error
    string on timeout, None when ready.  Runs for BOTH arms — the arms must
    never start on a half-mapped desktop."""
    if not app:
        return None
    from hermes_loopguard.cu.driver import get_driver

    own = driver is None
    if driver is None:
        driver = get_driver("cua", socket=SOCKET, app=app)
    try:
        deadline = time.time() + timeout_s
        last = "no probe ran"
        while time.time() < deadline:
            try:
                els = driver.elements()
            except Exception as e:
                els, last = [], f"a11y probe failed: {e}"
            if not els:
                last = "app window not mapped / exposes no a11y elements yet"
            else:
                try:
                    frame = driver.screenshot()
                    if frame_is_blank(frame.png):
                        last = "window is mapped but the frame is blank (still painting)"
                    else:
                        return None
                except Exception as e:
                    last = f"screenshot failed: {e}"
            time.sleep(poll_s)
        return f"app {app!r} not ready after {timeout_s:.0f}s: {last}"
    finally:
        if own:
            try:
                driver.close()
            except Exception:
                pass


def infra_error_reason(row: dict) -> str:
    """Why this row's failure is INFRASTRUCTURE (model endpoint error/timeout,
    driver capture error, app never became ready), or "" when it is a task
    failure.  Infra rows stay in the data; BENCH.md reports them separately
    instead of as task failures."""
    state = str(row.get("terminal_state", ""))
    reason = str(row.get("reason", ""))
    if row.get("success"):
        return ""  # infra errors only explain failures, never successes
    if state in ("error", "infra_error"):
        return reason or "runner/infra exception"
    if reason.startswith("driver screenshot failed"):
        return reason  # the run died on a driver capture failure
    trace = row.get("trace") or []
    if trace and not trace[-1].get("action"):
        error = str(trace[-1].get("error") or "")
        if error:
            # The run ended on a model call that never answered (baseline
            # "every model failed: <model>: timed out" shape).
            return f"model endpoint failed: {error}"
    # Jev arm: a failing step whose grounding never got a model answer — the
    # locate calls all died on endpoint errors (TypeSafe 400s, timeouts, 429s),
    # so the step failure is infrastructure, not agent behavior.
    if any(
        t.get("verdict") == "fail" and str(t.get("effect", "")).startswith("grounding failed")
        for t in trace
    ) and _endpoint_errors(row):
        return "grounding locate calls died on model endpoint errors: " + _endpoint_errors(row)
    return ""


def _endpoint_errors(row: dict) -> str:
    """A short summary of the endpoint errors in the row's fallback list."""
    import re as _re

    errors = []
    for fb in row.get("fallbacks") or []:
        text = str(fb)
        if _re.search(r"(timed out|HTTP Error|System One call failed|non-JSON stdout|Capture err)", text):
            model = text.split(":", 1)[0].strip()
            if model == "openai" and ":" in text:
                model = "openai:" + text.split(":")[1].strip()  # "openai:<model>"
            kind = ("timeout" if "timed out" in text
                    else "HTTP 400 image dimensions" if "Invalid image dimensions" in text
                    else "HTTP error" if "HTTP Error" in text
                    else "capture error" if "Capture err" in text
                    else "endpoint error")
            errors.append(f"{model}: {kind}")
    seen: list[str] = []
    for e in errors:
        if e not in seen:
            seen.append(e)
    return "; ".join(seen[:4]) + (f" (+{len(seen) - 4} more)" if len(seen) > 4 else "")


def wait_browser_ready(timeout_s: float = 120.0) -> None:
    """After open_url setup: poll cua list_windows until a chrome window has a
    real page title — chrome titles end with " - Google Chrome" only once the
    page has a document; "Loading…"/"untitled" never match.  Free — no model
    turns burned watching chrome cold-start (28s warm, minutes worst case)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        windows = list_windows_cli()
        for w in windows:
            title = str(w.get("title", "")).strip()
            lower = title.lower()
            if (
                "chrome" in str(w.get("app_name", "")).lower()
                and lower.endswith("google chrome")
                and not lower.startswith(("untitled", "loading"))
            ):
                return
        time.sleep(1.0)
    print("  WARNING: browser page title never became ready", flush=True)


def default_out(arm: str) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    return RESULTS / f"{arm}-{ts}.jsonl"


def write_row(out: Path, row: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def preflight_stack() -> None:
    """Fail fast when the stack is not up (never write garbage rows)."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8793/healthz", timeout=5):
            pass
    except Exception as e:
        raise SystemExit(f"STACK DOWN: judge /healthz failed: {e}")
    from hermes_loopguard.cu.driver import get_driver

    driver = get_driver("cua", socket=SOCKET)
    try:
        frame = driver.screenshot()
        print(f"preflight: screenshot ok ({frame.width}x{frame.height})", flush=True)
    except Exception as e:
        raise SystemExit(f"STACK DOWN: driver screenshot failed: {e}")
    finally:
        driver.close()


def ensure_stack() -> None:
    """Start the lg-cu-stack unit when it is inactive (X3 does start/probe/stop
    cycles on the shared unit, so it may be down when we get the lock)."""
    active = (
        subprocess.run(
            ["systemctl", "--user", "is-active", "lg-cu-stack"],
            capture_output=True, text=True,
        ).stdout.strip()
        == "active"
    )
    if not active:
        print("stack unit inactive; starting it (stack.sh start) ...", flush=True)
        res = subprocess.run(
            [str(HERE / "stack.sh"), "start"], capture_output=True, text=True
        )
        print(res.stdout.strip() or res.stderr.strip(), flush=True)
        if res.returncode != 0:
            raise SystemExit(f"stack.sh start failed: {res.stderr[-400:]}")


def load_secrets() -> None:
    """Load ~/.hermes/.env keys into the process env (bench harness only, not the plugin).
    Never prints values.  Background units do not inherit the interactive
    shell's exports, and paths.get_secret() depends on them."""
    env_file = Path.home() / ".hermes" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip().strip('"')
        if name in ("CLIPROXY_API_KEY", "TYPESAFE_API_KEY") and value and not os.environ.get(name):
            os.environ[name] = value


# --- Jev-arm preflight (the invalid first run must stay impossible) ---------


def _import_typesafe_sdk():
    import typesafe_sdk  # noqa: F401

    return typesafe_sdk


def _jev_ping(model: str = "jev-1.14.0", timeout: float = 45.0) -> str:
    """One tiny REAL System One call through the SDK path (a few tokens).

    Returns the model id that actually served the request; raises when the
    Jev call fails or a fallback model answered instead.
    """
    from hermes_loopguard.backends.typesafe import TypeSafeBackend
    from hermes_loopguard.questions import Noul

    backend = TypeSafeBackend({"typesafe": {"timeout": timeout}})
    result = backend.ask(
        "Preflight: answer the question about integers.",
        {"ok": Noul(instructions="Is 2 + 2 equal to 4?")},
        model=model,
    )
    usage = getattr(result, "usage", None)
    used = str(
        getattr(result, "model", "") or getattr(usage, "model", "") or ""
    )
    if model not in used:
        raise RuntimeError(f"preflight request was served by {used!r}, not {model}")
    return used


def preflight_jev(_import_sdk=None, _ping=None, _key=None) -> None:
    """Exit non-zero BEFORE any spend unless the Jev arm can really run.

    Guards the failure mode of the invalid first Jev run
    (an unpublished early result set): run_bench.py executed under a python
    without typesafe-sdk, every Jev request failed, and VisionGate silently
    fell back to the CLIProxy model — a whole "Jev arm" with zero jev-* calls.
    """
    try:
        (_import_sdk or _import_typesafe_sdk)()
    except Exception as e:
        raise SystemExit(
            "JEV PREFLIGHT FAILED: typesafe-sdk is not importable from this "
            f"interpreter ({e}). Run the bench with "
            "a Python with the bench extras (typesafe-sdk, pillow) and PYTHONPATH=src (see BENCH.md) "
            "— otherwise every Jev call fails and the gate falls back silently."
        )
    key = _key if _key is not None else os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        raise SystemExit(
            "JEV PREFLIGHT FAILED: TYPESAFE_API_KEY is not set (load ~/.hermes/.env "
            "or run_bench.py's load_secrets must have found it). Refusing to run "
            "a Jev arm that would silently fall back."
        )
    try:
        used = str((_ping or _jev_ping)())
    except Exception as e:
        raise SystemExit(
            "JEV PREFLIGHT FAILED: tiny real jev-1.14.0 call did not succeed: "
            f"{e}. Fix the endpoint/key first; refusing to burn a run on "
            "guaranteed fallbacks."
        )
    if "jev-1.14.0" not in used:
        raise SystemExit(
            f"JEV PREFLIGHT FAILED: the tiny call was served by {used!r}, "
            "not jev-1.14.0 (fallback chain active). Refusing to run."
        )
    print(f"jev preflight: sdk importable, key present, tiny call ok (served by {used})", flush=True)


def run(args) -> int:
    load_secrets()
    if args.arm == "jev":
        preflight_jev()
    check_pricing(args.arm)
    tasks = load_tasks(args.tasks)
    out = Path(args.out) if args.out else default_out(args.arm)
    run_dir = out.with_suffix("")
    run_dir.mkdir(parents=True, exist_ok=True)

    lock = open(STACK_LOCK, "w")  # noqa: SIM115 — held for the whole run
    print(f"waiting for stack lock {STACK_LOCK} ...", flush=True)
    fcntl.flock(lock, fcntl.LOCK_EX)
    ensure_stack()  # inside the lock: nobody else can stop it mid-run
    preflight_stack()
    print(f"lock held; {len(tasks)} task(s) x {args.repeat} repeat(s), arm={arm_name(args.arm)}", flush=True)

    spent = 0.0
    try:
        for task in tasks:
            for repeat in range(1, args.repeat + 1):
                if (
                    args.arm == "baseline"
                    and args.spend_cap is not None
                    and spent >= args.spend_cap
                ):
                    print(
                        f"ABORT: baseline list-price spend ${spent:.4f} >= cap "
                        f"${args.spend_cap:.2f}; remaining tasks not run.",
                        flush=True,
                    )
                    return 2
                print(f"=== {task['id']} repeat {repeat}: reset ===", flush=True)
                # Per-TASK reset (X8): the launcher kills every app it spawned
                # (tracked PIDs + process groups, SIGTERM -> SIGKILL) and THEN
                # wipes the disposable HOME once per task.  The scoped pattern
                # kills below stay as a fallback for apps not spawned through
                # the launcher; the display must be EMPTY before the next task.
                reset_resp = launcher_request({"reset": True})
                if reset_resp.get("error"):
                    print(f"  launcher reset error: {reset_resp['error']}", flush=True)
                elif reset_resp.get("alive"):
                    print(f"  WARNING: tracked apps survived the reset: {reset_resp['alive']}", flush=True)
                kill_bench_apps()
                if (err := wait_display_empty()) is not None:
                    print(f"  RESET ERROR: {err}", flush=True)
                log_index_before = len(read_log())
                setup = subprocess.run(
                    [sys.executable, str(HERE / "setup.py"), task["id"]],
                    capture_output=True, text=True,
                )
                if setup.returncode != 0:
                    print(f"  setup FAILED: {setup.stderr[-400:]}", flush=True)
                is_browser = any(
                    (isinstance(s, dict) and "open_url" in s) or (isinstance(s, str) and s.startswith("open_url"))
                    for s in task.get("setup", [])
                )
                if is_browser:
                    wait_browser_ready()
                # Both arms start only on a mapped, a11y-exposed, painted app
                # window (bounded wait); otherwise the row is an infra error.
                app = task_app(task)
                ready_err = wait_app_ready(app)
                if ready_err is not None:
                    print(f"  APP READY ERROR: {ready_err}", flush=True)
                    row = {
                        "task": task["id"],
                        "arm": args.arm,
                        "repeat": repeat,
                        "ts": time.time(),
                        "model": (baseline_model() if args.arm == "baseline" else planner_model()),
                        "terminal_state": "infra_error",
                        "steps": 0,
                        "wall_s": 0.0,
                        "calls_per_model": {},
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "usd": 0.0,
                        "big_model_calls": 0,
                        "reason": ready_err,
                        "infra_error": ready_err,
                    }
                    ok, details = judge_task(task, log_index_before)
                    row["success"] = ok
                    row["judge"] = details
                    row["unpriced_models"] = []
                    write_row(out, row)
                    for d in details:
                        print(f"     {d}", flush=True)
                    continue
                time.sleep(2.0)  # settle after the app is up

                print(f"=== {task['id']} repeat {repeat}: run {arm_name(args.arm)} ===", flush=True)
                row = {
                    "task": task["id"],
                    "arm": args.arm,
                    "repeat": repeat,
                    "ts": time.time(),
                    "model": (baseline_model() if args.arm == "baseline" else planner_model()),
                }
                try:
                    result = run_arm(args.arm, task, repeat, run_dir)
                    row.update(result)
                except Exception as e:  # driver/infra failure: record, keep going
                    row.update(
                        {
                            "terminal_state": "error",
                            "steps": 0,
                            "wall_s": 0.0,
                            "calls_per_model": {},
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "usd": 0.0,
                            "big_model_calls": 0,
                            "reason": f"runner exception: {e!r}",
                        }
                    )

                ok, details = judge_task(task, log_index_before)
                row["success"] = ok
                row["judge"] = details
                # Infrastructure failures (endpoint errors/timeouts, driver
                # capture errors) are REPORTED SEPARATELY, not dropped and
                # not silently counted as plain task failures (X8).
                row["infra_error"] = infra_error_reason(row)
                if row["infra_error"]:
                    print(f"  INFRA ERROR: {row['infra_error']}", flush=True)
                # Flag unpriced models on this row (should be none: check_pricing).
                unpriced = pricing.unpriced_models(list(row["calls_per_model"]))
                row["unpriced_models"] = unpriced
                tokens_known = row["input_tokens"] or row["output_tokens"]
                if tokens_known and unpriced:
                    print(f"  PRICING WARNING: {unpriced} have no price row; $ understated", flush=True)
                spent += float(row["usd"] or 0.0)
                write_row(out, row)
                print(
                    f"  -> success={ok} state={row['terminal_state']} steps={row['steps']} "
                    f"tokens={row['input_tokens']}/{row['output_tokens']} "
                    f"${row['usd']:.4f} ({row['reason'][:80]})",
                    flush=True,
                )
                for d in details:
                    print(f"     {d}", flush=True)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    print(f"done; rows in {out} (baseline spend this run: ${spent:.4f})", flush=True)
    return 0


def arm_name(arm: str) -> str:
    return "baseline (big-model CU loop)" if arm == "baseline" else "jev (cheap-path driver)"


def baseline_model() -> str:
    import baseline as baseline_mod

    return baseline_mod.MODEL_CHAIN[0]


def planner_model() -> str:
    return DEFAULTS["computer_use"].get("planner_model", "")


# --- summary (the BENCH table) ----------------------------------------------------


def load_rows(results_dir: Path | None = None) -> list[dict]:
    rows = []
    for path in sorted((results_dir or RESULTS).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Aggregate rows -> per-arm totals and per-(task, arm) cells.  Pure math
    (unit-tested); rows are the JSONL records run_bench writes."""
    arms: dict[str, dict] = {}
    for row in rows:
        arm = row["arm"]
        cell = arms.setdefault(
            arm,
            {
                "attempts": 0,
                "successes": 0,
                "steps": 0.0,
                "wall_s": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "usd": 0.0,
                "big_model_calls": 0,
                "infra_errors": 0,
                "tasks": {},
            },
        )
        cell["attempts"] += 1
        cell["successes"] += 1 if row["success"] else 0
        cell["steps"] += row.get("steps", 0)
        cell["wall_s"] += row.get("wall_s", 0.0)
        cell["input_tokens"] += row.get("input_tokens", 0)
        cell["output_tokens"] += row.get("output_tokens", 0)
        cell["usd"] += row.get("usd", 0.0)
        cell["big_model_calls"] += row.get("big_model_calls", 0)
        if row.get("infra_error"):
            cell["infra_errors"] += 1
        tcell = cell["tasks"].setdefault(row["task"], {"attempts": 0, "successes": 0, "usd": 0.0})
        tcell["attempts"] += 1
        tcell["successes"] += 1 if row["success"] else 0
        tcell["usd"] += row.get("usd", 0.0)
    for cell in arms.values():
        n = cell["attempts"]
        cell["avg_steps"] = cell["steps"] / n if n else 0.0
        cell["avg_wall_s"] = cell["wall_s"] / n if n else 0.0
        cell["usd_per_success"] = cell["usd"] / cell["successes"] if cell["successes"] else None
    return arms


def non_jev_vision(row: dict) -> dict[str, int]:
    """Vision-gate calls served by anything other than a jev-* model.

    This is the silent-fallback signature of the invalid first run (a "Jev"
    arm whose vision calls are all CLIProxy).  Returns {model: calls}.
    """
    return {
        m: n
        for m, n in (row.get("vision_calls_by_model") or {}).items()
        if not str(m).startswith("jev-")
    }


def flag_fallback_rows(rows: list[dict]) -> int:
    """Print a loud flag for every row with non-Jev vision calls or recorded
    fallbacks.  Returns the number of flagged rows."""
    flagged = 0
    for row in rows:
        non_jev = non_jev_vision(row)
        fallbacks = row.get("fallbacks") or []
        if not non_jev and not fallbacks:
            continue
        flagged += 1
        label = f"{row.get('arm', '?')}/{row.get('task', '?')}/r{row.get('repeat', '?')}"
        if non_jev:
            print(
                f"  FLAG {label}: {sum(non_jev.values())} NON-JEV vision call(s) "
                f"{non_jev} — the cheap path did NOT serve this row"
                + ("" if row.get("big_model_calls") else " (silent fallback?)"),
                flush=True,
            )
        for reason in fallbacks:
            print(f"  FALLBACK {label}: {reason}", flush=True)
    return flagged


def summary(results_dir: Path | None = None) -> None:
    rows = load_rows(results_dir)
    if not rows:
        print("no result rows found")
        return
    agg = aggregate(rows)
    task_ids = sorted({r["task"] for r in rows})
    header = f"{'task':24s} | " + " | ".join(
        f"{a:>28s}" for a in ("baseline", "jev") if a in agg
    )
    print(header)
    print("-" * len(header))
    for tid in task_ids:
        cells = []
        for armname in ("baseline", "jev"):
            cell = agg.get(armname, {}).get("tasks", {}).get(tid)
            if cell:
                cells.append(f"{cell['successes']}/{cell['attempts']} ok ${cell['usd']:.4f}")
            else:
                cells.append("-")
        print(f"{tid:24s} | " + " | ".join(f"{c:>28s}" for c in cells))
    print("-" * len(header))
    for armname, cell in agg.items():
        ups = (
            f"${cell['usd_per_success']:.4f}"
            if cell['usd_per_success'] is not None
            else "n/a"
        )
        failures = cell["attempts"] - cell["successes"]
        print(
            f"{armname:9s}: {cell['successes']}/{cell['attempts']} succeeded, "
            f"avg steps {cell['avg_steps']:.1f}, wall {cell['wall_s']:.0f}s, "
            f"tokens {cell['input_tokens']}/{cell['output_tokens']}, "
            f"${cell['usd']:.4f} total, ${ups} per success, "
            f"big-model calls {cell['big_model_calls']}, "
            f"infra errors {cell.get('infra_errors', 0)} "
            f"({failures - cell.get('infra_errors', 0)} task failure(s))"
        )
    if "baseline" in agg and "jev" in agg:
        b, j = agg["baseline"]["usd"], agg["jev"]["usd"]
        ratio = (j / b) if b > 0 else None
        print(
            f"cost ratio jev/baseline: {ratio:.3f}" if ratio is not None
            else "cost ratio undefined (baseline $0)"
        )
    flagged = flag_fallback_rows(rows)
    if flagged:
        print(
            f"WARNING: {flagged} row(s) with non-Jev vision calls or fallbacks "
            "(see FLAG/FALLBACK lines above); BENCH.md must not use those rows "
            "as Jev-arm results."
        )
    else:
        print("all rows served their vision calls on the jev-* cheap path")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", choices=("baseline", "jev"))
    ap.add_argument("--tasks", default="all", help="all, or comma-separated task ids")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default=None, help="results JSONL path (default results/<arm>-<ts>.jsonl)")
    ap.add_argument("--summary", action="store_true", help="print the BENCH table from results/ and exit")
    ap.add_argument("--spend-cap", type=float, default=3.0,
                    help="abort the baseline arm above this list-price total (USD, default 3.0)")
    args = ap.parse_args()

    if args.summary:
        summary()
        return 0
    if not args.arm:
        ap.error("--arm is required (or use --summary)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
