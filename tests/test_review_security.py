"""Security review. Tests in the first part FAIL today: each one shows a vulnerability and
should pass once it is fixed. Tests in the second part pass and pin down a guarantee.

Thresholds (seconds, counts, ratios) are generous: they separate "bounded" from "unbounded",
they are not a design for the fix.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import stat
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from fakes_discord import FakeInteraction

from rssbot import logembed
from rssbot.commands import _ui as ui
from rssbot.commands import access as access_commands
from rssbot.commands import feed as feed_commands
from rssbot.commands import filter as filter_commands
from rssbot.commands import setup as setup_commands
from rssbot.commands import template as template_commands
from rssbot.db import Database
from rssbot.deliver import DiscordDeliverer, build_allowed_mentions
from rssbot.fetch import is_public_address
from rssbot.identity import discover
from rssbot.journal import Journal
from rssbot.models import (
    Actor,
    ChannelKind,
    Feed,
    Item,
    ItemStatus,
    OutgoingMessage,
    ParsedFeed,
)
from rssbot.opml import OpmlError, parse_opml
from rssbot.parse import ParseError, parse_feed
from rssbot.ports import DeliveryOutcome, FetchResult, ImageData
from rssbot.render import render_default, render_item
from rssbot.scheduler import STARTED_KEY, Scheduler
from rssbot.service import FeedService, ServiceError

URL = "https://feeds.example/feed.xml"
ALEX = Actor(id=7, name="Alex")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


# -- fakes --


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds)


class OneBody:
    """A fetcher that answers every address with the same body."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        return FetchResult(False, self.body, None, None, url)

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        return ImageData(PNG, "image.png", "image/png")


class Recorder:
    def __init__(self, outcome: DeliveryOutcome = DeliveryOutcome.DELIVERED) -> None:
        self.outcome = outcome
        self.sent: list[OutgoingMessage] = []

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        self.sent.append(message)
        return self.outcome


class Notes:
    def __init__(self) -> None:
        self.notes: list[str] = []

    async def notify(self, server_id: int, text: str) -> None:
        self.notes.append(text)

    async def announce(self, server_id: int, entries: Any, actor: Any) -> None:
        # As the Logs channel shows it: the embeds, and the plain text used without them.
        for embeds in logembed.build_messages(entries, actor):
            self.notes.extend(f"{embed.title}\n{embed.description}" for embed in embeds)
        self.notes.extend(logembed.render_text(entries, actor))


class Channels:
    """Stands in for the discord client's channel cache, which spans every Server."""

    def __init__(self, *channels: Any) -> None:
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        raise AssertionError("every channel in these tests is cached")


class TextChannel(discord.TextChannel):
    def __init__(self, id: int, guild_id: int) -> None:
        self.id = id
        self.guild = SimpleNamespace(id=guild_id)  # type: ignore[assignment]
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:  # type: ignore[override]
        self.sent.append({"content": content, **kwargs})


class ForumChannel(discord.ForumChannel):
    def __init__(self, id: int, guild_id: int) -> None:
        self.id = id
        self.guild = SimpleNamespace(id=guild_id)  # type: ignore[assignment]
        self.posts: list[dict[str, Any]] = []

    @property
    def flags(self) -> Any:
        return SimpleNamespace(require_tag=False)

    @property
    def available_tags(self) -> Any:
        return []

    async def create_thread(self, **kwargs: Any) -> None:  # type: ignore[override]
        self.posts.append(kwargs)


def new_feed(db: Database, *, server_id: int = 1, channel_id: int = 10, **kwargs: Any) -> Feed:
    values: dict[str, Any] = {
        "channel_kind": ChannelKind.MESSAGES,
        "name": "Feed",
        "url": URL,
        "now": 1_700_000_000,
    }
    values.update(kwargs)
    return db.create_feed(server_id=server_id, channel_id=channel_id, **values)


def one_item(key: str = "new") -> Item:
    return Item(
        key=key,
        title="Title",
        link="https://feeds.example/1",
        summary="Summary",
        content="",
        author="",
        published=None,
        categories=(),
        image="",
    )


# =======================================================================================
# Part 1: vulnerabilities. Every test here fails today.
# =======================================================================================


# ISSUE 1: ReDoS in identity._SIZE on a <link sizes="..."> attribute, run on the event loop.
async def test_icon_sizes_attribute_cannot_stall_the_event_loop() -> None:
    page = b"<html><head><link rel='icon' href='/a.png' sizes='" + b"1" * 20_000 + b"'></head>"
    parsed = ParsedFeed(title="Site", link="https://site.example/", image="", items=())

    started = time.perf_counter()
    await discover(parsed, URL, OneBody(page))  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started

    # Quadratic today: about 4.5 s for 20,000 digits, about 50 minutes for a full 512 kB page,
    # all of it with the event loop blocked (no heartbeats, no commands, no Checks).
    assert elapsed < 1.0, f"the event loop was blocked for {elapsed:.1f} s by one attribute"


