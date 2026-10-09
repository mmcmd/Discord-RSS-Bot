from __future__ import annotations

import datetime
import re

import discord
import pytest

from rssbot import logembed
from rssbot.logembed import AMBER, BLUE, GREEN, GREY, RED, build_messages, build_note, render_text
from rssbot.models import Actor, Change, LogEntry, LogKind

SERVER = 1
AT = 1_760_000_000
ALEX = Actor(987, "alex", "https://cdn.example/alex.png")
BOT = Actor(None, "RSS Bot", "https://cdn.example/bot.png")  # as the notifier fills it in


def entry(kind: LogKind = LogKind.FEED_PAUSED, **overrides: object) -> LogEntry:
    args: dict[str, object] = {
        "id": 1,
        "server_id": SERVER,
        "at": AT,
        "actor_id": 987,
        "actor_name": "alex",
        "kind": kind,
        "feed_id": 45,
        "feed_name": "BBC News",
        "channel_id": 678,
        "feed_url": "https://example.com/rss",
    }
    args.update(overrides)
    return LogEntry(**args)  # type: ignore[arg-type]


def one(entries: list[LogEntry], actor: Actor = ALEX) -> discord.Embed:
    ((embed,),) = build_messages(entries, actor)
    return embed


def check_limits(messages: list[list[discord.Embed]]) -> None:
    for embeds in messages:
        assert 1 <= len(embeds) <= 10
        assert sum(len(embed) for embed in embeds) <= 6000
        for embed in embeds:
            assert len(embed.title or "") <= 256
            assert len(embed.description or "") <= 4096
            assert len(embed.author.name or "") <= 256


# -- one entry --


def test_a_member_action() -> None:
    embed = one([entry()])
    assert embed.title == "Feed paused"
    assert embed.author.name == "alex"
    assert embed.author.icon_url == "https://cdn.example/alex.png"
    assert embed.description == (
        "**BBC News** in <#678>\n<https://example.com/rss>\nBy <@987> `987`"
    )
    assert embed.timestamp == datetime.datetime.fromtimestamp(AT, datetime.UTC)
    assert embed.colour.value == GREY


def test_changes_and_detail() -> None:
    changes = (
        Change("Check interval", "10 minutes", "30 minutes"),
        Change("Name", "", "BBC *World*"),
    )
    embed = one([entry(LogKind.FEED_EDITED, changes=changes, detail="removed Button 2")])
    assert embed.title == "Feed edited"
    assert embed.description.splitlines() == [
        "**BBC News** in <#678>",
        "<https://example.com/rss>",
        "**Check interval**: 10 minutes → 30 minutes",
        "**Name**: *nothing* → BBC \\*World\\*",
        "removed Button 2",
        "By <@987> `987`",
    ]


def test_the_bot_as_actor_has_its_own_author_line_and_no_mention() -> None:
    report = entry(
        LogKind.FEED_AUTO_PAUSED,
        actor_id=None,
        actor_name="",
        detail="the bot can no longer post in that channel",
    )
    embed = one([report], BOT)
    assert embed.author.name == "RSS Bot"
    assert embed.author.icon_url == "https://cdn.example/bot.png"
    assert embed.title == "Feed paused by the bot"
    assert embed.description.splitlines() == [
        "**BBC News** in <#678>",
        "<https://example.com/rss>",
        "the bot can no longer post in that channel",
    ]
    assert "<@" not in embed.description

    nameless = one([report], Actor.bot())  # the notifier could not tell the bot's name
    assert nameless.author.name is None and nameless.description == embed.description


@pytest.mark.parametrize(
    ("kind", "colour"),
    [
        (LogKind.FEED_ADDED, GREEN),
        (LogKind.FEED_RESUMED, GREEN),
        (LogKind.FEED_PAUSED, GREY),
        (LogKind.FEED_REMOVED, RED),
        (LogKind.FEED_EDITED, BLUE),
        (LogKind.GRANT_GIVEN, BLUE),
        (LogKind.GRANT_TAKEN, BLUE),
        (LogKind.LOGS_CHANNEL_CHANGED, BLUE),
        (LogKind.FEED_BROKEN, RED),
        (LogKind.FEED_AUTO_PAUSED, AMBER),
        (LogKind.FEED_WORKING_AGAIN, GREEN),
    ],
)
def test_colours(kind: LogKind, colour: int) -> None:
    assert one([entry(kind)]).colour.value == colour
    assert len({GREEN, GREY, RED, BLUE, AMBER}) == 5


