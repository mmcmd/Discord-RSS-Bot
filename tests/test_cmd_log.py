"""/log, and the Log entry lines and member lookups that /log and /feed history share."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import OWNER, SERVER, USER, FakeInteraction, component_ids

from rssbot.commands import _history as history
from rssbot.commands import _ui as ui
from rssbot.commands import log
from rssbot.db import Database
from rssbot.models import Change, Level, LogEntry, LogKind, TargetKind

OTHER_SERVER = 200
SAM = 3
KIM = 4
AT = 1_700_000_000
COMPONENT = discord.InteractionType.component


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def admin(db: Database, **kwargs: Any) -> FakeInteraction:
    """An Admin; the members the entries are by are in the Server's cache unless said otherwise."""
    kwargs.setdefault("user_id", OWNER)
    kwargs.setdefault("cached", (USER, SAM, KIM))
    return FakeInteraction(db, **kwargs)


def click(db: Database, **kwargs: Any) -> FakeInteraction:
    interaction = admin(db, type=COMPONENT, **kwargs)
    interaction.message = object()  # type: ignore[attr-defined]
    return interaction


def entry(
    db: Database,
    kind: LogKind = LogKind.FEED_EDITED,
    *,
    by: int | None = USER,
    name: str = "Alex",
    at: int = AT,
    server_id: int = SERVER,
    feed: str = "Example News",
    channel: int | None = 70,
    **fields: Any,
) -> LogEntry:
    return db.add_log_entry(
        server_id=server_id,
        at=at,
        actor_id=by,
        actor_name=name if by is not None else "",
        kind=kind,
        feed_name=feed,
        channel_id=channel,
        **fields,
    )


async def run(interaction: FakeInteraction, **options: Any) -> str:
    await log.log_command.callback(interaction, **options)  # type: ignore[arg-type]
    return interaction.text


def member(user_id: int) -> Any:
    return SimpleNamespace(id=user_id)


# -- The groups and the words --


def test_every_kind_is_in_exactly_one_group() -> None:
    grouped = [kind for group in log.GROUPS for kind in group.kinds]
    assert sorted(grouped) == sorted(LogKind)
    assert len(set(grouped)) == len(grouped)


def test_every_kind_has_words() -> None:
    assert set(history.VERBS) == set(LogKind)


def test_the_kind_choices_are_the_groups_numbered_from_one() -> None:
    assert [(c.name, c.value) for c in log.KIND_CHOICES] == [
        ("Feeds added and removed", 1),
        ("Pauses and resumes", 2),
        ("Edits", 3),
        ("Access", 4),
        ("The bot's own reports", 5),
    ]


def test_the_command_serialises_with_its_options() -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = app_commands.CommandTree(client)
    log.register(tree)
    (command,) = tree.get_commands()
    payload = command.to_dict(tree)
    assert payload["name"] == "log"
    assert payload["contexts"] == [0]
    assert payload.get("default_member_permissions") is None
    user, kind = payload["options"]
    assert (user["name"], user["type"], user.get("required", False)) == ("member", 6, False)
    assert (kind["name"], kind["type"], kind.get("required", False)) == ("kind", 4, False)
    assert [(c["name"], c["value"]) for c in kind["choices"]] == [
        (c.name, c.value) for c in log.KIND_CHOICES
    ]


# -- Access --


