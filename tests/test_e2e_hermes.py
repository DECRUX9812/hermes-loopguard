"""End-to-end tests: the loopguard plugin entry point on real stock Hermes.

These run the real ``hermes`` binary (stock NousResearch/hermes-agent) in
subprocesses against a throwaway HERMES_HOME. They exist to catch the bug
class where the plugin imports fine in the outer venv but fails inside
Hermes: the CLI rewires ``sys.path`` to its PM-managed environment under
``$HERMES_HOME/installs/``, so the outer venv's site-packages (pyyaml, ...)
are NOT importable inside a hermes process. A plugin that depends on them
silently fails to register and ``hermes loopguard`` is "not a hermes
command".

Environment:
- ``LOOPGUARD_E2E_HERMES``: hermes binary to test. Default: the ship-test
  lg-venv binary when it exists. The module skips cleanly when absent.
- ``LOOPGUARD_E2E_INSTALLS``: path to an already-bootstrapped PM
  ``installs/`` dir; symlinked into the test home so the first hermes
  command skips the several-minute (~240 MB) bootstrap. Without it, the
  first command bootstraps once (hence its 600s timeout).

No network, no model calls: the watcher round trip uses ``backend: local``
and the shim is executed directly (never via ``hermes cron run``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

_DEFAULT_HERMES = Path("/path/to/lg-venv/bin/hermes")
HERMES_BIN = os.environ.get("LOOPGUARD_E2E_HERMES") or (
    str(_DEFAULT_HERMES) if _DEFAULT_HERMES.is_file() else ""
)
if not HERMES_BIN:
    pytest.skip(
        "stock hermes binary not found; set LOOPGUARD_E2E_HERMES to run e2e tests",
        allow_module_level=True,
    )

HERMES_PY = Path(HERMES_BIN).parent / "python"
# The first hermes command in a fresh HERMES_HOME bootstraps the PM
# environment under <home>/installs (~240 MB, minutes). Every later command
# reuses it and is fast.
_FIRST_COMMAND_TIMEOUT = 600
_COMMAND_TIMEOUT = 180

_bootstrapped = False


@pytest.fixture(scope="module")
def hermes_home(tmp_path_factory) -> Path:
    """One HERMES_HOME for the whole module. The PM environment bootstrap is
    paid at most once; a prepared installs dir (LOOPGUARD_E2E_INSTALLS) is
    symlinked in so even that first command stays fast."""
    home = tmp_path_factory.mktemp("lg-e2e-home")
    prepared = os.environ.get("LOOPGUARD_E2E_INSTALLS", "")
    if prepared and Path(prepared).is_dir():
        (home / "installs").symlink_to(Path(prepared).resolve(), target_is_directory=True)
    return home


def _child_env(home: Path) -> dict[str, str]:
    """Env for hermes/shim subprocesses.

    PYTHONPATH is stripped on purpose: the repo's pytest invocation sets
    PYTHONPATH=src, which would put hermes_loopguard on sys.path in every
    child and mask exactly the PM-env import bug these tests exist to catch.
    LOOPGUARD_DB (set for the unit tests by tests/conftest.py) must not leak
    into the children either.
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "LOOPGUARD_DB")
    }
    env["HERMES_HOME"] = str(home)
    # `hermes loopguard watch install` shells out to a bare `hermes cron add`.
    hermes_dir = str(Path(HERMES_BIN).parent)
    env["PATH"] = hermes_dir + os.pathsep + env.get("PATH", "")
    return env


