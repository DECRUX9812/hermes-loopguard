from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from hermes_loopguard import _yaml, paths

PLUGIN_ID = "loopguard"

DEFAULTS = {
    "backend": "auto",  # auto | typesafe | openai | hermes | local
    "shadow": False,  # global shadow mode
    "on_error": "wake",  # watcher error policy: wake (fail open) | skip
    "route_min_prob": 0.6,  # default Choice threshold for routes without min_prob
    "typesafe": {
        # Pinned on purpose: aliases (jev-preview) move between releases and
        # would change behavior silently.
        "model": "jev-1.14.0",
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
    "computer_use": {
        "enabled": False,
        "shadow": False,
        "driver": {
            "name": "cua",
            "socket": "/tmp/lg-cu/cua.sock",
            "app": None,
            "cua_bin": "cua-driver",
            "timeout_s": 20,
        },
        "planner_model": "gemini-3.1-flash-lite",
        "models": ["jev-1.14.0", "jev-1.13.0", "openai:gemini-3.8-flash-high"],
        "openai": {"base_url": "http://127.0.0.1:8317/v1", "api_key_env": "CLIPROXY_API_KEY"},
        "timeout_s": 30,
        "thresholds": {"presence": 0.6, "box_confidence": 0.5, "verify": 0.7, "safety": 0.2},
        "hash": {"unchanged_max": 3, "changed_min": 12},
        "stall": {"stale_limit": 4},
        "escalation": {"model": "openai:claude-opus-4-6-thinking", "after_failures": 2},
        "max_steps": 12,
        "budget_usd": 0.50,
        "frames": {"retention": 20},
    },
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


# Settings > Plugins keys (the plugin.yaml config_schema) and the LoopGuard
# config path each layers onto. The secret key is deliberately absent: secret
# values never land in config.yaml (Hermes routes them to .env), and the
# plugin reads them only through paths.get_secret (agent.secret_scope).
SETTING_KEYS: dict[str, str] = {
    "shadow": "shadow",
    "backend": "backend",
    "route_min_prob": "route_min_prob",
    "kanban.spend_cap_tokens": "kanban.spend_cap_tokens",
    "tool_gate.enabled": "tool_gate.enabled",
    "tool_gate.semantic": "tool_gate.semantic",
    "completion.max_continuations": "completion.max_continuations",
    "computer_use.enabled": "computer_use.enabled",
    "computer_use.shadow": "computer_use.shadow",
    "typesafe.model": "typesafe.model",
    "computer_use.models": "computer_use.models",
}
SECRET_SETTING_KEYS = frozenset({"typesafe_api_key"})


def hermes_plugin_settings() -> dict:
    """Values saved in Settings > Plugins (``plugins.entries.<id>.settings``).

    Read through Hermes's own config loader (profile-aware). Empty outside a
    Hermes process (the cron shim still works from the LoopGuard config file
    alone); a broken Hermes config can never fail a gate run.
    """
    try:
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly() or {}
        plugins = data.get("plugins")
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get(PLUGIN_ID) if isinstance(entries, dict) else None
        settings = entry.get("settings") if isinstance(entry, dict) else None
        return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _set_dotted(cfg: dict, dotted: str, value: Any) -> dict:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = copy.deepcopy(value)
    return cfg


def apply_plugin_settings(cfg: dict, settings: dict | None) -> dict:
    """Layer values saved in Settings > Plugins over the LoopGuard config file.

    Only keys the user actually saved are applied — the form shows schema
    defaults but writes nothing until saved, so existing loopguard
    config.yaml values keep working. Unknown keys and secret keys are
    ignored (secrets live in .env via Hermes, never in config).
    """
    cfg = copy.deepcopy(cfg)
    for key, value in (settings or {}).items():
        if key in SECRET_SETTING_KEYS:
            continue
        path = SETTING_KEYS.get(str(key))
        if path is None:
            continue
        cfg = _set_dotted(cfg, path, value)
    return cfg


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
