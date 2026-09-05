"""Settings. Owner: hub agent. YAML file + DIBS_* env overrides (nested via __)."""

from __future__ import annotations

import os
from typing import Any, Literal

import yaml
from pydantic import BaseModel


class PresenceConfig(BaseModel):
    enabled: bool = True
    idle_after_s: float = 30
    resume_after_s: float = 20
    consent_timeout_s: float = 60
    consent_grant_s: float = 300
    deny_cooldown_s: float = 120


class OverlayConfig(BaseModel):
    enabled: bool = True
    halo_color: str = "#00e5ff"
    banner: bool = True


class HotkeysConfig(BaseModel):
    pause: str = "ctrl+alt+shift+p"
    allow: str = "ctrl+alt+shift+y"
    deny: str = "ctrl+alt+shift+n"
    release: str = "ctrl+alt+shift+r"


class TraySettings(BaseModel):
    enabled: bool = True


class MotionConfig(BaseModel):
    human_like: bool = True
    speed: float = 1.0


class Settings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7474
    data_dir: str = "./data"
    screen_index: int | None = None
    max_long_edge: int = 1568
    max_pixels: int = 1_150_000
    lease_default_ttl_s: int = 60
    lease_max_ttl_s: int = 600
    auto_lease_wait_s: int = 30
    allow_local_open_registration: bool = True
    dashboard_open_on_loopback: bool = True
    allow_launch: bool = False
    keep_screenshots: int = 200
    mode: Literal["ask", "hands_off", "locked"] = "ask"
    presence: PresenceConfig = PresenceConfig()
    overlay: OverlayConfig = OverlayConfig()
    tray: TraySettings = TraySettings()
    motion: MotionConfig = MotionConfig()
    hotkeys: HotkeysConfig = HotkeysConfig()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` into `base`, recursing into nested dicts. Returns `base`."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _env_overrides(prefix: str = "DIBS_") -> dict[str, Any]:
    """Build a nested dict from DIBS_<KEY> env vars; `__` separates nesting levels."""
    overrides: dict[str, Any] = {}
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        remainder = env_key[len(prefix) :]
        if not remainder:
            continue
        parts = [part.lower() for part in remainder.split("__")]
        node = overrides
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = raw_value
    return overrides


def load_settings(path: str | None = None) -> Settings:
    """Load Settings from a YAML file (if any) then apply DIBS_* env overrides.

    File resolution order: `path` argument, else `DIBS_CONFIG` env var, else
    `./config.yaml` if it exists in the current working directory. It's fine for no
    file to be found — defaults + env overrides still apply.
    """
    config_path = path or os.environ.get("DIBS_CONFIG")
    if not config_path and os.path.isfile("config.yaml"):
        config_path = "config.yaml"

    data: dict[str, Any] = {}
    if config_path and os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if loaded:
            if not isinstance(loaded, dict):
                raise ValueError(f"config file {config_path!r} must contain a YAML mapping")
            data = loaded

    _deep_merge(data, _env_overrides())
    return Settings(**data)
