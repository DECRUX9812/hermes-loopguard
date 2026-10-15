from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    try:
        import hermes_constants
        if hasattr(hermes_constants, "get_hermes_home"):
            return Path(hermes_constants.get_hermes_home()).expanduser().resolve()
    except Exception:
        pass

    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


def data_dir() -> Path:
    p = hermes_home() / "plugin-data" / "loopguard"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    env_db = os.environ.get("LOOPGUARD_DB")
    if env_db:
        p = Path(env_db).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return data_dir() / "loopguard.db"


def config_dir() -> Path:
    return hermes_home() / "loopguard"


def watchers_dir() -> Path:
    return config_dir() / "watchers"


def scripts_dir() -> Path:
    return hermes_home() / "scripts"


def bootstrap_hermes_env() -> None:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv(hermes_home=hermes_home())
    except Exception:
        pass


def get_secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret as hermes_get_secret
        val = hermes_get_secret(name)
        if val is not None:
            return val
    except Exception:
        pass
    return os.environ.get(name)