# ISSUE 2: quadratic parse time in the number of attributes on one element (feedparser's
# strict handler calls xml.sax getValueByQName per attribute), in a thread that cannot be stopped.
def test_many_attributes_do_not_make_parsing_quadratic() -> None:
    attributes = b" ".join(b'a%d="1"' % n for n in range(16_000))
    body = (
        b'<rss version="2.0"><channel><title>t</title><item '
        + attributes
        + b"><guid>1</guid></item></channel></rss>"
    )
    assert len(body) < 200_000  # 1/30 of what the fetcher lets through

    started = time.perf_counter()
    with contextlib.suppress(Exception):  # refusing such a body is a fine fix
        parse_feed(body, URL)
    elapsed = time.perf_counter() - started

    # About 3.7 s today, and four times that for each doubling: a 5 MB body is ~40 minutes.
    assert elapsed < 1.5, f"165 kB of attributes took {elapsed:.1f} s to parse"


def _nested_tags() -> bytes:
    return b"<rss><channel>" + b"<a>" * 20_000


def _entity_expansion() -> bytes:
    # Stays under expat's own limit (100 times the input), which is all that stands in the way.
    half = 100_000
    return (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "' + b"a" * half + b'">]>'
        b'<rss version="2.0"><channel><title>t</title><!--' + b"p" * half + b"-->"
        b"<item><guid>1</guid><description>" + b"&a;" * 190 + b"</description></item>"
        b"</channel></rss>"
    )


# ISSUE 3: a small body makes the parser hold hundreds of times its size: internal XML entities
# are expanded (measured: 5 MB body, 1.85 GB RSS), and unclosed nested tags (5 MB, 705 MB).
@pytest.mark.parametrize("make_body", [_entity_expansion, _nested_tags])
def test_a_feed_body_does_not_multiply_memory(make_body: Any) -> None:
    body = make_body()

    tracemalloc.start()
    try:
        with contextlib.suppress(Exception):  # refusing such a body is a fine fix
            parse_feed(body, URL)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Each of these compresses to a few kB on the wire. Five Checks run at once, and every
    # command that reads a feed (add, test, placeholders, import) starts one more parse.
    ratio = peak / len(body)
    assert ratio < 60, f"parsing held {ratio:.0f} times the size of the body"


# ISSUE 4: no limit on the Items of one listing, so one Check stores a Seen item for each
# (kept 30 days): about 13 MB of database per Check for a 3 MB feed that renames its guids.
async def test_one_check_does_not_store_a_seen_item_for_every_entry_of_a_huge_listing() -> None:
    entries = b"".join(b"<item><guid>g%d</guid></item>" % n for n in range(20_000))
    body = b'<rss version="2.0"><channel><title>t</title>' + entries + b"</channel></rss>"
    db = Database(":memory:")
    clock = Clock()
    feed = new_feed(db, now=clock.t)
    db.record_seen(feed.id, [(STARTED_KEY, ItemStatus.SEEN)], clock.t)  # not its first Check
    scheduler = Scheduler(
        db,
        OneBody(body),  # type: ignore[arg-type]
        Recorder(),
        Journal(db, clock, Notes()),
        clock,
        parse_feed,
        render_item,
        render_default,
    )

    clock.t += 1000
    await scheduler.tick()

    listed = [item.key for item in parse_feed(body, URL).items]
    stored = len(db.seen_states(feed.id, listed))
    assert stored < 10_000, f"one Check of one Feed stored {stored} Seen items"


# ISSUE 5: no limit on the Feeds of one Server (Database.count_feeds exists and is never used).
async def test_a_server_cannot_have_feeds_without_limit() -> None:
    db = Database(":memory:")
    for n in range(5_000):
        new_feed(db, url=f"https://feeds.example/{n}.xml")
    parsed = ParsedFeed(title="t", link="", image="", items=())
    clock = Clock()
    service = FeedService(
        db,
        OneBody(b"x"),  # type: ignore[arg-type]
        clock,
        Journal(db, clock),
        parse=lambda body, url, content_type="": parsed,
    )

    with pytest.raises(ServiceError):
        await service.add_feed(
            1, 10, ChannelKind.MESSAGES, "https://feeds.example/one-more.xml", actor=ALEX
        )


