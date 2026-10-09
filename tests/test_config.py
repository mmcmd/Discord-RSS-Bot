from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from rssbot.config import Config, ConfigError, load_config


def test_defaults():
    config = load_config({"DISCORD_TOKEN": "abc"})

    assert config == Config(
        token="abc",
        data_dir=Path("/data"),
        allow_private_urls=False,
        log_level="INFO",
    )


def test_db_path_is_inside_data_dir():
    config = load_config({"DISCORD_TOKEN": "abc", "DATA_DIR": "/srv/bot"})

    assert config.db_path == Path("/srv/bot/rssbot.db")


@pytest.mark.parametrize("env", [{}, {"DISCORD_TOKEN": ""}, {"DISCORD_TOKEN": "   "}])
def test_missing_or_blank_token_is_an_error(env):
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config(env)


def test_token_is_stripped():
    assert load_config({"DISCORD_TOKEN": "  abc \n"}).token == "abc"


def test_blank_data_dir_falls_back_to_default():
    assert load_config({"DISCORD_TOKEN": "abc", "DATA_DIR": ""}).data_dir == Path("/data")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", "On", " true "])
def test_allow_private_urls_true_values(value):
    config = load_config({"DISCORD_TOKEN": "abc", "ALLOW_PRIVATE_URLS": value})

    assert config.allow_private_urls is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "maybe"])
def test_allow_private_urls_other_values_are_false(value):
    config = load_config({"DISCORD_TOKEN": "abc", "ALLOW_PRIVATE_URLS": value})

    assert config.allow_private_urls is False


@pytest.mark.parametrize("value", ["debug", "Info", "WARNING", "error", "critical"])
def test_log_level_is_stored_upper_case(value):
    config = load_config({"DISCORD_TOKEN": "abc", "LOG_LEVEL": value})

    assert config.log_level == value.upper()


@pytest.mark.parametrize("value", ["loud", "10", "WARN", "NOTSET"])
def test_unknown_log_level_is_an_error(value):
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_config({"DISCORD_TOKEN": "abc", "LOG_LEVEL": value})


def test_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "from-environ")
    monkeypatch.setenv("DATA_DIR", "/tmp/rssbot")

    config = load_config()

    assert config.token == "from-environ"
    assert config.data_dir == Path("/tmp/rssbot")


def test_config_is_frozen():
    config = load_config({"DISCORD_TOKEN": "abc"})

    with pytest.raises(FrozenInstanceError):
        config.token = "other"  # type: ignore[misc]


def test_the_token_is_not_shown_when_the_config_is_printed():
    config = load_config({"DISCORD_TOKEN": "s3cret-token", "DATA_DIR": "/srv/bot"})

    assert "s3cret-token" not in repr(config)
    assert "s3cret-token" not in str(config)
    assert "s3cret-token" not in f"{config}"
    assert "/srv/bot" in repr(config)  # the rest is still there to read
