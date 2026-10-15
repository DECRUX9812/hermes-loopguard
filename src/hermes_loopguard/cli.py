"""`hermes loopguard ...` CLI.

`setup_parser` is shared by Hermes (`ctx.register_cli_command`) and by
`main()`, so both surfaces expose exactly the same subcommands.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from hermes_loopguard import _yaml, paths

DEFAULT_PROMPT = (
    "You are woken by LoopGuard only when items need work. Handle each item in the "
    "LoopGuard brief above, then report each one as DONE <id> or BLOCKED <id>: <reason>."
)


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="lg_command", metavar="COMMAND")

    p = sub.add_parser("stats", help="Show savings and activity")
    p.add_argument("--watcher", default=None, help="Limit to one watcher")
    p.add_argument("--since", default=None, help="Window: 30m, 24h, 7d, 30d")
    p.add_argument("--json", action="store_true", help="Emit JSON")

    p = sub.add_parser("decisions", help="Show the decision log")
    p.add_argument("--watcher", default=None)
    p.add_argument("--kind", default=None)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("poll", help="Run one watcher poll now")
    p.add_argument("watcher")
    p.add_argument("--dry-run", action="store_true", help="Write nothing")

    watch = sub.add_parser("watch", help="Manage watchers")
    wsub = watch.add_subparsers(dest="lg_watch_command", metavar="ACTION")
    wsub.add_parser("list", help="List installed watchers")
    wi = wsub.add_parser("install", help="Install a watcher and its cron job")
    wi.add_argument("spec", help="Path to the watcher YAML")
    wi.add_argument("--schedule", default="every 15m")
    wi.add_argument("--prompt", default=None)
    wi.add_argument("--deliver", default="local")
    wi.add_argument("--no-cron", action="store_true", help="Write files only")
    wr = wsub.add_parser("remove", help="Remove a watcher and its shim")
    wr.add_argument("name")

    review = sub.add_parser("review", help="Operator review queue")
    rsub = review.add_subparsers(dest="lg_review_command", metavar="ACTION")
    rsub.add_parser("list", help="List open reviews")
    ra = rsub.add_parser("approve", help="Force the agent route for an item")
    ra.add_argument("id", type=int)
    rd = rsub.add_parser("drop", help="Drop an item")
    rd.add_argument("id", type=int)

    p = sub.add_parser("check", help="Check GOAL.md completion")
    p.add_argument("goal", nargs="?", default="GOAL.md")
    p.add_argument("--cwd", default=None)
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("eval", help="Run the gate eval harness")
    p.add_argument("cases", help="JSONL cases file")
    p.add_argument("--watcher", required=True, help="Watcher name or YAML path")
    p.add_argument("--backend", default=None, choices=["auto", "typesafe", "openai", "hermes", "local"])
    p.add_argument("--json", action="store_true")

    sub.add_parser("doctor", help="Check the LoopGuard install")

    dash = sub.add_parser("dashboard", help="Dashboard plugin page")
    dsub = dash.add_subparsers(dest="lg_dashboard_command", metavar="ACTION")
    dsub.add_parser("install", help="Copy the dashboard page into Hermes")

    p = sub.add_parser("shadow", help="Turn shadow mode on or off")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--watcher", default=None)

    p = sub.add_parser("reset", help="Forget item state so items are re-evaluated")
    p.add_argument("watcher")
    p.add_argument("item_id", nargs="?", default=None)


def handle(args) -> int:
    command = getattr(args, "lg_command", None)
    if command is None:
        return 0
    handler = _HANDLERS.get(command)
    if handler is None:
        return 1
    return handler(args) or 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-loopguard", description="LoopGuard")
    setup_parser(parser)
    args = parser.parse_args(argv)
    return handle(args)


# --- stats / decisions --------------------------------------------------
def _cmd_stats(args) -> int:
    from hermes_loopguard.config import load_config
    from hermes_loopguard.db import Store
    from hermes_loopguard.stats import compute_stats, format_stats

    store = Store.open_default()
    try:
        stats = compute_stats(store, load_config(), watcher=args.watcher, since=args.since)
    finally:
        store.close()
    if args.json:
        _print_json(stats)
    else:
        print(format_stats(stats))
    return 0


def _cmd_decisions(args) -> int:
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        rows = store.decisions(watcher=args.watcher, kind=args.kind, limit=args.limit)
    finally:
        store.close()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("No decisions recorded.")
        return 0
    for row in rows:
        stamp = _short_time(row.get("created_at"))
        detail = row.get("route") or row.get("proposed_route") or ""
        outcome = row.get("outcome") or ""
        note = row.get("note") or ""
        print(
            f"{stamp}  {str(row.get('watcher'))[:18]:<18} {str(row.get('kind'))[:11]:<11} "
            f"{str(row.get('item_id') or '-')[:14]:<14} {str(detail)[:10]:<10} "
            f"{str(outcome)[:12]:<12} {note[:40]}"
        )
    return 0


def _short_time(ts) -> str:
    if not ts:
        return "-"
    import time as _time

    return _time.strftime("%m-%d %H:%M", _time.localtime(ts))


# --- poll ---------------------------------------------------------------
def _cmd_poll(args) -> int:
    from hermes_loopguard import router
    from hermes_loopguard.backends import backend_chain
    from hermes_loopguard.config import deep_merge, load_config, load_watcher
    from hermes_loopguard.db import Store

    paths.bootstrap_hermes_env()
    cfg = load_config()
    try:
        spec = load_watcher(args.watcher)
    except Exception as e:
        print(f"Cannot load watcher '{args.watcher}': {e}", file=sys.stderr)
        return 1
    if spec.config:
        cfg = deep_merge(cfg, spec.config)

    store = Store.open_default()
    try:
        result = router.poll(
            spec,
            store=store,
            cfg=cfg,
            owner=f"cli:{spec.name}:{os.getpid()}",
            chain_factory=lambda: backend_chain(cfg),
            dry_run=args.dry_run,
        )
    finally:
        store.close()
    _print_poll(result, dry_run=args.dry_run)
    return 0


def _print_poll(result, *, dry_run: bool = False) -> None:
    suffix = " (dry run, nothing written)" if dry_run else ""
    print(f"Watcher: {result.watcher}{suffix}")
    counts: dict[str, int] = {}
    for delta in result.deltas:
        counts[delta.status] = counts.get(delta.status, 0) + 1
    if counts:
        print("Items: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    else:
        print("Items: none")
    if result.routed:
        print("Routes:")
        for routed in result.routed:
            prob = f" p={routed.prob:.2f}" if routed.prob is not None else ""
            print(
                f"  {routed.delta.item_id}: {routed.route} -> {routed.handler} "
                f"[{routed.outcome}]{prob}"
            )
    print(f"Wake agent: {'yes' if result.wake else 'no'}")
    if result.shadow:
        print("Shadow mode: the route was logged; the run wakes exactly as before")
    if result.brief:
        print("")
        print(result.brief)


# --- watch --------------------------------------------------------------
def _cmd_watch(args) -> int:
    action = getattr(args, "lg_watch_command", None)
    if action == "list" or action is None:
        return _cmd_watch_list(args)
    if action == "install":
        return _cmd_watch_install(args)
    if action == "remove":
        return _cmd_watch_remove(args)
    return 1


def _cmd_watch_list(args) -> int:
    from hermes_loopguard.config import list_watchers

    names = list_watchers()
    if not names:
        print("No watchers installed.")
        return 0
    for name in names:
        yaml_path = paths.watchers_dir() / f"{name}.yaml"
        shim = paths.scripts_dir() / f"loopguard_{name}.py"
        print(
            f"{name:<22} spec {'yes' if yaml_path.is_file() else 'no ':>3}   "
            f"shim {'yes' if shim.is_file() else 'no'}"
        )
    return 0


def _recorded_site_dirs() -> list[str]:
    """Site-packages dirs of the interpreter installing the watcher.

    These hold the pip dependencies (typesafe-sdk among them) that Hermes's
    own cron interpreter lacks; the shim appends them at run time.
    """
    import site

    dirs: list[str] = []
    try:
        dirs.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        user_site = site.getusersitepackages()
    except Exception:
        user_site = None
    if user_site and os.path.isdir(user_site):
        dirs.append(user_site)
    seen: set[str] = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def _cmd_watch_install(args) -> int:
    import hermes_loopguard

    from hermes_loopguard.config import watcher_from_dict
    from hermes_loopguard.cron import SHIM_TEMPLATE

    src = Path(args.spec).expanduser()
    if not src.is_file():
        print(f"Watcher spec not found: {src}", file=sys.stderr)
        return 1
    with open(src, "r", encoding="utf-8") as fh:
        data = _yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        print(f"Watcher spec must be a YAML mapping: {src}", file=sys.stderr)
        return 1
    if "name" not in data:
        data["name"] = src.stem
    try:
        spec = watcher_from_dict(data)
    except Exception as e:
        print(f"Invalid watcher spec: {e}", file=sys.stderr)
        return 1

    wdir = paths.watchers_dir()
    wdir.mkdir(parents=True, exist_ok=True)
    dest = wdir / f"{spec.name}.yaml"
    dest.write_text(_yaml.safe_dump(data), encoding="utf-8")

    sdir = paths.scripts_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    shim = sdir / f"loopguard_{spec.name}.py"
    pkg_root = Path(hermes_loopguard.__file__).resolve().parent.parent
    site_dirs = _recorded_site_dirs()
    shim.write_text(
        SHIM_TEMPLATE.format(
            name=spec.name,
            pkg_root=repr(str(pkg_root)),
            site_dirs=repr(site_dirs),
            version=repr(tuple(sys.version_info[:2])),
        ),
        encoding="utf-8",
    )
    print(f"  shim site dirs: {len(site_dirs)} recorded "
          f"(python {sys.version_info[0]}.{sys.version_info[1]})")

    cmd = [
        "hermes",
        "cron",
        "add",
        args.schedule,
        args.prompt or DEFAULT_PROMPT,
        "--name",
        f"lg-{spec.name}",
        "--script",
        f"loopguard_{spec.name}.py",
        "--deliver",
        args.deliver,
    ]
    printable = " ".join(shlex.quote(part) for part in cmd)
    print(f"Installed watcher '{spec.name}'")
    print(f"  spec: {dest}")
    print(f"  shim: {shim}")
    if args.no_cron:
        print("  cron: skipped (--no-cron)")
        print(f"  run later: {printable}")
        return 0
    print(f"  cron: {printable}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("  'hermes' is not on PATH; run the command above manually", file=sys.stderr)
        return 0
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)
        return 1
    return 0


def _cmd_watch_remove(args) -> int:
    removed = []
    for candidate in (
        paths.watchers_dir() / f"{args.name}.yaml",
        paths.watchers_dir() / f"{args.name}.yml",
        paths.scripts_dir() / f"loopguard_{args.name}.py",
    ):
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate)
    if not removed:
        print(f"No watcher named '{args.name}'", file=sys.stderr)
        return 1
    for path in removed:
        print(f"removed {path}")
    print(f"The cron job is left in place; remove it with: hermes cron remove lg-{args.name}")
    return 0


# --- review -------------------------------------------------------------
def _review_row(store, review_id: int) -> dict | None:
    cur = store.conn.cursor()
    cur.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
    return cur.fetchone()


def _not_found(review_id: int) -> Exception:
    """Raise the dashboard's 404 type when we are called from its API."""
    try:
        from hermes_loopguard.dashboard.plugin_api import HTTPException

        return HTTPException(status_code=404, detail=f"Review {review_id} not found")
    except Exception:
        return LookupError(f"Review {review_id} not found")


