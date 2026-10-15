from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermes_loopguard import paths
from hermes_loopguard.backends.base import GateResult, Usage
from hermes_loopguard.cli import main
from hermes_loopguard.config import load_watcher
from hermes_loopguard.db import Store
from hermes_loopguard.questions import ChoiceAnswer
from hermes_loopguard.router import poll

WATCHER_YAML = {
    "name": "issues",
    "source": {"file": "issues.json"},
    "gate": {
        "questions": {
            "kind": {
                "type": "choice",
                "instructions": "Classify the issue",
                "criteria": {"bug": "a real defect", "spam": "noise or advertising"},
            }
        },
        "route_question": "kind",
        "routes": {
            "bug": {"handler": "agent", "min_prob": 0.6},
            "spam": {"handler": "drop", "min_prob": 0.6},
        },
        "default_route": "review",
    },
}


class _SpamBackend:
    # FAKE BACKEND (unit test only)
    name = "fake_spam"

    def available(self):
        return True

    def ask(self, state, questions):
        return GateResult(
            answers={"kind": ChoiceAnswer(choice="spam", confidence=0.9,
                                          probabilities={"bug": 0.1, "spam": 0.9})},
            backend=self.name,
            model="fake-v1",
            usage=Usage(input_tokens=5, output_tokens=2, cost_usd=0.0),
        )


def _write_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "issues.yaml"
    spec.write_text(yaml.safe_dump(WATCHER_YAML), encoding="utf-8")
    return spec


def test_watch_install_writes_yaml_and_shim(tmp_path: Path, capsys):
    spec = _write_spec(tmp_path)
    rc = main(["watch", "install", str(spec), "--no-cron"])
    out = capsys.readouterr().out

    assert rc == 0
    installed = paths.watchers_dir() / "issues.yaml"
    shim = paths.scripts_dir() / "loopguard_issues.py"
    assert installed.is_file()
    assert shim.is_file()
    assert 'main(["issues"])' in shim.read_text(encoding="utf-8")
    assert "hermes cron add" in out
    assert "lg-issues" in out
    assert yaml.safe_load(installed.read_text(encoding="utf-8"))["name"] == "issues"