# ISSUE 6: LOG_LEVEL=DEBUG is applied to the root logger, so discord.py logs every request URL:
# webhook URLs carry the webhook token, interaction URLs carry the interaction token.
def test_debug_log_level_does_not_make_discord_py_log_urls_with_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import rssbot.__main__ as entry

    async def no_run(config: Any) -> int:
        return 0

    monkeypatch.setattr(entry, "run", no_run)
    monkeypatch.setenv("DISCORD_TOKEN", "not-a-real-token")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    root.handlers.clear()  # as in a fresh process; basicConfig does nothing otherwise
    try:
        assert entry.main() == 0
        # discord/webhook/async_.py:185 and discord/http.py:660 log the full request URL at DEBUG.
        leaking = [
            name
            for name in ("discord.webhook.async_", "discord.http")
            if logging.getLogger(name).isEnabledFor(logging.DEBUG)
        ]
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    assert not leaking, f"these loggers print URLs with tokens in them: {leaking}"


# ISSUE 7: the database holds every webhook token in plain text and is created world-readable.
def test_the_database_cannot_be_read_by_other_users(tmp_path: Path) -> None:
    previous = os.umask(0o022)  # the default in the image and on most hosts
    try:
        path = tmp_path / "data" / "rssbot.db"
        db = Database(path)
        db.set_webhook(1, 2, "webhook-token")
        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in path.parent.iterdir()}
        directory = stat.S_IMODE(path.parent.stat().st_mode)
        db.close()
    finally:
        os.umask(previous)
    readable = {name: oct(mode) for name, mode in modes.items() if mode & 0o077}
    # Either the files or the directory they are in must be closed to group and others.
    assert not readable or not directory & 0o077, f"readable by others: {readable}"


# ISSUE 8: nothing checks that a Feed's channel is in the Feed's Server. The id comes from a
# form payload and is looked up in the client's cache, which spans every Server the bot is in.
@pytest.mark.parametrize("channel_guild", [1, 2])
async def test_a_feed_is_never_posted_in_a_channel_of_another_server(channel_guild: int) -> None:
    db = Database(":memory:")
    channel = TextChannel(10, guild_id=channel_guild)
    deliverer = DiscordDeliverer(Channels(channel), db, OneBody(b""))  # type: ignore[arg-type]
    feed = new_feed(db, server_id=1, channel_id=channel.id)

    outcome = await deliverer.deliver(feed, OutgoingMessage(content="hello"))

    if channel_guild == feed.server_id:
        assert outcome is DeliveryOutcome.DELIVERED and len(channel.sent) == 1  # the control
    else:
        assert channel.sent == [], "Server 1's Feed was posted in a channel of Server 2"
        assert outcome is not DeliveryOutcome.DELIVERED


# ISSUE 9: the Feed's name goes into Logs channel notes unescaped; the name is the feed
# publisher's title unless a Manager typed one. The bot then vouches for the link.
async def test_a_logs_note_does_not_render_markdown_from_the_feed_name() -> None:
    db = Database(":memory:")
    clock = Clock()
    feed = new_feed(db, name="x** was removed. [Restore it](https://evil.example) **", now=clock.t)
    db.record_seen(feed.id, [(STARTED_KEY, ItemStatus.SEEN)], clock.t)
    notes = Notes()
    parsed = ParsedFeed(title="t", link="", image="", items=(one_item(),))
    scheduler = Scheduler(
        db,
        OneBody(b"x"),  # type: ignore[arg-type]
        Recorder(DeliveryOutcome.LOST_CHANNEL),
        Journal(db, clock, notes),
        clock,
        lambda body, url, content_type="": parsed,
        render_item,
        render_default,
    )

    clock.t += 1000
    await scheduler.tick()

    assert len(notes.notes) == 2  # the embed and its plain text
    for note in notes.notes:
        assert "Feed paused by the bot" in note
        assert not re.search(r"(?<!\\)\[Restore it\]\(https://evil\.example\)", note), note
        assert not re.search(r"(?<!\\)\*\* was removed", note), note


# ISSUE 10: one Cover image download at a time for the whole Instance: a publisher whose
# images answer slowly (up to 30 s each) holds up the Forum posts of every other Server.
async def test_a_slow_cover_image_does_not_hold_up_another_servers_forum_posts() -> None:
    gate = asyncio.Event()

    class Images(OneBody):
        async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
            if "slow" in url:
                await gate.wait()
            return ImageData(PNG, "image.png", "image/png")

    db = Database(":memory:")
    theirs, ours = ForumChannel(20, guild_id=1), ForumChannel(21, guild_id=2)
    deliverer = DiscordDeliverer(Channels(theirs, ours), db, Images(b""))  # type: ignore[arg-type]
    slow_feed = new_feed(db, server_id=1, channel_id=20, channel_kind=ChannelKind.FORUM)
    our_feed = new_feed(db, server_id=2, channel_id=21, channel_kind=ChannelKind.FORUM)

    def post(host: str) -> OutgoingMessage:
        return OutgoingMessage(
            content="hello", thread_title="Title", cover_image_url=f"https://{host}/a.png"
        )

    slow = asyncio.create_task(deliverer.deliver(slow_feed, post("slow.example")))
    try:
        await asyncio.sleep(0.05)
        try:
            outcome = await asyncio.wait_for(deliverer.deliver(our_feed, post("fast.example")), 1.0)
        except TimeoutError:
            pytest.fail("Server 2's Forum post waited for Server 1's slow Cover image")
        assert outcome is DeliveryOutcome.DELIVERED and len(ours.posts) == 1
    finally:
        gate.set()
        await slow


