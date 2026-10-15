from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
from typing import Any

from hermes_loopguard import paths
from hermes_loopguard.fingerprint import dotted_get


class SourceError(RuntimeError):
    pass


def load_items(source: dict, *, base_dir: Path | None = None, timeout: float = 60) -> list[dict]:
    if not isinstance(source, dict):
        raise SourceError(f"Source spec must be a dict, got {type(source).__name__}")

    if base_dir is None:
        base_dir = paths.config_dir()

    items_path = source.get("items_path")

    if "file" in source:
        file_val = source["file"]
        p = Path(file_val).expanduser()
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        if not p.is_file():
            raise SourceError(f"Source file not found: {p}")

        with open(p, "r", encoding="utf-8") as f:
            text = f.read().strip()

        if not text:
            raw_items = []
        else:
            try:
                data = json.loads(text)
                if items_path:
                    raw_items = dotted_get(data, items_path)
                elif isinstance(data, list):
                    raw_items = data
                elif isinstance(data, dict):
                    raise SourceError(f"JSON root in file {p} is a dict but no items_path was specified")
                else:
                    raise SourceError(f"Unexpected JSON root type in file {p}: {type(data).__name__}")
            except json.JSONDecodeError:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                try:
                    raw_items = [json.loads(line) for line in lines]
                except Exception as e:
                    raise SourceError(f"Failed to parse file {p} as JSON or JSONL: {e}") from e

    elif "command" in source:
        argv = source["command"]
        if isinstance(argv, str):
            raise SourceError("Command must be a list of arguments, not a string (shell=True is forbidden)")
        if not isinstance(argv, (list, tuple)):
            raise SourceError(f"Command must be a list or tuple of arguments, got {type(argv).__name__}")

        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            raise SourceError(f"Failed to run command {argv}: {e}") from e

        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "")[-500:]
            raise SourceError(f"Command exited with code {proc.returncode}: {stderr_tail}")

        stdout = proc.stdout.strip()
        if not stdout:
            raw_items = []
        else:
            try:
                data = json.loads(stdout)
                if items_path:
                    raw_items = dotted_get(data, items_path)
                elif isinstance(data, list):
                    raw_items = data
                elif isinstance(data, dict):
                    raise SourceError("Command output is a JSON dict but no items_path was specified")
                else:
                    raise SourceError(f"Unexpected JSON root type in command output: {type(data).__name__}")
            except json.JSONDecodeError:
                lines = [line.strip() for line in stdout.splitlines() if line.strip()]
                try:
                    raw_items = [json.loads(line) for line in lines]
                except Exception as e:
                    raise SourceError(f"Failed to parse command output as JSON or JSONL: {e}") from e

    elif "python" in source:
        py_target = source["python"]
        if ":" not in py_target:
            raise SourceError(f"Invalid python target '{py_target}', expected 'module:function'")
        mod_name, func_name = py_target.split(":", 1)
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, func_name)
            raw_items = fn()
        except Exception as e:
            raise SourceError(f"Failed to run python source '{py_target}': {e}") from e

    else:
        raise SourceError(f"Unknown source type in {source}")

    if not isinstance(raw_items, list):
        raise SourceError(f"Expected list of items, got {type(raw_items).__name__}")

    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise SourceError(f"Item at index {idx} is not a dict: {type(item).__name__}")

    return raw_items
