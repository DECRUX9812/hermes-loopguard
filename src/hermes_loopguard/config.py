from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from hermes_loopguard import _yaml, paths

DEFAULTS = {
    "backend": "auto",  # auto | typesafe | openai | hermes | local
    "shadow": False,  # global shadow mode
    "typesafe": {
        "model": None,
        "api_key_env": "TYPESAFE_API_KEY",
        "timeout": 30,
        "usd_per_mtok_in": 0.0,
        "usd_per_mtok_out": 0.0,
    },
    "openai": {
        "base_url": None,
        "model": None,
        "api_key_env": None,
        "timeout": 30,
        "mode": "json",  # json | logprobs
        "usd_per_mtok_in": 0.0,
        "usd_per_mtok_out": 0.0,
    },
    "hermes": {"task": "loopguard_gate", "max_tokens": 400},
    "local": {"keywords": {}},  # {category: [words]} for Choice; {"<noul name>": {"yes": [...], "no": [...]}}
    "pricing": {"est_tokens_per_run": 25000, "usd_per_mtok": 3.0},
    "tool_gate": {
        "enabled": False,
        "scope": "automations",
        "allowed_paths": [],
        "network": "approve",
        "destructive": "block",
        "unclear": "approve",
        "semantic": False,
    },
    "completion": {"goal_file": "GOAL.md", "max_continuations": 3},
    "kanban": {"max_attempts": 3, "spend_cap_tokens": 400000},
}


def deep_merge(base: dict, over: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in over.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result


def load_config(path: Path | None = None) -> dict:
    if path is None:
        cfg_file = paths.config_dir() / "config.yaml"
        if not cfg_file.exists():
            cfg_file = paths.config_dir() / "config.yml"
    else:
        cfg_file = Path(path)

    if cfg_file.exists():
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        return deep_merge(DEFAULTS, data)
    return copy.deepcopy(DEFAULTS)


@dataclass
class WatcherSpec:
    name: str
    source: dict  # {"file": path} | {"command": [argv...]} | {"python": "module:function"}; optional "items_path": "a.b"
    id_field: str = "id"
    title_field: str = "title"
    fingerprint_fields: Sequence[str] = ()  # empty -> whole item
    gate: dict | None = None  # GateSpec dict (see semantic_gate); None -> every changed item routes to "agent"
    state_fields: Sequence[str] = ()  # fields sent to the semantic gate as state; empty -> fingerprint fields
    brief: str = ""  # header of the agent brief
    max_items_per_run: int = 5
    max_brief_chars: int = 6000
    max_attempts: int = 3
    spend_cap_tokens: int = 300000  # per item
    claim_ttl_s: int = 7200
    shadow: bool | None = None  # None -> global
    kanban: dict = field(default_factory=dict)  # {"assignee": str, "board": str|None, "tenant": str|None}
    config: dict | None = None  # per-watcher overrides merged over the global config


def watcher_from_dict(d: dict) -> WatcherSpec:
    kwargs = dict(d)
    name = kwargs.pop("name")
    source = kwargs.pop("source")
    cfg_overrides = kwargs.pop("config", None)
    return WatcherSpec(name=name, source=source, config=cfg_overrides, **kwargs)


def load_watcher(name_or_path: str) -> WatcherSpec:
    cand = Path(name_or_path)
    if cand.is_file():
        path = cand
    elif (paths.watchers_dir() / f"{name_or_path}.yaml").is_file():
        path = paths.watchers_dir() / f"{name_or_path}.yaml"
    elif (paths.watchers_dir() / f"{name_or_path}.yml").is_file():
        path = paths.watchers_dir() / f"{name_or_path}.yml"
    else:
        path = cand
        if not path.is_file():
            raise FileNotFoundError(f"Watcher spec not found: {name_or_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.safe_load(f) or {}
    if "name" not in data:
        data["name"] = path.stem
    return watcher_from_dict(data)


def list_watchers() -> list[str]:
    wdir = paths.watchers_dir()
    if not wdir.is_dir():
        return []
    names = set()
    for p in wdir.glob("*.yaml"):
        names.add(p.stem)
    for p in wdir.glob("*.yml"):
        names.add(p.stem)
    return sorted(names)