def first_agent_route(watcher: str) -> str | None:
    try:
        from hermes_loopguard.config import load_watcher

        spec = load_watcher(watcher)
    except Exception:
        return None
    gate = spec.gate if isinstance(spec.gate, dict) else None
    routes = (gate or {}).get("routes") or {}
    for name, rule in routes.items():
        handler = rule.get("handler") if isinstance(rule, dict) else getattr(rule, "handler", None)
        if handler == "agent":
            return name
    return None


def review_approve(store, review_id: int) -> str:
    row = _review_row(store, review_id)
    if row is None:
        raise _not_found(review_id)
    watcher = row["watcher"]
    item_id = row["item_id"]
    route = first_agent_route(watcher) or "agent"
    store.set_kv(f"force_route:{watcher}:{item_id}", route)
    store.upsert_item(watcher, item_id, fingerprint="")
    store.resolve_review(review_id, "approved")
    return route


def review_drop(store, review_id: int) -> None:
    row = _review_row(store, review_id)
    if row is None:
        raise _not_found(review_id)
    watcher = row["watcher"]
    item_id = row["item_id"]
    if store.get_item(watcher, item_id) is None:
        store.upsert_item(watcher, item_id, fingerprint="")
    store.set_item_state(watcher, item_id, "dropped")
    store.resolve_review(review_id, "dropped")