async def test_a_manager_is_refused(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    entry(db)
    interaction = FakeInteraction(db)
    with pytest.raises(ui.UserError, match="Only Admins"):
        await run(interaction)
    assert interaction.calls == []


async def test_a_manager_is_refused_on_a_page_button(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = FakeInteraction(db, type=COMPONENT)
    await log.LogPage(0, 0, 1).callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == ui.NEED_ADMIN
    assert "view" not in interaction.last[1]


async def test_an_admin_by_grant_is_allowed(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.ADMIN)
    entry(db)
    assert "edited" in await run(FakeInteraction(db))


# -- What it shows --


async def test_the_whole_server_newest_first_with_the_feed_on_each_line(db: Database) -> None:
    entry(db, LogKind.FEED_ADDED, at=AT)
    entry(
        db,
        LogKind.FEED_EDITED,
        at=AT + 60,
        changes=[Change("Name", "Old", "New"), Change("Check interval", "10 minutes", "1 hour")],
    )
    entry(
        db,
        LogKind.FEED_REMOVED,
        at=AT + 120,
        by=SAM,
        name="Sam",
        feed="Gone Site",
        channel=71,
        detail="removed its 2 Filters",
    )
    entry(
        db,
        LogKind.GRANT_GIVEN,
        at=AT + 180,
        feed="",
        channel=None,
        detail="<@&50>",
        changes=[Change("Level", "", "Manager")],
    )
    entry(
        db,
        LogKind.LOGS_CHANNEL_CHANGED,
        at=AT + 240,
        feed="",
        channel=None,
        changes=[Change("Logs channel", "", "<#80>")],
    )
    entry(
        db,
        LogKind.FEED_AUTO_PAUSED,
        at=AT + 300,
        by=None,
        detail="The bot can no longer post in that channel. Check it, then resume the Feed.",
    )
    interaction = admin(db)
    text = await run(interaction)

    assert text.splitlines() == [
        "**Log entries**: 6",
        f"<t:{AT + 300}:f> the bot paused **Example News** in <#70> · "
        "The bot can no longer post in that channel. Check it, then resume the Feed.",
        f"<t:{AT + 240}:f> <@{USER}> changed the Logs channel · Logs channel: *nothing* → <#80>",
        f"<t:{AT + 180}:f> <@{USER}> gave access · Level: *nothing* → Manager · <@&50>",
        f"<t:{AT + 120}:f> <@{SAM}> removed **Gone Site** in <#71> · removed its 2 Filters",
        f"<t:{AT + 60}:f> <@{USER}> edited **Example News** in <#70> · "
        "Name: Old → New · Check interval: 10 minutes → 1 hour",
        f"<t:{AT}:f> <@{USER}> added **Example News** in <#70>",
    ]
    name, sent = interaction.last
    assert name == "send_message" and sent["ephemeral"] is True
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    assert "view" not in sent  # one page


async def test_an_empty_server_and_no_match(db: Database) -> None:
    assert await run(admin(db)) == log.NO_LOG
    entry(db, LogKind.FEED_ADDED)
    entry(db, server_id=OTHER_SERVER)
    assert await run(admin(db), kind=2) == log.NO_MATCH
    assert await run(admin(db), member=member(SAM)) == log.NO_MATCH


async def test_another_servers_entries_are_not_shown(db: Database) -> None:
    entry(db, server_id=OTHER_SERVER, feed="Secret Site", detail="secret")
    entry(db, feed="Mine")
    text = await run(admin(db))
    assert "Mine" in text and "Secret" not in text and "secret" not in text
    assert text.startswith("**Log entries**: 1\n")


async def test_the_filters(db: Database) -> None:
    for kind in LogKind:
        entry(db, kind, by=None if kind in log.GROUPS[4].kinds else USER, feed=f"F {kind.name}")
    entry(db, LogKind.FEED_EDITED, by=SAM, name="Sam", feed="By Sam")

    for number, group in enumerate(log.GROUPS, 1):
        text = await run(admin(db), kind=number)
        header, *lines = text.splitlines()
        expected = len(group.kinds) + (1 if LogKind.FEED_EDITED in group.kinds else 0)
        assert header == f"**Log entries** ({group.name}): {expected}"
        assert len(lines) == expected

    text = await run(admin(db), member=member(SAM))
    assert text.splitlines()[0] == f"**Log entries** (by <@{SAM}>): 1"
    assert "By Sam" in text and text.count("\n") == 1

    text = await run(admin(db), member=member(SAM), kind=3)
    assert text.splitlines()[0] == f"**Log entries** (by <@{SAM}> · Edits): 1"
    assert await run(admin(db), member=member(SAM), kind=1) == log.NO_MATCH
    # The bot's own reports have no member to filter by.
    assert await run(admin(db), member=member(USER), kind=5) == log.NO_MATCH


# -- Pages --


def many(db: Database, count: int, **kwargs: Any) -> None:
    for number in range(count):
        entry(db, at=AT + number, feed=f"Feed {number:02}", **kwargs)


async def test_pages_of_ten_with_stateless_buttons(db: Database) -> None:
    many(db, 25)
    interaction = admin(db)
    text = await run(interaction)

    lines = text.splitlines()
    assert lines[0] == "**Log entries**: 25" and lines[-1] == "Page 1 of 3"
    assert len(lines) == 12 and "Feed 24" in lines[1] and "Feed 15" in lines[10]
    view = interaction.last[1]["view"]
    assert component_ids(view) == ["rss:c:log_page:0:0:0", "rss:c:log_page:0:0:1"]
    assert [c["disabled"] for c in view.to_components()[0]["components"]] == [True, False]
    assert view.timeout is None

    last = click(db)
    await log.LogPage(0, 0, 2).callback(last)  # type: ignore[arg-type]
    assert last.last[0] == "edit_message"
    assert last.text.splitlines()[-1] == "Page 3 of 3"
    assert len(last.text.splitlines()) == 7  # header, 5 entries, footer
    assert component_ids(last.last[1]["view"]) == ["rss:c:log_page:0:0:1", "rss:c:log_page:0:0:2"]

    beyond = click(db)
    await log.LogPage(0, 0, 99).callback(beyond)  # type: ignore[arg-type]
    assert beyond.text.splitlines()[-1] == "Page 3 of 3"


async def test_paging_keeps_the_filters(db: Database) -> None:
    many(db, 15, by=SAM, name="Sam")
    many(db, 15, by=USER, kind=LogKind.FEED_PAUSED)
    many(db, 15, by=SAM, name="Sam", kind=LogKind.FEED_PAUSED)
    interaction = admin(db)
    await run(interaction, member=member(SAM), kind=2)

    assert component_ids(interaction.last[1]["view"]) == [
        f"rss:c:log_page:{SAM}:2:0",
        f"rss:c:log_page:{SAM}:2:1",
    ]
    second = click(db)
    await log.LogPage(SAM, 2, 1).callback(second)  # type: ignore[arg-type]
    header, *lines, footer = second.text.splitlines()
    assert header == f"**Log entries** (by <@{SAM}> · Pauses and resumes): 15"
    assert footer == "Page 2 of 2" and len(lines) == 5
    assert all(f"<@{SAM}> paused" in line for line in lines)
    assert component_ids(second.last[1]["view"]) == [
        f"rss:c:log_page:{SAM}:2:0",
        f"rss:c:log_page:{SAM}:2:1",
    ]


async def test_a_page_button_for_an_unknown_group_is_out_of_date(db: Database) -> None:
    many(db, 3)
    interaction = click(db)
    await log.LogPage(0, len(log.GROUPS) + 1, 0).callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == ui.STALE


async def test_a_page_that_shrinks_loses_its_buttons(db: Database) -> None:
    many(db, 3)
    interaction = click(db)
    await log.LogPage(0, 0, 1).callback(interaction)  # type: ignore[arg-type]
    assert interaction.last[0] == "edit_message"
    assert interaction.last[1]["view"] is None and "Page" not in interaction.text


# -- Members who left --


async def test_a_member_who_left_is_named_from_the_entry(db: Database) -> None:
    entry(db, by=SAM, name="Sam *the* @everyone", feed="A")
    entry(db, by=USER, feed="B")
    entry(db, by=KIM, name="Kim", feed="C")
    interaction = admin(db, members=(OWNER, USER, KIM), cached=())
    text = await run(interaction)

    assert f"<@{USER}> edited **B**" in text and f"<@{KIM}> edited **C**" in text
    assert "Sam \\*the\\* @​everyone (left the Server) edited **A**" in text
    assert f"<@{SAM}>" not in text
    assert interaction.guild.fetched == [KIM, USER, SAM]  # type: ignore[union-attr]
    assert [name for name, _ in interaction.calls] == ["defer", "followup"]


async def test_each_member_is_looked_up_once_and_only_when_not_cached(db: Database) -> None:
    for _ in range(3):
        entry(db, by=SAM, name="Sam")
    entry(db, by=KIM, name="Kim")
    entry(db, by=USER, name="Alex")  # whoever runs the command is plainly in the Server
    interaction = admin(db, user_id=USER, administrator=True, members=(), cached=(KIM,))
    text = await run(interaction)

    assert interaction.guild.fetched == [SAM]  # type: ignore[union-attr]
    assert text.count("Sam (left the Server)") == 3
    assert f"<@{KIM}>" in text and f"<@{USER}>" in text


async def test_nothing_is_looked_up_for_members_in_the_cache(db: Database) -> None:
    entry(db, by=SAM, name="Sam")
    interaction = admin(db, members=(), cached=(SAM,))
    text = await run(interaction)
    assert interaction.guild.fetched == []  # type: ignore[union-attr]
    assert f"<@{SAM}>" in text
    assert [name for name, _ in interaction.calls] == ["send_message"]  # no defer needed


async def test_a_failed_lookup_shows_the_mention(db: Database) -> None:
    entry(db, by=SAM, name="Sam")
    entry(db, by=KIM, name="Kim")
    interaction = admin(db, members=(), cached=(), fetch_fails=(SAM,))
    text = await run(interaction)
    assert f"<@{SAM}> edited" in text and "Sam (left" not in text
    assert "Kim (left the Server)" in text


async def test_more_than_ten_members_are_not_looked_up(db: Database) -> None:
    for number in range(11):
        entry(db, by=1000 + number, name=f"M{number}")
    interaction = admin(db, members=(), cached=())
    text = await run(interaction)  # only the first page: 10 entries
    assert interaction.guild.fetched == [1000 + number for number in range(10, 0, -1)]  # type: ignore[union-attr]
    assert text.count("(left the Server)") == 10

    actors = [
        LogEntry(0, SERVER, AT, 2000 + n, "x", LogKind.FEED_EDITED)
        for n in range(ui.MAX_LOOKUPS + 1)
    ]
    interaction = admin(db, members=(), cached=())
    shown = await ui.actors_of(interaction, actors)  # type: ignore[arg-type]
    assert interaction.guild.fetched == []  # type: ignore[union-attr]
    assert shown == {2000 + n: f"<@{2000 + n}>" for n in range(ui.MAX_LOOKUPS + 1)}
    assert interaction.calls == []


async def test_actors_of_without_a_server_or_a_name(db: Database) -> None:
    gone = LogEntry(0, SERVER, AT, SAM, "", LogKind.FEED_EDITED)
    bot = LogEntry(0, SERVER, AT, None, "", LogKind.FEED_BROKEN)
    interaction = admin(db, members=(), cached=())
    shown = await ui.actors_of(interaction, [gone, bot])  # type: ignore[arg-type]
    assert shown == {SAM: "A member (left the Server)"}
    assert ui.actor_words(bot, shown) == "the bot"
    assert ui.actor_words(gone, shown) == "A member (left the Server)"
    assert ui.actor_words(gone, {}) == f"<@{SAM}>"

    direct = FakeInteraction(db, guild_id=None)
    assert await ui.actors_of(direct, [gone]) == {SAM: f"<@{SAM}>"}  # type: ignore[arg-type]


# -- Hostile text --

HOSTILE = "**bold** [click](https://evil.example) @everyone @here <@&1> <#2> # head `code` ||x||"


async def test_text_from_members_and_feeds_is_defused(db: Database) -> None:
    entry(
        db,
        by=SAM,
        name=HOSTILE,
        feed=HOSTILE,
        detail=HOSTILE,
        changes=[Change(HOSTILE, HOSTILE, HOSTILE)],
    )
    interaction = admin(db, members=(), cached=())
    text = await run(interaction)

    for danger in ("@everyone", "@here", "[click](", "<@&1>", "<#2>", "**bold**", "||x||"):
        assert danger not in text, danger
    assert "\\*\\*bold\\*\\* \\[click\\]\\(https" in text  # shown as typed
    assert "<#70>" in text  # the channel the bot recorded
    assert len(text) <= ui.MESSAGE_LIMIT


async def test_a_page_of_huge_entries_still_fits_and_keeps_every_head(db: Database) -> None:
    huge = "@everyone " + "*" * 3000
    entry(db, LogKind.FEED_ADDED, at=AT - 1)  # the oldest, so on page 2
    for number in range(10):
        entry(
            db,
            LogKind.FILTER_CHANGED,
            by=1000 + number,
            name="_" * 100,
            at=AT + number,
            feed="_" * 300,
            detail=huge,
            changes=[Change("L" * 100, huge, huge)] * 5,
        )
    interaction = admin(db, members=(), cached=())
    text = await run(interaction)

    assert len(text) <= ui.MESSAGE_LIMIT and not text.endswith("…")
    lines = text.splitlines()
    assert lines[0] == "**Log entries**: 11" and lines[-1] == "Page 1 of 2"
    assert len(lines) == 12
    for number, line in enumerate(lines[1:11]):
        assert line.startswith(f"<t:{AT + 9 - number}:f> ")
        assert "changed the Filters of" in line and "(left the Server)" in line
    assert "@everyone" not in text
    assert interaction.last[1]["allowed_mentions"] is ui.NO_MENTIONS


def test_a_cut_never_leaves_half_an_escape() -> None:
    text = "\\*" * 20
    for limit in range(2, 40):
        cut = history._trim(text, limit)
        assert len(cut) <= limit
        assert cut == text or cut.endswith("…")
        assert (len(cut[:-1]) - len(cut[:-1].rstrip("\\"))) % 2 == 0


def test_lines_share_the_room_the_short_ones_leave() -> None:
    lines = [("a" * 10, ""), ("b" * 10, "t" * 500), ("c" * 10, "u" * 500), ("d" * 10, "short")]
    fitted = history._fit(lines, 300)
    assert len("\n".join(fitted)) <= 300
    assert fitted[0] == "a" * 10 and fitted[3] == f"{'d' * 10} · short"
    assert fitted[1].startswith("b" * 10 + " · t") and fitted[1].endswith("…")
    assert abs(len(fitted[1]) - len(fitted[2])) <= 1
