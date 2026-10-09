from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import rssbot.__main__ as entry
from rssbot.config import Config
from rssbot.models import Actor, ChannelKind, LogKind

NOW = 1_700_000_000


def guild(server_id: int, *, unavailable: bool = False) -> Any:
    return SimpleNamespace(id=server_id, name=f"server {server_id}", unavailable=unavailable)


@pytest.fixture
async def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[entry.Bot]:
    """A Bot that never connects. `bot.present` is what Discord says it is a member of."""
    present: list[Any] = []
    monkeypatch.setattr(entry.Bot, "guilds", property(lambda self: list(present)))
    bot = entry.Bot(Config("not-a-real-token", tmp_path, False, "INFO"))
    bot.present = present  # type: ignore[attr-defined]
    bot.clock = SimpleNamespace(now=lambda: NOW)  # type: ignore[assignment]
    yield bot
    await bot.fetcher.close()
    bot.db.close()


def add_feed(bot: entry.Bot, server_id: int) -> int:
    return bot.db.create_feed(
        server_id=server_id,
        channel_id=server_id * 10,
        channel_kind=ChannelKind.MESSAGES,
        name="News",
        url=f"https://example.com/{server_id}.xml",
        now=NOW,
    ).id


def due(bot: entry.Bot) -> list[int]:
    return [feed.id for feed in bot.db.due_feeds(NOW)]


async def test_a_server_the_bot_was_removed_from_while_offline_is_marked_removed(bot):
    stayed, left, outage = add_feed(bot, 1), add_feed(bot, 2), add_feed(bot, 4)
    bot.db.ensure_server(3)
    bot.db.mark_server_removed(3, 50)
    bot.present[:] = [guild(1), guild(4, unavailable=True)]

    await bot.on_ready()

    assert bot.db.get_server(1).removed_at is None
    assert bot.db.get_server(2).removed_at == NOW  # its 30 days start now
    assert bot.db.get_server(3).removed_at == 50  # an earlier removal keeps its date
    assert bot.db.get_server(4).removed_at is None  # an outage is not a removal
    assert due(bot) == [stayed, outage]  # the removed Server's Feed is no longer checked
    assert left not in due(bot)


@pytest.mark.parametrize("how", ["invited back while running", "there again at the next start"])
async def test_a_server_marked_removed_at_start_up_is_served_again_when_it_is_back(bot, how):
    feed = add_feed(bot, 2)
    await bot.on_ready()
    assert due(bot) == []

    if how == "invited back while running":
        await bot.on_guild_join(guild(2))
    else:
        bot.present[:] = [guild(2)]
        await bot.on_ready()

    assert bot.db.get_server(2).removed_at is None
    assert due(bot) == [feed]


async def test_start_up_goes_on_when_the_removed_servers_cannot_be_worked_out(bot, monkeypatch):
    add_feed(bot, 2)
    bot.present[:] = [guild(1)]

    def broken() -> list[int]:
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(bot.db, "present_server_ids", broken)

    await bot.on_ready()  # does not raise

    assert bot.db.get_server(1).removed_at is None


@pytest.fixture
def quiet_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() sets up logging for the whole process; keep that out of the other tests."""
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: None)
    discord_log = logging.getLogger("discord")
    monkeypatch.setattr(discord_log, "level", discord_log.level)


def test_a_data_dir_the_bot_cannot_write_to_is_a_configuration_problem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    quiet_logging: None,
) -> None:
    # Whatever user the tests run as, nobody can make a folder inside a file.
    (tmp_path / "blocked").write_text("")
    data_dir = tmp_path / "blocked" / "data"
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    code = entry.main()

    assert code == 2  # as for a missing token
    captured = capsys.readouterr()
    assert captured.err.startswith("Configuration problem: ")
    assert str(data_dir) in captured.err
    assert "write" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert "not-a-real-token" not in captured.err + captured.out


def test_a_database_file_that_cannot_be_opened_is_a_configuration_problem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    quiet_logging: None,
) -> None:
    (tmp_path / "rssbot.db").write_bytes(b"this is not a database, " * 100)
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert entry.main() == 2

    captured = capsys.readouterr()
    assert captured.err.startswith("Configuration problem: ")
    assert str(tmp_path / "rssbot.db") in captured.err


async def test_the_journal_is_shared_with_the_commands(bot):
    assert bot.deps.journal is bot.journal
    assert bot.service._journal is bot.journal and bot.scheduler._journal is bot.journal
    entry = bot.journal.record(1, Actor.bot(), LogKind.FEED_BROKEN)
    assert bot.db.list_log_entries(1, limit=5) == [entry]
    await bot.journal.announce(entry, Actor.bot())  # no Logs channel: nothing to post, no error