def _cmd_review(args) -> int:
    from hermes_loopguard.db import Store

    action = getattr(args, "lg_review_command", None) or "list"
    store = Store.open_default()
    try:
        if action == "list":
            rows = store.review_queue("open")
            if not rows:
                print("Review queue is empty.")
                return 0
            for row in rows:
                print(
                    f"{row['id']:>4}  {str(row.get('watcher'))[:20]:<20} "
                    f"{str(row.get('item_id'))[:16]:<16} {str(row.get('title') or '')[:40]:<40} "
                    f"{str(row.get('reason') or '')[:40]}"
                )
            return 0
        if action == "approve":
            route = review_approve(store, args.id)
            print(f"Review {args.id} approved; the next poll forces route '{route}'")
            return 0
        if action == "drop":
            review_drop(store, args.id)
            print(f"Review {args.id} dropped")
            return 0
    except Exception as e:
        print(f"review {action} failed: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 1


# --- check --------------------------------------------------------------
def _cmd_check(args) -> int:
    from hermes_loopguard import completion

    cwd = args.cwd or os.getcwd()
    goal = args.goal or "GOAL.md"
    path = goal if os.path.isabs(goal) else os.path.join(cwd, goal)

    chain = None
    if not args.no_semantic:
        try:
            from hermes_loopguard.backends import backend_chain
            from hermes_loopguard.config import load_config

            chain = backend_chain(load_config())
        except Exception:
            chain = None

    try:
        verdict = completion.check_goal(path, cwd=cwd, chain=chain)
    except Exception as e:
        print(f"check failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(
            {
                "goal": path,
                "met": verdict.met,
                "code_ok": verdict.code_ok,
                "semantic_ok": verdict.semantic_ok,
                "semantic_prob": verdict.semantic_prob,
                "reasons": verdict.reasons,
                "checks": [
                    {"raw": r.check.raw, "ok": r.ok, "detail": r.detail} for r in verdict.results
                ],
            }
        )
    else:
        print(f"GOAL {path}: {'met' if verdict.met else 'not met'}")
        for result in verdict.results:
            print(f"  [{'ok' if result.ok else 'FAIL'}] {result.check.raw}")
            if not result.ok and result.detail:
                print(f"         {result.detail}")
        for reason in verdict.reasons:
            print(f"  - {reason}")
    return 0 if verdict.met else 1


# --- eval ---------------------------------------------------------------
def _cmd_eval(args) -> int:
    from hermes_loopguard import evals
    from hermes_loopguard.backends import backend_chain
    from hermes_loopguard.config import load_config, load_watcher
    from hermes_loopguard.semantic_gate import gate_spec_from_dict

    cfg = load_config()
    if args.backend:
        cfg = dict(cfg)
        cfg["backend"] = args.backend
    try:
        spec = load_watcher(args.watcher)
    except Exception as e:
        print(f"Cannot load watcher '{args.watcher}': {e}", file=sys.stderr)
        return 1
    if not spec.gate:
        print(f"Watcher '{spec.name}' has no gate configured", file=sys.stderr)
        return 1
    gate = gate_spec_from_dict(spec.gate)
    try:
        cases = evals.load_cases(args.cases)
    except Exception as e:
        print(f"Cannot load cases '{args.cases}': {e}", file=sys.stderr)
        return 1

    report = evals.run_eval(gate, cases, backend_chain(cfg))
    if args.json:
        _print_json(
            {
                "n": report.n,
                "accuracy": report.accuracy,
                "usage": report.usage,
                "confusion": report.confusion,
                "sweep": report.sweep,
                "misses": [
                    {"id": row.case.id, "expected": row.case.expected_route,
                     "predicted": row.predicted_route}
                    for row in report.misses
                ],
                "false_admits": [
                    {"id": row.case.id, "expected": row.case.expected_route,
                     "predicted": row.predicted_route}
                    for row in report.false_admits
                ],
            }
        )
    else:
        print(evals.format_report(report))
    return 0


# --- doctor -------------------------------------------------------------
def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _cmd_doctor(args) -> int:
    from hermes_loopguard.backends import make_backend
    from hermes_loopguard.config import list_watchers, load_config

    cfg = load_config()
    print("LoopGuard doctor")
    print(f"  hermes home: {paths.hermes_home()}")
    print(f"  data dir:    {paths.data_dir()}")
    print(f"  database:    {paths.db_path()}")
    config_file = paths.config_dir() / "config.yaml"
    print(f"  config:      {config_file} ({'present' if config_file.is_file() else 'missing'})")

    for name in ("typesafe", "openai", "hermes", "local"):
        try:
            available = make_backend(name, cfg).available()
        except Exception:
            available = False
        print(f"  backend {name:<9} {'available' if available else 'unavailable'}")

    env_names = ["TYPESAFE_API_KEY"]
    openai_key_env = (cfg.get("openai") or {}).get("api_key_env")
    if openai_key_env:
        env_names.append(str(openai_key_env))
    for env_name in env_names:
        print(f"  env {env_name:<20} {'set' if paths.get_secret(env_name) else 'missing'}")

    print(f"  typesafe_sdk {'importable' if _importable('typesafe_sdk') else 'missing'}")
    print(f"  hermes       {'importable' if _importable('hermes_cli') else 'missing'}")

    enabled = None
    try:
        from hermes_cli.config import load_config as hermes_load_config

        hermes_cfg = hermes_load_config() or {}
        enabled = "loopguard" in ((hermes_cfg.get("plugins") or {}).get("enabled") or [])
    except Exception:
        enabled = None
    if enabled is None:
        print("  plugin       unknown (hermes not importable)")
    else:
        print(f"  plugin       {'enabled' if enabled else 'not enabled'}")

    names = list_watchers()
    if not names:
        print("  watchers     none installed")
    for name in names:
        shim = paths.scripts_dir() / f"loopguard_{name}.py"
        print(f"  watcher {name:<14} shim {'present' if shim.is_file() else 'missing'}")
    return 0


# --- dashboard ----------------------------------------------------------
def _cmd_dashboard(args) -> int:
    from importlib import resources

    action = getattr(args, "lg_dashboard_command", None)
    if action != "install":
        print("Usage: hermes loopguard dashboard install", file=sys.stderr)
        return 1

    dest = paths.hermes_home() / "plugins" / "loopguard-dashboard" / "dashboard"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    source = resources.files("hermes_loopguard.dashboard")
    with resources.as_file(source) as source_path:
        shutil.copytree(source_path, dest, dirs_exist_ok=True)
    print(f"Installed dashboard page to {dest}")
    print("Reload the dashboard plugin registry:")
    print("  curl http://127.0.0.1:9119/api/dashboard/plugins/rescan")
    return 0


# --- shadow / reset -----------------------------------------------------
def _cmd_shadow(args) -> int:
    enabled = args.state == "on"
    if args.watcher:
        path = None
        for ext in (".yaml", ".yml"):
            candidate = paths.watchers_dir() / f"{args.watcher}{ext}"
            if candidate.is_file():
                path = candidate
                break
        if path is None:
            print(f"Watcher not found: {args.watcher}", file=sys.stderr)
            return 1
        data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
        data["shadow"] = enabled
        path.write_text(_yaml.safe_dump(data), encoding="utf-8")
        print(f"shadow {'on' if enabled else 'off'} for watcher '{args.watcher}' ({path})")
        return 0

    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.yaml"
    data = {}
    if path.is_file():
        data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        data = {}
    data["shadow"] = enabled
    path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    print(f"shadow {'on' if enabled else 'off'} ({path})")
    return 0


def _cmd_reset(args) -> int:
    from hermes_loopguard.db import Store

    store = Store.open_default()
    try:
        cur = store.conn.cursor()
        if args.item_id:
            cur.execute(
                "DELETE FROM items WHERE watcher = ? AND item_id = ?", (args.watcher, args.item_id)
            )
            cur.execute(
                "DELETE FROM claims WHERE watcher = ? AND item_id = ?", (args.watcher, args.item_id)
            )
            print(f"reset {args.watcher}:{args.item_id}")
        else:
            cur.execute("DELETE FROM items WHERE watcher = ?", (args.watcher,))
            cur.execute("DELETE FROM claims WHERE watcher = ?", (args.watcher,))
            print(f"reset all items for watcher '{args.watcher}'")
    finally:
        store.close()
    return 0


_HANDLERS = {
    "stats": _cmd_stats,
    "decisions": _cmd_decisions,
    "poll": _cmd_poll,
    "watch": _cmd_watch,
    "review": _cmd_review,
    "check": _cmd_check,
    "eval": _cmd_eval,
    "doctor": _cmd_doctor,
    "dashboard": _cmd_dashboard,
    "shadow": _cmd_shadow,
    "reset": _cmd_reset,
}