@pytest.mark.parametrize("kind", list(LogKind))
def test_every_kind_has_a_title_and_renders(kind: LogKind) -> None:
    embed = one([entry(kind)])
    assert embed.title == logembed.title_of(kind) != ""
    assert "guild" not in embed.title.lower() and "audit" not in embed.title.lower()
    (text,) = render_text([entry(kind)], ALEX)
    assert text.startswith(f"**{embed.title}** by ")


def test_entries_about_no_feed() -> None:
    grant = entry(
        LogKind.GRANT_GIVEN,
        feed_id=None,
        feed_name="",
        channel_id=None,
        feed_url="",
        detail="Manager to <@&555> and <@666>, see <#777>",
    )
    embed = one([grant])
    assert embed.title == "Access given"
    # A Grant's target is a reference the bot wrote: it stays a reference.
    assert embed.description.splitlines() == [
        "Manager to <@&555> and <@666>, see <#777>",
        "By <@987> `987`",
    ]

    logs = entry(
        LogKind.LOGS_CHANNEL_CHANGED,
        feed_id=None,
        feed_name="",
        feed_url="",
        channel_id=42,
        changes=(Change("Logs channel", "", "#logs"),),
    )
    assert one([logs]).description.splitlines() == [
        "<#42>",
        "**Logs channel**: *nothing* → \\#logs",
        "By <@987> `987`",
    ]


# -- text from members and feeds --


NASTY = "x** was removed. [Restore it](https://evil.example) **<@1> <@&2> <#3> @everyone @here"


LIVE_MENTION = re.compile(r"(?<!\\)<[@#]")


def assert_defused(text: str) -> None:
    """No formatting character, link or mention of the nasty text is left live."""
    for live in ("](", "<@1>", "<@&2>", "<#3>", "@everyone", "@here"):
        assert live not in text
    assert "x\\*\\* was removed" in text


def test_a_feed_name_cannot_add_formatting_links_or_mentions() -> None:
    embed = one([entry(feed_name=NASTY, detail=NASTY)])
    where, _, detail, by = embed.description.splitlines()
    assert_defused(where)
    assert_defused(detail)
    assert where.endswith("** in <#678>") and by == "By <@987> `987`"
    (text,) = render_text([entry(feed_name=NASTY, detail=NASTY)], Actor(987, NASTY))
    assert_defused(text.replace("<@987>", ""))
    # Only the bot's own references are live: the actor and the Feed's channel.
    assert LIVE_MENTION.findall(text) == ["<@", "<#"]


@pytest.mark.parametrize(
    "text",
    [
        "# Heading",
        "> quote",
        "- item",
        "`code`",
        "||spoiler||",
        "~~gone~~",
        "__under__",
        "<t:0:R>",
        "<:emoji:123>",
        "<https://evil.example>",
        "a\nBy <@1> `1`",
        "a\r\n**Feed removed**",
        "\\",
    ],
)
def test_safe_shows_text_as_it_was_typed_on_one_line(text: str) -> None:
    safe = logembed.safe(text)
    assert "\n" not in safe and "\r" not in safe
    # Every character Discord gives a meaning to has a backslash in front of it.
    position = 0
    while position < len(safe):
        if safe[position] == "\\":
            position += 2
            continue
        assert safe[position] not in "*_~`|>[]()#<:-", safe
        position += 1
    assert safe.replace("\\", "") == " ".join(text.split()).replace("\\", "")


def test_mentions_are_kept_only_whole_and_only_when_asked() -> None:
    assert logembed.safe("<@1> <@&2> <#3>") == "\\<@1\\> \\<@&2\\> \\<\\#3\\>"
    assert logembed.safe("<@1> <@&2> <#3>", keep_mentions=True) == "<@1> <@&2> <#3>"
    kept = logembed.safe("<@1x> <@!> [a](b) <@everyone> @everyone", keep_mentions=True)
    assert not LIVE_MENTION.search(kept) and "](" not in kept and "@everyone" not in kept
    # Only a Grant's detail keeps them.
    assert "\\<@&5\\>" in one([entry(LogKind.FEED_EDITED, detail="<@&5>")]).description
    assert "\n<@&5>\n" in one([entry(LogKind.GRANT_TAKEN, detail="<@&5>")]).description


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a> [click](https://evil.example) <b",
        "https://example.com/a b\nc",
        "https://example.com/`x`",
    ],
)
def test_an_address_cannot_leave_its_brackets(url: str) -> None:
    line = one([entry(feed_url=url)]).description.splitlines()[1]
    assert line.startswith("<https://example.com/") and line.endswith(">")
    inner = line[1:-1]
    assert not set(inner) & set("<>` \n")
    assert "%3E" in inner or "%20" in inner or "%60" in inner


