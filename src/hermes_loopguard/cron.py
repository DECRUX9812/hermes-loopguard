from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, TextIO

from hermes_loopguard import paths
from hermes_loopguard.backends import backend_chain
from hermes_loopguard.config import load_config, load_watcher
from hermes_loopguard.db import Store
from hermes_loopguard.router import poll

WAKE_TRUE = '{"wakeAgent": true}'
WAKE_FALSE = '{"wakeAgent": false}'

SHIM_TEMPLATE = """\
import sys
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
            pass

    if chain_factory is None:
        chain_factory = lambda: backend_chain(cfg)

    try:
        spec = load_watcher(name)
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
        on_error = cfg.get("on_error", "skip") if cfg else "skip"
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
