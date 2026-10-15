"""YAML compatibility: pyyaml preferred, ruamel.yaml fallback (PM Hermes env)."""
from __future__ import annotations

import io
import sys

import pytest

from hermes_loopguard import _yaml

DOC = {"name": "demo", "count": 2, "nested": {"flag": True, "items": ["a", "b"]}}

try:
    import ruamel.yaml  # noqa: F401

    HAVE_RUAMEL = True
except Exception:
    HAVE_RUAMEL = False


def test_safe_load_stream_pyyaml_path():
    assert _yaml.safe_load(io.StringIO("a: 1\nb: [x, y]\n")) == {"a": 1, "b": ["x", "y"]}


def test_safe_load_text_pyyaml_path():
    assert _yaml.safe_load("key: value\n") == {"key": "value"}


def test_safe_load_empty_returns_none():
    assert _yaml.safe_load("") is None


def test_safe_dump_roundtrip():
    text = _yaml.safe_dump(DOC)
    assert _yaml.safe_load(text) == DOC


def test_safe_dump_to_stream():
    buf = io.StringIO()
    assert _yaml.safe_dump(DOC, buf) is None
    assert _yaml.safe_load(buf.getvalue()) == DOC


def test_safe_dump_rejects_non_plain_types():
    with pytest.raises(Exception):
        _yaml.safe_dump({"x": object()})


@pytest.mark.skipif(not HAVE_RUAMEL, reason="ruamel.yaml not installed")
def test_safe_load_falls_back_to_ruamel(monkeypatch: pytest.MonkeyPatch):
    # Simulate the Hermes PM environment: no pyyaml on sys.path.
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert _yaml.safe_load("key: [1, 2]\nnested: {a: b}\n") == {"key": [1, 2], "nested": {"a": "b"}}


@pytest.mark.skipif(not HAVE_RUAMEL, reason="ruamel.yaml not installed")
def test_safe_load_falls_back_to_ruamel_stream(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert _yaml.safe_load(io.StringIO("a: 1\n")) == {"a": 1}


@pytest.mark.skipif(not HAVE_RUAMEL, reason="ruamel.yaml not installed")
def test_safe_dump_falls_back_to_ruamel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    text = _yaml.safe_dump(DOC)
    assert isinstance(text, str)
    assert _yaml.safe_load(text) == DOC


@pytest.mark.skipif(not HAVE_RUAMEL, reason="ruamel.yaml not installed")
def test_safe_dump_falls_back_to_ruamel_stream(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    buf = io.StringIO()
    assert _yaml.safe_dump(DOC, buf) is None
    assert _yaml.safe_load(buf.getvalue()) == DOC


def test_no_yaml_lib_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel.yaml", None)
    monkeypatch.setitem(sys.modules, "ruamel", None)
    with pytest.raises(ImportError):
        _yaml.safe_load("a: 1\n")
