"""Instance and global configuration (TOML)."""

import os
import stat
import sys
import tomllib
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
INSTANCES_DIR = REPO_DIR / "instances"
GLOBAL_CONFIG = REPO_DIR / "global.toml"
STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "gsm"


class ConfigError(Exception):
    pass


def _check_perms(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & stat.S_IWOTH:
        raise ConfigError(f"{path} is world-writable. Fix with: chmod o-w {path}")


def _load_toml(path: Path) -> dict:
    _check_perms(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_global() -> dict:
    if not GLOBAL_CONFIG.exists():
        return {}
    return _load_toml(GLOBAL_CONFIG)


def list_instances() -> list[str]:
    if not INSTANCES_DIR.is_dir():
        return []
    return sorted(p.stem for p in INSTANCES_DIR.glob("*.toml"))


def load_instance(name: str) -> dict:
    path = INSTANCES_DIR / f"{name}.toml"
    if not path.exists():
        known = ", ".join(list_instances()) or "none"
        raise ConfigError(f"No instance config {path} (known instances: {known})")
    cfg = _load_toml(path)
    if "adapter" not in cfg:
        raise ConfigError(f"{path}: missing required key 'adapter'")
    cfg.setdefault("update_check_time", "03:00")
    cfg.setdefault("auto_update", True)
    cfg.setdefault("max_backups", 5)
    cfg.setdefault("check_interval", 30)
    cfg["name"] = name
    return cfg


def state_dir(instance: str | None = None) -> Path:
    d = STATE_ROOT / instance if instance else STATE_ROOT
    d.mkdir(parents=True, exist_ok=True)
    return d


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)
