"""Settings > Plugins (config_schema) and the settings layering.

Unit tests: the plugin.yaml schema is complete and every declared key is
actually read, the layering only applies keys the user saved, secret keys
never land in config, and the Hermes config.yaml route is read through
Hermes's own loader.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml

from hermes_loopguard import config as lg_config
from hermes_loopguard import paths, plugin
from hermes_loopguard.semantic_gate import gate_spec_from_dict

REPO_ROOT = Path(__file__).resolve().parents[1]

# FAKE SCHEMA (the real one, read from the repo-root plugin.yaml the loader sees)
@pytest.fixture(scope="module")
def schema() -> dict:
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert isinstance(manifest, dict) and manifest.get("config_schema")
    return manifest["config_schema"]


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    plugin._CFG_CACHE["cfg"] = None
    plugin._CFG_CACHE["key"] = None
    yield
    plugin._CFG_CACHE["cfg"] = None
    plugin._CFG_CACHE["key"] = None


def _dotted(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def test_manifest_is_a_v2_manifest():
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "loopguard"
    assert manifest["manifest_version"] == 2
    assert manifest["version"]
    assert manifest["description"]
    assert manifest["provides_tools"] == ["jev_computer", "loopguard_cu_status"]
    for hook in manifest["provides_hooks"]:
        assert isinstance(hook, str) and hook


def test_schema_every_key_has_label_description_default(schema):
    for key, spec in schema.items():
        assert isinstance(spec, dict), key
        assert spec.get("label"), f"{key}: missing label"
        assert spec.get("description"), f"{key}: missing description"
        assert "default" in spec, f"{key}: missing default"


def test_schema_every_declared_key_is_read(schema):
    """Every non-secret settings key maps onto the LoopGuard config (so the
    layering puts it where the engine reads it)."""
    for key, spec in schema.items():
        if spec.get("type") == "secret":
            assert key not in lg_config.SETTING_KEYS
            assert spec.get("env") == "TYPESAFE_API_KEY"
            continue
        assert key in lg_config.SETTING_KEYS, f"{key} is declared but never read"
        assert _dotted(lg_config.DEFAULTS, lg_config.SETTING_KEYS[key]) is not None


def test_schema_types_match_defaults(schema):
    type_check = {
        "bool": lambda v: isinstance(v, bool),
        "str": lambda v: isinstance(v, str),
        "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "list": lambda v: isinstance(v, list),
    }
    for key, spec in schema.items():
        typ = str(spec.get("type", "str"))
        if typ == "secret":
            continue
        assert typ in type_check, f"{key}: untested type {typ}"
        assert type_check[typ](_dotted(lg_config.DEFAULTS, key)), (
            f"{key}: schema type {typ} does not match the default "
            f"{_dotted(lg_config.DEFAULTS, key)!r}"
        )


def test_schema_choices_match_defaults(schema):
    backend = schema["backend"]
    assert lg_config.DEFAULTS["backend"] in backend["choices"]
    models = schema["computer_use.models"]["default"]
    assert all(m.split(":")[-1].count(".") >= 2 for m in models[:2])  # pinned ids, not aliases


def test_layering_applies_saved_keys_only(schema):
    settings = {
        "shadow": True,
        "backend": "local",
        "tool_gate.enabled": True,
        "completion.max_continuations": 7,
        "computer_use.models": ["jev-1.13.0"],
        "route_min_prob": 0.9,
        "kanban.spend_cap_tokens": 5,
        "not_a_loopguard_setting": "ignore-me",
    }
    cfg = lg_config.apply_plugin_settings(lg_config.DEFAULTS, settings)

    assert cfg["shadow"] is True
    assert cfg["backend"] == "local"
    assert cfg["tool_gate"]["enabled"] is True
    assert cfg["tool_gate"]["semantic"] is False  # not saved -> file/default value
    assert cfg["completion"]["max_continuations"] == 7
    assert cfg["computer_use"]["models"] == ["jev-1.13.0"]
    assert cfg["route_min_prob"] == 0.9
    assert cfg["kanban"]["spend_cap_tokens"] == 5
    assert "not_a_loopguard_setting" not in cfg


def test_layering_never_mutates_defaults():
    before = yaml.safe_dump(lg_config.DEFAULTS)
    lg_config.apply_plugin_settings(
        lg_config.DEFAULTS, {"tool_gate.enabled": True, "computer_use.shadow": True}
    )
    assert yaml.safe_dump(lg_config.DEFAULTS) == before


def test_secret_keys_never_land_in_config():
    # Even a hand-edited settings entry (Hermes itself refuses to write one)
    # must not leak into the config dict.
    cfg = lg_config.apply_plugin_settings(
        lg_config.DEFAULTS, {"typesafe_api_key": "sk-hand-written"}
    )
    assert "typesafe_api_key" not in cfg
    assert "typesafe_api_key" not in cfg.get("typesafe", {})
    dumped = "typesafe_api_key"
    assert dumped not in __import__("json").dumps(cfg)


def test_layering_over_the_loopguard_config_file(tmp_path, monkeypatch):
    config_dir = paths.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"backend": "openai", "tool_gate": {"enabled": True}}),
        encoding="utf-8",
    )
    # Saved settings win over the file; unsaved keys keep the file's value.
    monkeypatch.setattr(
        lg_config, "hermes_plugin_settings", lambda: {"backend": "local", "route_min_prob": 0.8}
    )
    cfg = plugin.get_cfg()
    assert cfg["backend"] == "local"  # saved in Settings > Plugins
    assert cfg["route_min_prob"] == 0.8
    assert cfg["tool_gate"]["enabled"] is True  # untouched file value


def test_get_cfg_reads_hermes_config_yaml(tmp_path, monkeypatch):
    """The real route: plugins.entries.loopguard.settings in HERMES_HOME's own
    config.yaml, read through Hermes's loader (faked here to stay unit-level)."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "entries": {
                        "loopguard": {
                            "settings": {"shadow": True, "typesafe.model": "jev-1.13.0"}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_load_config_readonly():
        return yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))

    fake_pkg = types.ModuleType("hermes_cli")
    fake_cfg_mod = types.ModuleType("hermes_cli.config")
    fake_cfg_mod.load_config_readonly = fake_load_config_readonly  # type: ignore[attr-defined]
    fake_pkg.config = fake_cfg_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_cfg_mod)

    cfg = plugin.get_cfg()
    assert cfg["shadow"] is True
    assert cfg["typesafe"]["model"] == "jev-1.13.0"


def test_hermes_plugin_settings_is_empty_outside_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # force the ImportError path
    assert lg_config.hermes_plugin_settings() == {}


def test_route_min_prob_is_the_route_threshold_default():
    gate = {
        "route_question": "kind",
        "questions": {
            "kind": {
                "type": "choice",
                "instructions": "What does this item need?",
                "criteria": {"bug": "a defect", "spam": "spam"},
            }
        },
        "routes": {
            "bug": {"handler": "agent"},  # no min_prob -> the setting applies
            "spam": {"handler": "drop", "min_prob": 0.95},  # explicit -> kept
        },
        "default_route": "review",
    }
    spec = gate_spec_from_dict(gate, default_min_prob=0.85)
    assert spec.routes["bug"].min_prob == 0.85
    assert spec.routes["spam"].min_prob == 0.95
    assert gate_spec_from_dict(gate).routes["bug"].min_prob == 0.6
