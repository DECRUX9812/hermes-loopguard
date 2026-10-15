"""The cron shim must run on an interpreter that does not have
hermes_loopguard (or the outer venv's site-packages) on sys.path —
that is how stock Hermes runs `$HERMES_HOME/scripts/*.py`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import hermes_loopguard
from hermes_loopguard.cron import SHIM_TEMPLATE

PKG_ROOT = str(Path(hermes_loopguard.__file__).resolve().parent.parent)

WATCHER_YAML = """\
name: shimdemo
source:
  file: {issues!r}
id_field: id
title_field: title
"""


def _run_shim(shim: Path, home: Path, db: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "HERMES_HOME", "LOOPGUARD_DB")}
    env["HERMES_HOME"] = str(home)
    env["LOOPGUARD_DB"] = str(db)
    # -I: isolated mode — PYTHONPATH is ignored and the script dir is not
    # added to sys.path, so only the shim's embedded pkg_root can rescue it.
    return subprocess.run(
        [sys.executable, "-I", str(shim)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_shim_wakes_then_skips_without_external_path(tmp_path: Path):
    home = tmp_path / "home"
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"id": "1", "title": "Fix the login crash"}]), encoding="utf-8")

    watchers = home / "loopguard" / "watchers"
    watchers.mkdir(parents=True)
    (watchers / "shimdemo.yaml").write_text(WATCHER_YAML.format(issues=str(issues)), encoding="utf-8")

    shim = tmp_path / "loopguard_shimdemo.py"
    shim.write_text(SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(PKG_ROOT)), encoding="utf-8")

    db = tmp_path / "lg.db"

    first = _run_shim(shim, home, db)
    assert first.returncode == 0, first.stderr
    assert first.stdout.rstrip().splitlines()[-1] == '{"wakeAgent": true}'

    second = _run_shim(shim, home, db)
    assert second.returncode == 0, second.stderr
    assert second.stdout.rstrip().splitlines()[-1] == '{"wakeAgent": false}'
    assert "LoopGuard error" not in second.stderr


def test_shim_honours_per_watcher_config_override(tmp_path: Path):
    home = tmp_path / "home"
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"id": "1", "title": "Fix the login crash"}]), encoding="utf-8")

    watchers = home / "loopguard" / "watchers"
    watchers.mkdir(parents=True)
    (watchers / "shimdemo.yaml").write_text(
        WATCHER_YAML.format(issues=str(issues)) + "config:\n  shadow: true\n",
        encoding="utf-8",
    )

    shim = tmp_path / "loopguard_shimdemo.py"
    shim.write_text(SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(PKG_ROOT)), encoding="utf-8")

    # Shadow mode comes from the watcher's config override: the brief is empty
    # (routes logged, agent woken exactly as before) even on the first run.
    first = _run_shim(shim, home, tmp_path / "lg.db")
    assert first.returncode == 0, first.stderr
    assert first.stdout == '{"wakeAgent": true}\n'


def test_shim_pkg_root_containing_quote(tmp_path: Path):
    """A package root with `"` in its path must still yield a runnable shim."""
    root = tmp_path / 'we"ird"root'
    pkg = root / "hermes_loopguard"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cron.py").write_text(
        "def main(argv):\n    print('stub ' + argv[0])\n    return 0\n",
        encoding="utf-8",
    )

    shim = tmp_path / "loopguard_shimdemo.py"
    shim.write_text(SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(str(root))), encoding="utf-8")

    # -I again: hermes_loopguard is not importable, so the shim must rescue
    # itself via the embedded (quoted) path and reach the stub package.
    proc = subprocess.run(
        [sys.executable, "-I", str(shim)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "stub shimdemo"