def test_a_change_value_cannot_add_formatting() -> None:
    change = Change("Name", "a](https://evil.example)", "**b**\n# c")
    line = one([entry(LogKind.FEED_EDITED, changes=(change,))]).description.splitlines()[2]
    assert line == "**Name**: a\\]\\(https\\://evil.example\\) → \\*\\*b\\*\\* \\# c"


def test_oversized_text_is_cut_rather_than_refused() -> None:
    huge = entry(
        LogKind.FEED_EDITED,
        feed_name="n" * 5000,
        feed_url="https://example.com/" + "u" * 5000,
        detail="d" * 5000,
        changes=tuple(Change("L", "b" * 5000, "a" * 5000) for _ in range(30)),
    )
    messages = build_messages([huge], Actor(987, "a" * 1000))
    check_limits(messages)
    assert sum(len(embeds) for embeds in messages) >= 2  # continued, not cut off
    assert messages[0][0].title == "Feed edited"
    assert messages[-1][-1].description.endswith("By <@987> `987`")
    assert all(len(text) <= 2000 for text in render_text([huge], Actor(987, "a" * 1000)))


# -- several entries: an OPML import --


def imported(count: int, name: str = "Feed {n}") -> list[LogEntry]:
    return [
        entry(LogKind.FEED_ADDED, id=n, feed_id=n, feed_name=name.format(n=n), channel_id=1000 + n)
        for n in range(count)
    ]


def test_a_small_import_is_one_embed_listing_every_feed() -> None:
    embed = one(imported(3), Actor(5, "Sam"))
    assert embed.title == "3 Feeds imported"
    assert embed.author.name == "Sam"
    assert embed.colour.value == GREEN
    assert embed.description.splitlines() == [
        "- **Feed 0** in <#1000>",
        "- **Feed 1** in <#1001>",
        "- **Feed 2** in <#1002>",
        "By <@5> `5`",
    ]


@pytest.mark.parametrize("filler", ["x", "*"])  # "*" doubles in length when escaped
def test_an_import_of_100_feeds_with_100_character_names_shows_every_name(filler: str) -> None:
    entries = imported(100, name="{n:03d} " + filler * 96)
    assert all(len(e.feed_name) == 100 for e in entries)
    messages = build_messages(entries, Actor(5, "Sam"))
    check_limits(messages)
    assert len(messages) > 1  # it does not fit one message, so it takes more

    embeds = [embed for message in messages for embed in message]
    assert embeds[0].title == "100 Feeds imported"
    assert all(embed.title == "100 Feeds imported (continued)" for embed in embeds[1:])
    assert all(embed.colour.value == GREEN and embed.author.name == "Sam" for embed in embeds)
    listed = [line for embed in embeds for line in embed.description.splitlines()]
    shown = filler if filler == "x" else "\\*"
    assert listed == [
        *(f"- **{n:03d} {shown * 96}** in <#{1000 + n}>" for n in range(100)),
        "By <@5> `5`",
    ]
    assert "more" not in " ".join(listed)

    texts = render_text(entries, Actor(5, "Sam"))
    assert all(len(text) <= 2000 for text in texts)
    plain = "\n".join(texts).splitlines()
    assert plain[0] == f"**100 Feeds imported** by **Sam** (<@5> `5`) <t:{AT}:f>"
    assert plain[1:] == listed[:-1]


def test_a_huge_import_never_exceeds_ten_embeds_a_message() -> None:
    messages = build_messages(imported(1000, name="{n} " + "y" * 95), Actor(5, "Sam"))
    check_limits(messages)
    listed = [
        line for message in messages for embed in message for line in embed.description.split("\n")
    ]
    assert len(listed) == 1001


