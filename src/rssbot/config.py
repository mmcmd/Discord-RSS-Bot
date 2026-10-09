"""Deployment settings, read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = "/data"
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class ConfigError(Exception):
    """The Instance is misconfigured. The message tells the Deployer what to fix."""


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)  # a Config that is logged must not give it away
    data_dir: Path
    allow_private_urls: bool
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rssbot.db"


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        env = os.environ

    token = env.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "DISCORD_TOKEN is not set. Put your bot token from the Discord developer portal "
            "in the DISCORD_TOKEN environment variable (for example in your .env file)."
        )

    data_dir = Path(env.get("DATA_DIR") or DEFAULT_DATA_DIR)

    allow_private_urls = env.get("ALLOW_PRIVATE_URLS", "").strip().lower() in TRUE_VALUES

    log_level = (env.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip().upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL is {env.get('LOG_LEVEL')!r}, which is not a log level. "
            "Use one of DEBUG, INFO, WARNING, ERROR or CRITICAL."
        )

    return Config(
        token=token,
        data_dir=data_dir,
        allow_private_urls=allow_private_urls,
        log_level=log_level,
    )