def run_hermes(
    home: Path, *args: str, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real stock hermes binary with HERMES_HOME=<home>."""
    global _bootstrapped
    if timeout is None:
        timeout = _COMMAND_TIMEOUT if _bootstrapped else _FIRST_COMMAND_TIMEOUT
    _bootstrapped = True
    return subprocess.run(
        [HERMES_BIN, *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_child_env(home),
        timeout=timeout,
    )


def _run_probe(home: Path, code: str, *argv: str, timeout: int | None = None) -> dict:
    """Run a probe snippet on the hermes venv's own interpreter.

    Hermes' PM bootstrap redirects the process stdout, so probes print their
    LG<MARKER>-tagged JSON result to stderr.
    """
    r = subprocess.run(
        [str(HERMES_PY), "-c", code, *argv],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=_child_env(home),
        timeout=timeout or _COMMAND_TIMEOUT,
    )
    assert r.returncode == 0, f"probe failed\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    lines = [ln for ln in r.stderr.splitlines() if ln.startswith("LGRESULT")]
    assert lines, f"probe produced no LGRESULT line\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    return json.loads(lines[-1][len("LGRESULT"):])


# --- 1. plugin opt-in -------------------------------------------------------

def test_plugins_enable_loopguard(hermes_home: Path):
    """`hermes plugins enable loopguard` succeeds and writes the allow-list."""
    r = run_hermes(hermes_home, "plugins", "enable", "loopguard")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    cfg = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8")) or {}
    enabled = (cfg.get("plugins") or {}).get("enabled") or []
    assert "loopguard" in enabled, f"config.yaml plugins.enabled = {enabled!r}"


# --- 2. the CLI command registers ------------------------------------------

def test_loopguard_help_lists_subcommands(hermes_home: Path):
    """`hermes loopguard --help` works on stock Hermes — this is exactly what
    breaks when plugin.register() dies on imports the PM env lacks."""
    r = run_hermes(hermes_home, "loopguard", "--help")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    for sub in ("stats", "decisions", "watch", "review", "doctor", "dashboard"):
        assert sub in r.stdout, f"subcommand {sub!r} missing from --help output"


# --- 3. stats on an empty home ---------------------------------------------

def test_stats_on_empty_home(hermes_home: Path):
    """`hermes loopguard stats` exits 0 with no watchers installed."""
    r = run_hermes(hermes_home, "loopguard", "stats")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "LoopGuard" in r.stdout


# --- 4. registration inside the Hermes process -----------------------------

def test_plugin_registration_in_hermes_process(hermes_home: Path):
    """Entry-point discovery registers the loopguard CLI command and its
    hooks inside a real Hermes process (after the PM sys.path rewiring)."""
    r = run_hermes(hermes_home, "plugins", "enable", "loopguard")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    probe = "\n".join([
        "import json, sys",
        # Importing hermes_cli.main triggers Hermes' PM sys.path rewiring:
        # from here on the outer venv's site-packages are gone.
        "import hermes_cli.main",
        "from hermes_cli.plugins import discover_plugins, get_plugin_manager",
        "discover_plugins()",
        "pm = get_plugin_manager()",
        "print('LGRESULT' + json.dumps({",
        "    'cli_commands': sorted(pm._cli_commands),",
        "    'hooks': {name: len(cbs) for name, cbs in pm._hooks.items()},",
        "}), file=sys.stderr)",
    ])
    result = _run_probe(hermes_home, probe)
    assert "loopguard" in result["cli_commands"], result
    assert result["hooks"].get("pre_tool_call", 0) >= 1, result
    assert result["hooks"].get("post_api_request", 0) >= 1, result


# --- 5. watcher round trip, backend: local ----------------------------------

def test_watcher_round_trip_local_backend(hermes_home: Path, tmp_path: Path):
    """watch install -> shim under $HERMES_HOME/scripts + cron job; running
    the shim twice on Hermes' own cron interpreter wakes once, then the
    change gate holds the second poll at {"wakeAgent": false}."""
    issues = [
        {"id": "E2E-1", "title": "Critical bug: login crashes on empty email"},
        {"id": "E2E-2", "title": "Spam: buy cheap followers now"},
    ]
    source = tmp_path / "issues.json"
    source.write_text(json.dumps(issues), encoding="utf-8")

    spec = tmp_path / "e2e-demo.yaml"
    spec.write_text(yaml.safe_dump({
        "name": "e2e-demo",
        "source": {"file": str(source)},
        "id_field": "id",
        "title_field": "title",
        "gate": {
            "route_question": "kind",
            "questions": {
                "kind": {
                    "type": "choice",
                    "instructions": "What does this issue need?",
                    "criteria": {
                        "bug": "a defect in existing behaviour",
                        "feature": "a request for new behaviour",
                    },
                },
            },
            "routes": {
                "bug": {"handler": "agent", "min_prob": 0.4, "brief": "Fix the bug."},
            },
            "default_route": "drop",
        },
        "config": {"backend": "local"},
    }), encoding="utf-8")

    r = run_hermes(hermes_home, "loopguard", "watch", "install", str(spec),
                   "--schedule", "every 15m")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"

    shim = hermes_home / "scripts" / "loopguard_e2e-demo.py"
    assert shim.is_file(), f"shim not created; install output:\n{r.stdout}\n{r.stderr}"

    r = run_hermes(hermes_home, "cron", "list")
    assert r.returncode == 0, f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    assert "lg-e2e-demo" in r.stdout, f"cron job missing from cron list:\n{r.stdout}"

    argv, overlay = _cron_shim_argv(hermes_home, shim)
    env = _child_env(hermes_home)
    env.update(overlay)
    outs = []
    for i in range(2):
        r = subprocess.run(
            argv, capture_output=True, text=True, env=env,
            cwd=str(tmp_path), stdin=subprocess.DEVNULL, timeout=_COMMAND_TIMEOUT,
        )
        assert r.returncode == 0, (
            f"shim run {i + 1} failed\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"
        )
        outs.append(r.stdout)

    # Run 1 admits new items -> the agent wakes. Run 2 sees an unchanged file
    # -> the change gate short-circuits at zero tokens.
    assert outs[0].strip().splitlines()[-1] == '{"wakeAgent": true}', outs[0]
    assert outs[1].strip().splitlines()[-1] == '{"wakeAgent": false}', outs[1]


def _cron_shim_argv(home: Path, shim: Path) -> tuple[list[str], dict[str, str]]:
    """The argv + env overlay Hermes' cron scheduler uses to run a .py script,
    computed by the hermes checkout itself (cron/scheduler_script.py::
    _script_argv) so the reproduction can never drift from upstream."""
    probe = "\n".join([
        "import json, sys",
        "from pathlib import Path",
        "from cron.scheduler_script import _script_argv",
        "argv, overlay, err = _script_argv(Path(sys.argv[1]))",
        "assert err is None, err",
        "print('LGRESULT' + json.dumps({'argv': argv, 'overlay': overlay}), file=sys.stderr)",
    ])
    result = _run_probe(home, probe, str(shim))
    return result["argv"], result["overlay"]