def test_watch_install_rejects_a_bad_spec(tmp_path: Path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: broken\n", encoding="utf-8")
    assert main(["watch", "install", str(bad), "--no-cron"]) == 1
    assert "Invalid watcher spec" in capsys.readouterr().err


def test_poll_dry_run_writes_nothing(tmp_path: Path, capsys):
    assert main(["watch", "install", str(_write_spec(tmp_path)), "--no-cron"]) == 0
    (paths.config_dir() / "issues.json").write_text(
        json.dumps([{"id": "1", "title": "Bug in login"}]), encoding="utf-8"
    )
    capsys.readouterr()

    assert main(["poll", "issues", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "Wake agent:" in out

    store = Store.open_default()
    try:
        assert store.polls() == []
        assert store.decisions() == []
        assert store.get_item("issues", "1") is None
    finally:
        store.close()


def test_stats_json_and_text(capsys):
    store = Store.open_default()
    store.log_poll(
        watcher="issues", n_items=3, n_changed=0, n_admitted=0, woke=False, shadow=False,
        est_tokens_avoided=25000, est_usd_avoided=0.075, gate_tokens=10, gate_usd=0.0001,
    )
    store.close()

    assert main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_runs"] == 1
    assert payload["tokens_avoided"] == 25000
    assert "per_watcher" in payload
    assert "timeline" in payload

    assert main(["stats"]) == 0
    text = capsys.readouterr().out
    assert "LoopGuard: 1 runs skipped" in text
    assert "issues" in text


def test_decisions_lists_rows(capsys):
    store = Store.open_default()
    store.log_decision(watcher="issues", kind="route", item_id="1", route="agent", outcome="admitted")
    store.close()

    assert main(["decisions", "--kind", "route", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["route"] == "agent"

    assert main(["decisions"]) == 0
    assert "agent" in capsys.readouterr().out


def test_review_approve_forces_the_route_on_the_next_poll(tmp_path: Path, capsys):
    assert main(["watch", "install", str(_write_spec(tmp_path)), "--no-cron"]) == 0
    (paths.config_dir() / "issues.json").write_text(
        json.dumps([{"id": "1", "title": "Bug in login"}]), encoding="utf-8"
    )

    store = Store.open_default()
    store.upsert_item("issues", "1", fingerprint="fp-old", title="Bug in login")
    review_id = store.enqueue_review("issues", "1", title="Bug in login", reason="low prob", payload={})
    store.close()

    assert main(["review", "approve", str(review_id)]) == 0
    capsys.readouterr()

    store = Store.open_default()
    try:
        assert store.get_kv("force_route:issues:1") == "bug"
        assert store.get_item("issues", "1")["fingerprint"] == ""
        assert store.review_queue("open") == []

        result = poll(
            load_watcher("issues"), store=store, cfg={}, owner="test",
            chain_factory=lambda: [_SpamBackend()],
        )
        assert result.wake is True
        assert result.routed[0].route == "bug"
        assert result.routed[0].handler == "agent"
        assert store.get_kv("force_route:issues:1") is None
        notes = [d.get("note") for d in store.decisions(kind="route")]
        assert "forced by review" in notes
    finally:
        store.close()


def test_review_drop_marks_the_item_dropped(capsys):
    store = Store.open_default()
    store.upsert_item("issues", "5", fingerprint="fp-5", title="Noise")
    review_id = store.enqueue_review("issues", "5", title="Noise", reason="unclear", payload={})
    store.close()

    assert main(["review", "drop", str(review_id)]) == 0
    store = Store.open_default()
    try:
        assert store.get_item("issues", "5")["state"] == "dropped"
    finally:
        store.close()


def test_doctor_never_prints_secret_values(capsys, monkeypatch: pytest.MonkeyPatch):
    secret = "ts-secret-value-do-not-print-9182736"
    monkeypatch.setenv("TYPESAFE_API_KEY", secret)

    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert secret not in out
    assert "TYPESAFE_API_KEY" in out
    assert "set" in out


def test_shadow_toggle_writes_the_loopguard_config(capsys):
    assert main(["shadow", "on"]) == 0
    config_file = paths.config_dir() / "config.yaml"
    assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["shadow"] is True

    assert main(["shadow", "off"]) == 0
    assert yaml.safe_load(config_file.read_text(encoding="utf-8"))["shadow"] is False
    capsys.readouterr()


def test_reset_deletes_items(capsys):
    store = Store.open_default()
    store.upsert_item("issues", "1", fingerprint="fp-1", title="A")
    store.close()

    assert main(["reset", "issues"]) == 0
    store = Store.open_default()
    try:
        assert store.get_item("issues", "1") is None
    finally:
        store.close()
    capsys.readouterr()


def test_check_reports_an_unmet_goal(tmp_path: Path, capsys):
    goal = tmp_path / "GOAL.md"
    goal.write_text("# Goal: x\n\n## Checks\n- file: missing.txt exists\n", encoding="utf-8")

    assert main(["check", str(goal), "--cwd", str(tmp_path), "--no-semantic"]) == 1
    assert "not met" in capsys.readouterr().out


def test_check_reports_a_met_goal(tmp_path: Path, capsys):
    (tmp_path / "artifact.txt").write_text("done", encoding="utf-8")
    goal = tmp_path / "GOAL.md"
    goal.write_text("# Goal: x\n\n## Checks\n- file: artifact.txt exists\n", encoding="utf-8")

    assert main(["check", str(goal), "--cwd", str(tmp_path), "--no-semantic"]) == 0
    assert "met" in capsys.readouterr().out


def test_watch_remove_deletes_files(capsys):
    tmp_spec = Path(paths.config_dir().parent.parent / "spec.yaml")
    tmp_spec.write_text(yaml.safe_dump(WATCHER_YAML), encoding="utf-8")
    assert main(["watch", "install", str(tmp_spec), "--no-cron"]) == 0
    assert main(["watch", "remove", "issues"]) == 0
    out = capsys.readouterr().out
    assert "hermes cron remove lg-issues" in out
    assert not (paths.watchers_dir() / "issues.yaml").exists()
    assert not (paths.scripts_dir() / "loopguard_issues.py").exists()
