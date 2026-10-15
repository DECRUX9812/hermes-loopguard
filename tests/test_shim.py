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
VERSION = repr(tuple(sys.version_info[:2]))

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
    shim.write_text(
        SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(PKG_ROOT), site_dirs="[]", version=VERSION),
        encoding="utf-8",
    )

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
    shim.write_text(
        SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(PKG_ROOT), site_dirs="[]", version=VERSION),
        encoding="utf-8",
    )

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
    shim.write_text(SHIM_TEMPLATE.format(name="shimdemo", pkg_root=repr(str(root)), site_dirs="[]", version=VERSION), encoding="utf-8")

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


# A stub hermes_loopguard whose cron.main imports a module that lives ONLY
# in a recorded site-packages dir; prints "dep <VALUE>" if reachable.
SITE_DEP_STUB_CRON = """\
def main(argv):
    import lg_extra_dep
    print('dep ' + lg_extra_dep.VALUE)
    return 0
"""


def _write_site_dep_fixtures(tmp_path: Path, site_dir: Path) -> Path:
    dep = site_dir / "lg_extra_dep"
    dep.mkdir(parents=True)
    (dep / "__init__.py").write_text("VALUE = 'from-recorded-site-dir'\n", encoding="utf-8")
    root = tmp_path / "pkgroot"
    pkg = root / "hermes_loopguard"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cron.py").write_text(SITE_DEP_STUB_CRON, encoding="utf-8")
    return root


def test_shim_appends_recorded_site_dirs_on_matching_version(tmp_path: Path):
    """A module living only in a recorded site-packages dir is importable
    from the shim when the interpreter version matches the recording."""
    site_dir = tmp_path / "site-packages"
    root = _write_site_dep_fixtures(tmp_path, site_dir)
    shim = tmp_path / "loopguard_sitepath.py"
    shim.write_text(
        SHIM_TEMPLATE.format(
            name="sitepathdemo",
            pkg_root=repr(str(root)),
            site_dirs=repr([str(site_dir)]),
            version=VERSION,
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(shim)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "dep from-recorded-site-dir"


def test_shim_skips_recorded_site_dirs_on_version_mismatch(tmp_path: Path):
    """A version mismatch (here 0.0) leaves the recorded dirs off sys.path,
    so the site-dir-only dependency stays unimportable."""
    site_dir = tmp_path / "site-packages"
    root = _write_site_dep_fixtures(tmp_path, site_dir)
    shim = tmp_path / "loopguard_sitepath.py"
    shim.write_text(
        SHIM_TEMPLATE.format(
            name="sitepathdemo",
            pkg_root=repr(str(root)),
            site_dirs=repr([str(site_dir)]),
            version=repr((0, 0)),
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-I", str(shim)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0
    assert "lg_extra_dep" in proc.stderr
