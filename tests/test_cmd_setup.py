from __future__ import annotations

from typing import Any

import discord
import pytest
from fakes_discord import (
    MEMBER_AVATAR,
    OWNER,
    SERVER,
    USER,
    FakeChannel,
    FakeInteraction,
    component_ids,
)

from rssbot.commands import _ui as ui
from rssbot.commands import setup
from rssbot.db import Database
from rssbot.models import Actor, Change, Level, LogEntry, LogKind, TargetKind

LOGS = 70
HIDDEN = 71
VOICE = 72
CHANNELS = (
    FakeChannel(LOGS, "bot-logs"),
    FakeChannel(HIDDEN, "staff", discord.ChannelType.news, bot_can_post=False),
    FakeChannel(VOICE, "voice", discord.ChannelType.voice),
)


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def admin(db: Database, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(db, user_id=OWNER, channels=CHANNELS, **kwargs)


def logs_channel(db: Database) -> int | None:
    server = db.get_server(SERVER)
    return server.logs_channel_id if server else None


async def pick(interaction: FakeInteraction, channel_id: int) -> None:
    select = setup.LogsChannelSelect()
    select.item._values = [discord.Object(channel_id)]  # type: ignore[attr-defined]
    await select.callback(interaction)  # type: ignore[arg-type]


async def test_setup_shows_no_logs_channel(db: Database) -> None:
    interaction = admin(db)
    await setup.setup_command.callback(interaction)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    assert "**Logs channel**: none set" in sent["content"]
    assert sent["content"].splitlines()[1] == (
        "The bot reports what members did to Feeds and access, and Feed problems, "
        "in the Logs channel."
    )
    assert component_ids(sent["view"]) == ["rss:c:setup_logs", "rss:c:setup_clear"]
    select, clear = (row["components"][0] for row in sent["view"].to_components())
    assert select["type"] == discord.ComponentType.channel_select.value
    assert select["channel_types"] == [0, 5]  # text and announcement
    assert clear["disabled"] is True
    assert sent["view"].timeout is None


async def test_setup_shows_the_current_logs_channel(db: Database) -> None:
    db.set_logs_channel(SERVER, LOGS)
    interaction = admin(db)
    await setup.setup_command.callback(interaction)  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert f"**Logs channel**: <#{LOGS}>" in sent["content"]
    assert "Warning" not in sent["content"]
    select, clear = (row["components"][0] for row in sent["view"].to_components())
    assert select["default_values"] == [{"id": LOGS, "type": "channel"}]
    assert clear["disabled"] is False


async def test_setup_warns_when_the_logs_channel_was_deleted(db: Database) -> None:
    db.set_logs_channel(SERVER, 999)  # not among the Server's channels
    interaction = admin(db)
    await setup.setup_command.callback(interaction)  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert sent["content"].endswith(
        "**Warning**: that channel no longer exists. Choose another one or clear it."
    )
    assert "cannot see or post" not in sent["content"]
    select, clear = (row["components"][0] for row in sent["view"].to_components())
    # Discord may reject a default value naming a channel that does not exist.
    assert select.get("default_values", []) == []
    assert clear["disabled"] is False


async def test_a_deleted_logs_channel_can_be_cleared(db: Database) -> None:
    db.set_logs_channel(SERVER, 999)
    interaction = admin(db, type=discord.InteractionType.component)
    await setup.ClearLogsChannel().callback(interaction)  # type: ignore[arg-type]

    assert logs_channel(db) is None
    assert "Warning" not in interaction.text


async def test_setup_is_refused_for_a_manager(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = FakeInteraction(db, channels=CHANNELS)
    with pytest.raises(ui.UserError, match="Only Admins"):
        await setup.setup_command.callback(interaction)  # type: ignore[arg-type]
    assert interaction.calls == []


async def test_selecting_a_channel_sets_it_and_edits_the_message(db: Database) -> None:
    interaction = admin(db, type=discord.InteractionType.component)
    await pick(interaction, LOGS)

    assert logs_channel(db) == LOGS
    name, sent = interaction.last
    assert name == "edit_message"
    assert f"**Logs channel**: <#{LOGS}>" in sent["content"]
    assert "Warning" not in sent["content"]
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    assert component_ids(sent["view"]) == ["rss:c:setup_logs", "rss:c:setup_clear"]


async def test_selecting_a_channel_the_bot_cannot_post_in_warns(db: Database) -> None:
    interaction = admin(db, type=discord.InteractionType.component)
    await pick(interaction, HIDDEN)

    assert logs_channel(db) == HIDDEN
    assert f"**Warning**: the bot cannot see or post in <#{HIDDEN}>" in interaction.text


@pytest.mark.parametrize(
    ("channel_id", "message"),
    [(VOICE, "must be a text or announcement channel"), (999, "cannot find that channel")],
)
async def test_unsuitable_channels_are_refused(db: Database, channel_id: int, message: str) -> None:
    db.set_logs_channel(SERVER, LOGS)
    interaction = admin(db, type=discord.InteractionType.component)
    await pick(interaction, channel_id)

    assert logs_channel(db) == LOGS
    assert interaction.last[0] == "send_message"
    assert message in interaction.text


async def test_clear_unsets_it(db: Database) -> None:
    db.set_logs_channel(SERVER, LOGS)
    interaction = admin(db, type=discord.InteractionType.component)
    await setup.ClearLogsChannel().callback(interaction)  # type: ignore[arg-type]

    assert logs_channel(db) is None
    # Deferred first: the last report is sent to the old channel before the panel is redrawn.
    assert [name for name, _ in interaction.calls] == ["defer", "edit_original_response"]
    assert "none set" in interaction.text


@pytest.mark.parametrize("use", ["select", "clear"])
async def test_components_recheck_admin_access(db: Database, use: str) -> None:
    db.set_logs_channel(SERVER, LOGS)
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = FakeInteraction(db, channels=CHANNELS, type=discord.InteractionType.component)
    if use == "select":
        await pick(interaction, HIDDEN)
    else:
        await setup.ClearLogsChannel().callback(interaction)  # type: ignore[arg-type]

    assert logs_channel(db) == LOGS
    assert interaction.calls == [
        (
            "send_message",
            {"content": ui.NEED_ADMIN, "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]


async def test_another_servers_logs_channel_is_untouched(db: Database) -> None:
    db.set_logs_channel(200, LOGS)
    await setup.ClearLogsChannel().callback(admin(db))  # type: ignore[arg-type]
    assert db.get_server(200).logs_channel_id == LOGS  # type: ignore[union-attr]


# -- Log entries --

ADMIN = Actor(OWNER, "Alex", MEMBER_AVATAR)


def logged(db: Database) -> list[LogEntry]:
    """The Server's Log entries, oldest first."""
    return db.list_log_entries(SERVER, limit=100)[::-1]


async def test_setting_the_logs_channel_is_saved_and_reported_in_it(db: Database) -> None:
    interaction = admin(db, type=discord.InteractionType.component)
    await pick(interaction, LOGS)
    [entry] = logged(db)
    assert (entry.kind, entry.actor_id) == (LogKind.LOGS_CHANNEL_CHANGED, OWNER)
    assert entry.changes == (Change("Logs channel", "", f"<#{LOGS}>"),)
    await interaction.journal.drain()
    assert interaction.reports.sent == [(LOGS, [entry], ADMIN)]


async def test_changing_the_logs_channel_is_reported_in_the_new_one(db: Database) -> None:
    db.set_logs_channel(SERVER, LOGS)
    interaction = admin(db, type=discord.InteractionType.component)
    await pick(interaction, HIDDEN)
    [entry] = logged(db)
    assert entry.changes == (Change("Logs channel", f"<#{LOGS}>", f"<#{HIDDEN}>"),)
    await interaction.journal.drain()
    assert interaction.reports.sent == [(HIDDEN, [entry], ADMIN)]


async def test_clearing_the_logs_channel_is_reported_in_it_before_it_is_cleared(
    db: Database,
) -> None:
    db.set_logs_channel(SERVER, LOGS)
    interaction = admin(db, type=discord.InteractionType.component)
    await setup.ClearLogsChannel().callback(interaction)  # type: ignore[arg-type]
    [entry] = logged(db)
    assert (entry.kind, entry.actor_id) == (LogKind.LOGS_CHANNEL_CHANGED, OWNER)
    assert entry.changes == (Change("Logs channel", f"<#{LOGS}>", ""),)
    # Already sent when the handler returns, and to the channel the Server still had then.
    assert interaction.reports.sent == [(LOGS, [entry], ADMIN)]
    assert logs_channel(db) is None


async def test_clearing_still_clears_when_the_report_cannot_be_saved(db: Database) -> None:
    db.set_logs_channel(SERVER, LOGS)
    db._conn.execute("DROP TABLE log_entries")
    interaction = admin(db, type=discord.InteractionType.component)
    await setup.ClearLogsChannel().callback(interaction)  # type: ignore[arg-type]
    assert logs_channel(db) is None
    assert "none set" in interaction.text and interaction.reports.sent == []


async def test_what_did_not_change_the_logs_channel_is_not_saved(db: Database) -> None:
    db.set_logs_channel(SERVER, LOGS)
    await pick(admin(db, type=discord.InteractionType.component), LOGS)  # the same again
    await pick(admin(db, type=discord.InteractionType.component), VOICE)  # refused
    await setup.setup_command.callback(admin(db))  # type: ignore[arg-type]
    db.set_logs_channel(SERVER, None)
    await setup.ClearLogsChannel().callback(admin(db))  # type: ignore[arg-type]
    not_admin = FakeInteraction(db, channels=CHANNELS, type=discord.InteractionType.component)
    await pick(not_admin, LOGS)
    assert logged(db) == [] and logs_channel(db) is None