# =======================================================================================
# Part 2: guarantees that hold. Every test here passes.
# =======================================================================================


def _all_commands() -> list[Any]:
    found: list[Any] = [setup_commands.setup_command, filter_commands.filter_command]
    for group in (access_commands.access_group, feed_commands.group, template_commands.group):
        found.extend(group.walk_commands())
    return found


@pytest.mark.parametrize("command", _all_commands(), ids=lambda c: c.qualified_name)
async def test_every_slash_command_refuses_a_member_without_access_first(command: Any) -> None:
    db = Database(":memory:")
    new_feed(db, server_id=100)
    interaction = FakeInteraction(db)  # a plain member of Server 100
    options = [name for name in inspect.signature(command.callback).parameters][1:]

    with pytest.raises(ui.UserError) as refused:
        await command.callback(interaction, **dict.fromkeys(options, "1"))

    assert refused.value.user_message in (ui.NEED_ADMIN, ui.NEED_MANAGER)
    assert interaction.calls == []


def test_every_button_select_and_form_requires_access_except_cancel_and_help() -> None:
    # Only the bot's own controls: other test files register throwaway ones of their own.
    # /help is open to everyone and shows each member only the categories of their Level.
    open_to_all = [
        name
        for name, cls in ui._ACTIONS.items()
        if cls.requires is None and cls.__module__.startswith("rssbot.")
    ]
    open_to_all += [name for name, form in ui._FORMS.items() if form.requires is None]
    assert sorted(open_to_all) == ["cancel", "help_category", "help_page"]


async def test_feed_autocomplete_tells_a_member_without_access_nothing() -> None:
    db = Database(":memory:")
    new_feed(db, server_id=100, name="Secret plans")
    assert await ui.feed_autocomplete(FakeInteraction(db), "") == []  # type: ignore[arg-type]
    other_server = FakeInteraction(db, guild_id=200, administrator=True)
    assert await ui.feed_autocomplete(other_server, "") == []  # type: ignore[arg-type]


def test_feed_content_cannot_ping_everyone_users_or_unchosen_roles() -> None:
    db = Database(":memory:")
    feed = db.update_feed(new_feed(db).id, mention_role_ids=(555,))
    item = Item(
        key="k",
        title="@everyone @here <@1> <@&999>",
        link="https://feeds.example/1",
        summary="@EVERYONE <@!2> <@&998>",
        content="",
        author="",
        published=None,
        categories=(),
        image="",
    )
    for message in (render_item(feed, item), render_default(feed, item)):
        assert "@everyone" not in message.content.lower() and "@here" not in message.content
        allowed = build_allowed_mentions(message.mention_role_ids).to_dict()
        assert allowed["parse"] == [] and allowed["roles"] == [555]
        assert not allowed.get("users") and not allowed.get("replied_user")


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
        "::ffff:169.254.169.254",
        "::ffff:0:127.0.0.1",
        "::127.0.0.1",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::7f00:1",
        "2002:a9fe:a9fe::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "fe80::1%eth0",
        "0.0.0.0",
        "::",
        "127.0.0.1\n",
    ],
)
def test_internal_addresses_in_unusual_forms_are_not_public(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "cp037"])
def test_opml_entity_declarations_are_refused_in_any_encoding(encoding: str) -> None:
    text = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE opml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<opml><body><outline text="&b;" xmlUrl="https://feeds.example/f"/></body></opml>'
    )
    with pytest.raises(OpmlError):
        parse_opml(text.encode(encoding))


def test_external_xml_entities_in_a_feed_are_not_fetched() -> None:
    body = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
        b'<rss version="2.0"><channel><title>T&x;T</title>'
        b"<item><guid>1</guid><title>I&x;I</title></item></channel></rss>"
    )
    # Bodies that declare entities are refused outright, so nothing can be fetched or expanded.
    with pytest.raises(ParseError):
        parse_feed(body, URL)
