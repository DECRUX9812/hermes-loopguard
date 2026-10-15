from __future__ import annotations

from pathlib import Path
import pytest

from hermes_loopguard.db import Store


@pytest.fixture(autouse=True)
def setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "test_loopguard.db"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("LOOPGUARD_DB", str(db_file))
    yield


@pytest.fixture
def store(tmp_path: Path):
    db_file = tmp_path / "fixture_store.db"
    s = Store(db_file)
    yield s
    s.close()