def test_entries_of_different_kinds_are_a_report_each() -> None:
    (embeds,) = build_messages([entry(LogKind.FEED_PAUSED), entry(LogKind.FEED_RESUMED)], ALEX)
    assert [embed.title for embed in embeds] == ["Feed paused", "Feed resumed"]
    texts = render_text([entry(LogKind.FEED_PAUSED), entry(LogKind.FEED_RESUMED)], ALEX)
    assert len(texts) == 2

    grouped = one([entry(LogKind.FEED_REMOVED), entry(LogKind.FEED_REMOVED)])
    assert grouped.title == "2 Feeds removed"
    other = one([entry(LogKind.FEED_EDITED), entry(LogKind.FEED_EDITED)])
    assert other.title == "Feed edited (2)"
    assert build_messages([], ALEX) == [] and render_text([], ALEX) == []


# -- plain text --


def test_plain_text_says_the_same() -> None:
    change = Change("Check interval", "10 minutes", "30 minutes")
    (text,) = render_text([entry(LogKind.FEED_EDITED, changes=(change,), detail="why")], ALEX)
    assert text.splitlines() == [
        f"**Feed edited** by **alex** (<@987> `987`) <t:{AT}:f>",
        "**BBC News** in <#678>",
        "<https://example.com/rss>",
        "**Check interval**: 10 minutes → 30 minutes",
        "why",
    ]
    (text,) = render_text([entry(LogKind.FEED_BROKEN, actor_id=None, actor_name="")], BOT)
    assert text.splitlines()[0] == f"**Broken feed** by **RSS Bot** <t:{AT}:f>"
    (text,) = render_text([entry(LogKind.FEED_BROKEN, actor_id=None)], Actor.bot())
    assert text.splitlines()[0] == f"**Broken feed** <t:{AT}:f>"


# -- notes --


def test_a_note_is_an_amber_embed_under_the_bots_name() -> None:
    embed = build_note("Feeds in <#1> are being posted under the bot's own name.", BOT)
    assert embed.description == "Feeds in <#1> are being posted under the bot's own name."
    assert embed.colour.value == AMBER
    assert (embed.author.name, embed.author.icon_url) == ("RSS Bot", "https://cdn.example/bot.png")
    assert len(build_note("x" * 9000, BOT).description) == 4096


# -- Changes --


def test_a_changed_channel_shows_as_the_channel() -> None:
    moved = entry(LogKind.FEED_EDITED, changes=(Change("Channel", "<#111>", "<#222>"),))
    assert "**Channel**: <#111> → <#222>" in one([moved]).description
    assert "**Channel**: <#111> → <#222>" in render_text([moved], ALEX)[0]
    logs = entry(LogKind.LOGS_CHANNEL_CHANGED, changes=(Change("Logs channel", "", "<#222>"),))
    assert "**Logs channel**: *nothing* → <#222>" in one([logs]).description
    cleared = entry(LogKind.LOGS_CHANNEL_CHANGED, changes=(Change("Logs channel", "<#222>", ""),))
    assert "**Logs channel**: <#222> → *nothing*" in one([cleared]).description


@pytest.mark.parametrize(
    "name",
    [
        "<@123>",
        "<@&123>",
        "<@!123>",
        "@everyone",
        "@here",
        "see <#123>",  # a channel reference inside other text is text
        "<#123> <#456>",
        "<#123>\n<@456>",
        " <#123>",
        "<#12a>",
        "<#>",
        "[<#123>](https://evil.example)",
    ],
)
def test_a_changed_value_that_is_not_just_a_channel_stays_defused(name: str) -> None:
    renamed = entry(LogKind.FEED_EDITED, changes=(Change("Name", "News", name),))
    for shown in (one([renamed]).description, render_text([renamed], ALEX)[0]):
        line = next(line for line in shown.splitlines() if line.startswith("**Name**"))
        assert not LIVE_MENTION.search(line), line
        assert "@everyone" not in line and "@here" not in line
        assert "](" not in line
        # Nothing is lost: without the escaping it reads as it was typed.
        plain = line.replace("\\", "").replace("\u200b", "")
        assert " ".join(name.split()) in plain


def test_a_name_that_is_exactly_a_channel_reference_is_only_ever_a_channel() -> None:
    renamed = entry(LogKind.FEED_EDITED, changes=(Change("Name", "News", "<#123>"),))
    line = one([renamed]).description
    assert "**Name**: News → <#123>" in line  # a link to a channel: it cannot ping anyone
