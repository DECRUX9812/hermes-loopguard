from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, TextIO

from hermes_loopguard import paths
from hermes_loopguard.backends import backend_chain
from hermes_loopguard.config import deep_merge, load_config, load_watcher
from hermes_loopguard.db import Store
from hermes_loopguard.router import poll

WAKE_TRUE = '{"wakeAgent": true}'
WAKE_FALSE = '{"wakeAgent": false}'

# Hermes runs cron .py scripts on its own PM-managed interpreter, where the
# outer venv's site-packages (and therefore hermes_loopguard and its venv-only
# dependencies such as typesafe-sdk) are NOT on sys.path. The shim embeds, as
# repr'd literals (so paths with quotes or backslashes stay valid Python):
#   - the install-time package root, inserted only when the import fails;
#   - the installing interpreter's site-packages dirs, APPENDED (never
#     prepended, so Hermes's own packages keep priority) and only when the
#     running interpreter's major.minor matches the recording one.
SHIM_TEMPLATE = """\
import os
import sys
if sys.version_info[:2] == {version}:
    for _site_dir in {site_dirs}:
        if os.path.isdir(_site_dir) and _site_dir not in sys.path:
            sys.path.append(_site_dir)
try:
    import hermes_loopguard
except ImportError:
    sys.path.insert(0, {pkg_root})
from hermes_loopguard.cron import main
raise SystemExit(main(["{name}"]))
"""


def run_watcher(
    name: str,
    *,
    cfg: dict | None = None,
    store: Store | None = None,
    chain_factory: Callable[[], list[Any]] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    dry_run: bool = False,
) -> int:
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    paths.bootstrap_hermes_env()
    owner = f"cron:{name}:{os.getpid()}:{int(time.time())}"

    if cfg is None:
        cfg = load_config()

    if store is None:
        try:
            store = Store.open_default()
        except Exception as e:
            # A broken store must never silently mute the automation (a run
            # with no store could only ever print {"wakeAgent": false}).
            # Warn once on stderr, which Hermes captures, and apply the
            # on_error policy here: "skip" keeps the agent asleep, anything
            # else (the default is "wake") fails open so LoopGuard is never
            # worse than not having it.
            err.write(
                f"LoopGuard: could not open the decision store ({e}); "
                f"watcher '{name}' cannot gate this run\n"
            )
            on_error = cfg.get("on_error", "wake") if cfg else "wake"
            if on_error == "skip":
                out.write(WAKE_FALSE + "\n")
            else:
                out.write(WAKE_TRUE + "\n")
            return 0

    if chain_factory is None:
        chain_factory = lambda: backend_chain(cfg)

    try:
        spec = load_watcher(name)
        if spec.config:
            cfg = deep_merge(cfg, spec.config)
        res = poll(
            spec,
            store=store,
            cfg=cfg,
            owner=owner,
            chain_factory=chain_factory,
            dry_run=dry_run,
        )
        if res.brief:
            out.write(res.brief.rstrip() + "\n")
        if res.wake:
            out.write(WAKE_TRUE + "\n")
        else:
            out.write(WAKE_FALSE + "\n")
        return 0
    except Exception as e:
        err.write(f"LoopGuard error in watcher '{name}': {e}\n")
        if store is not None:
            try:
                store.log_decision(
                    watcher=name,
                    kind="change_gate",
                    outcome="error",
                    note=str(e)[:300],
                )
            except Exception:
                pass
        on_error = cfg.get("on_error", "wake") if cfg else "wake"
        if on_error == "skip":
            out.write(WAKE_FALSE + "\n")
        else:
            out.write(WAKE_TRUE + "\n")
        return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        sys.stderr.write("Usage: hermes-loopguard cron <watcher_name> [--dry-run]\n")
        return 1

    dry_run = "--dry-run" in argv
    clean_argv = [a for a in argv if a != "--dry-run"]
    if not clean_argv:
        sys.stderr.write("Usage: hermes-loopguard cron <watcher_name> [--dry-run]\n")
        return 1

    name = clean_argv[0]
    return run_watcher(name, dry_run=dry_run)
