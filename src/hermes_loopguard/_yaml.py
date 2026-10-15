"""YAML compatibility layer.

LoopGuard runs in two interpreters: the user's venv (pyyaml) and stock
Hermes's PM-managed environment, which re-execs scripts and rebuilds
``sys.path`` so the outer venv's site-packages are gone. The PM env ships
``ruamel.yaml`` instead of pyyaml, so every YAML read/write goes through
this module: prefer pyyaml, fall back to ``ruamel.yaml`` (safe typ).
Both libraries are optional; the choice is made lazily at call time so
tests can force either path.
"""
from __future__ import annotations

import io
from typing import Any, TextIO

__all__ = ["safe_load", "safe_dump"]


def _pyyaml():
    try:
        import yaml  # noqa: PLC0415
    except Exception:
        return None
    return yaml


def _ruamel():
    try:
        from ruamel.yaml import YAML  # noqa: PLC0415
    except Exception:
        return None
    return YAML(typ="safe")


def safe_load(text_or_stream: Any) -> Any:
    """yaml.safe_load for a str/bytes or an open file object."""
    yaml = _pyyaml()
    if yaml is not None:
        return yaml.safe_load(text_or_stream)

    ruamel = _ruamel()
    if ruamel is not None:
        if isinstance(text_or_stream, bytes):
            text_or_stream = text_or_stream.decode("utf-8")
        if isinstance(text_or_stream, str):
            text_or_stream = io.StringIO(text_or_stream)
        return ruamel.load(text_or_stream)

    raise ImportError("No YAML library available (need pyyaml or ruamel.yaml)")


def safe_dump(obj: Any, stream: TextIO | None = None) -> str | None:
    """yaml.safe_dump; returns the document when ``stream`` is None."""
    yaml = _pyyaml()
    if yaml is not None:
        if stream is None:
            return yaml.safe_dump(obj)
        yaml.safe_dump(obj, stream)
        return None

    ruamel = _ruamel()
    if ruamel is not None:
        if stream is None:
            buf = io.StringIO()
            ruamel.dump(obj, buf)
            return buf.getvalue()
        ruamel.dump(obj, stream)
        return None

    raise ImportError("No YAML library available (need pyyaml or ruamel.yaml)")
