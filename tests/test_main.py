from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import rssbot.__main__ as entry
from rssbot.config import Config
from rssbot.models import Actor, ChannelKind, LogKind, PostAs
from rssbot.service import NO_SUCH_FEED, ServiceError

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


async def refresh_identities_once(
    bot: entry.Bot, monkeypatch: pytest.MonkeyPatch, raises: Exception
) -> None:
    """Run one round of the Post as refresh for a Feed whose refresh raises `raises`."""
    feed = add_feed(bot, 1)
    bot.db.update_feed(feed, post_as=PostAs.SITE)
    bot.present[:] = [guild(1)]

    async def ready() -> None:
        pass

    async def refresh(server_id: int, feed_id: int) -> None:
        raise raises

    async def stop(seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(bot, "wait_until_ready", ready)
    monkeypatch.setattr(bot.service, "refresh_site_identity", refresh)
    monkeypatch.setattr(entry.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await bot._refresh_identities()


async def test_a_feed_removed_during_the_identity_refresh_is_skipped_quietly(
    bot, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    await refresh_identities_once(bot, monkeypatch, ServiceError(NO_SUCH_FEED))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_another_failure_of_the_identity_refresh_is_still_logged(bot, monkeypatch, caplog):
    await refresh_identities_once(bot, monkeypatch, RuntimeError("boom"))
    assert any(r.exc_info for r in caplog.records)


def test_the_bot_gives_up_on_long_rate_limit_waits_of_its_own_sends(bot):
    # Lets a long 429 raise RateLimited (a RETRY) instead of sleeping past the Item's allowance.
    assert bot.http.max_ratelimit_timeout == 30.0


class FakeHook:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.deleted = error, False

    async def delete(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.deleted = True


@pytest.fixture
def hooks(bot, monkeypatch):
    made: dict[int, FakeHook] = {}
    monkeypatch.setattr(
        entry.discord.Webhook,
        "partial",
        lambda webhook_id, token, **kwargs: made.setdefault(webhook_id, FakeHook()),
    )
    return made


@pytest.mark.parametrize("event", ["on_guild_channel_delete", "on_thread_delete"])
async def test_a_deleted_channel_or_thread_loses_its_webhook(bot, hooks, event):
    bot.db.set_webhook(55, 7, "tok")
    bot.db.set_webhook(56, 8, "other")

    await getattr(bot, event)(SimpleNamespace(id=55))

    assert hooks[7].deleted and bot.db.get_webhook(55) is None
    assert bot.db.get_webhook(56) == (8, "other")


async def test_a_deleted_channel_also_cleans_the_webhooks_of_its_threads(bot, hooks):
    bot.db.set_webhook(55, 7, "tok")
    bot.db.set_webhook(66, 9, "thread")
    channel = SimpleNamespace(id=55, threads=[SimpleNamespace(id=66)])

    await bot.on_guild_channel_delete(channel)

    assert bot.db.get_webhook(55) is None and bot.db.get_webhook(66) is None


async def test_a_deleted_channel_leaves_its_feeds_for_the_next_check_to_pause(bot, hooks):
    feed = add_feed(bot, 1)
    channel_id = bot.db.get_feed(feed).channel_id
    bot.db.set_webhook(channel_id, 7, "tok")

    await bot.on_guild_channel_delete(SimpleNamespace(id=channel_id))

    assert bot.db.get_feed(feed).paused is None  # the Check meets Unknown Channel: LOST_CHANNEL
    assert bot.db.get_webhook(channel_id) is None


async def test_channel_deletion_without_a_webhook_or_with_a_failing_store_is_harmless(
    bot, monkeypatch
):
    await bot.on_guild_channel_delete(SimpleNamespace(id=1))
    monkeypatch.setattr(bot.db, "get_webhook", lambda channel_id: 1 / 0)
    await bot.on_thread_delete(SimpleNamespace(id=1))


async def test_unused_webhooks_are_cleaned_when_ready_and_then_regularly(bot, monkeypatch):
    calls: list[str] = []

    async def ready() -> None:
        pass

    async def cleanup() -> None:
        calls.append("cleanup")

    async def stop(seconds: float) -> None:
        calls.append("sleep")
        if calls.count("sleep") == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot, "wait_until_ready", ready)
    monkeypatch.setattr(bot.deliverer, "cleanup_unused_webhooks", cleanup)
    monkeypatch.setattr(entry.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await bot._clean_webhooks()
    assert calls == ["cleanup", "sleep", "cleanup", "sleep"]


def test_the_libraries_imported_directly_are_declared_at_the_locked_versions():
    import tomllib

    root = Path(__file__).parent.parent
    declared = set(tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"])
    locked = {
        line.strip().casefold()
        for line in (root / "requirements.lock").read_text().splitlines()
        if line.startswith(("aiohttp==", "yarl=="))
    }
    assert len(locked) == 2
    assert locked <= {dep.casefold() for dep in declared}
