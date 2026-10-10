"""Resilience review: tests that FAIL today and would pass once the defect is fixed.

Each failing test names the defect in a one-line comment. The property under review:
nothing that goes wrong with one Item or Feed may stop or indefinitely delay the handling
of any other Item or Feed.

The findings here are in DiscordDeliverer. It is a single instance shared by every Feed
of every Server, and it guards two network operations with process-global asyncio.Locks:

  * self._webhook_lock, held across parent.create_webhook(...)
  * self._cover_lock,    held across the message send that attaches a Cover image

The scheduler is explicitly built to survive a delivery that never returns: it times the
Check out, cancels the work, and -- if the work will not stop -- "leaves it behind" so the
Feed turns into a Broken feed without taking anything else down (see Scheduler._bounded and
Scheduler._leave_behind, and tests test_a_check_that_ignores_its_cancellation_is_left_behind
and test_a_delivery_that_hangs...). But a left-behind delivery that is stuck inside one of
these locks keeps holding it, so EVERY other Feed's delivery that needs the same lock blocks
behind it indefinitely. One Feed's stuck delivery then stalls unrelated Feeds -- the exact
cross-Feed coupling the bot is meant to prevent. "A call that never returns" is named in the
property as something that must be contained.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from rssbot.db import Database
from rssbot.deliver import DiscordDeliverer
from rssbot.journal import Journal
from rssbot.models import Actor, ChannelKind, Feed, Item, OutgoingMessage, ParsedFeed
from rssbot.ports import DeliveryOutcome, FetchResult, ImageData
from rssbot.service import FeedService

CHAN_A, CHAN_B = 100, 101
FORUM_A, FORUM_B = 200, 201


# -- minimal discord fakes (modelled on tests/test_deliver.py) --


class FakeWebhook:
    def __init__(self, id: int, token: str | None, name: str = "") -> None:
        self.id = id
        self.token = token
        self.name = name
        self.sends: list[dict[str, Any]] = []

    async def send(self, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        self.sends.append(kwargs)

    async def delete(self, **kwargs: Any) -> None:
        return None


class FakeText(discord.TextChannel):
    def __init__(self, id: int) -> None:
        self.id = id
        self.sends: list[dict[str, Any]] = []
        self.created: list[FakeWebhook] = []
        self.create_entered: asyncio.Event | None = None  # set -> hang forever in create_webhook

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if content is not None:
            kwargs["content"] = content
        self.sends.append(kwargs)

    async def create_webhook(self, *, name: str, reason: str | None = None) -> FakeWebhook:
        if self.create_entered is not None:
            self.create_entered.set()
            await asyncio.Event().wait()  # a Discord call that never returns
        hook = FakeWebhook(9000 + self.id + len(self.created), f"tok{len(self.created)}", name)
        self.created.append(hook)
        return hook


class FakeForum(discord.ForumChannel):
    def __init__(self, id: int, *, hang: bool = False) -> None:
        self.id = id
        self.posts: list[dict[str, Any]] = []
        self._hang = hang
        self.create_entered: asyncio.Event | None = None

    @property
    def flags(self) -> Any:
        return SimpleNamespace(require_tag=False)

    @property
    def available_tags(self) -> Any:
        return []

    async def create_thread(self, **kwargs: Any) -> None:
        if self._hang:
            if self.create_entered is not None:
                self.create_entered.set()
            await asyncio.Event().wait()  # a Discord call that never returns
        await asyncio.sleep(0)
        self.posts.append(kwargs)


class FakeClient:
    def __init__(self, channels: dict[int, Any]) -> None:
        self._channels = channels

    def get_channel(self, channel_id: int) -> Any:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        if channel_id in self._channels:
            return self._channels[channel_id]
        raise discord.NotFound(SimpleNamespace(status=404, reason="x"), {"code": 10003})


class FakeFetcher:
    async def fetch(self, url: str, **kwargs: Any) -> Any:  # pragma: no cover - unused here
        raise AssertionError("not used")

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        await asyncio.sleep(0)
        return ImageData(b"\x89PNG\r\n\x1a\n" + url.encode(), "cover.png", "image/png")


def _feed(db: Database, channel_id: int, kind: ChannelKind) -> Feed:
    return db.create_feed(
        server_id=1, channel_id=channel_id, channel_kind=kind, name="Feed",
        url=f"https://example.com/{channel_id}", now=0,
    )


async def _cancel(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(BaseException):
            await task


async def test_webhook_lock_stuck_creation_blocks_an_unrelated_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DEFECT: DiscordDeliverer._webhook_lock is a process-global lock held across
    # parent.create_webhook(); a stuck creation for one channel blocks delivery for every
    # other Feed -- even Feeds whose webhook already exists and need no creation at all.
    db = Database(":memory:")
    partials: dict[int, FakeWebhook] = {}

    def partial(id: int, token: str, **kwargs: Any) -> FakeWebhook:
        return partials.setdefault(id, FakeWebhook(id, token))

    monkeypatch.setattr(discord.Webhook, "partial", partial)

    chan_a = FakeText(CHAN_A)
    chan_a.create_entered = asyncio.Event()  # its create_webhook will hang forever
    chan_b = FakeText(CHAN_B)
    client = FakeClient({CHAN_A: chan_a, CHAN_B: chan_b})
    deliverer = DiscordDeliverer(client, db, FakeFetcher(), None)  # type: ignore[arg-type]

    feed_a = _feed(db, CHAN_A, ChannelKind.MESSAGES)
    feed_b = _feed(db, CHAN_B, ChannelKind.MESSAGES)
    db.set_webhook(CHAN_B, 7001, "btok")  # feed B already has a webhook: no creation needed

    msg = OutgoingMessage(content="hi", username="Site")
    task_a = asyncio.create_task(deliverer.deliver(feed_a, msg))
    task_b: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(chan_a.create_entered.wait(), 1.0)  # A now holds _webhook_lock

        task_b = asyncio.create_task(deliverer.deliver(feed_b, msg))
        try:
            outcome_b = await asyncio.wait_for(task_b, 0.5)
        except TimeoutError:
            raise AssertionError(
                "feed B (webhook already stored) was blocked indefinitely by feed A's stuck "
                "create_webhook holding the global _webhook_lock"
            ) from None
        assert outcome_b is DeliveryOutcome.DELIVERED
        assert partials[7001].sends, "feed B should have posted through its own webhook"
    finally:
        await _cancel(*(t for t in (task_a, task_b) if t is not None))
        db.close()


async def test_cover_lock_stuck_send_blocks_an_unrelated_feed() -> None:
    # DEFECT: DiscordDeliverer._cover_lock is a process-global lock held across the message
    # send; a Forum post send that never returns for one Feed blocks the Cover-image delivery
    # of every other Feed indefinitely.
    db = Database(":memory:")

    forum_a = FakeForum(FORUM_A, hang=True)  # its create_thread hangs while holding _cover_lock
    forum_a.create_entered = asyncio.Event()
    forum_b = FakeForum(FORUM_B)
    client = FakeClient({FORUM_A: forum_a, FORUM_B: forum_b})
    deliverer = DiscordDeliverer(client, db, FakeFetcher(), None)  # type: ignore[arg-type]

    feed_a = _feed(db, FORUM_A, ChannelKind.FORUM)
    feed_b = _feed(db, FORUM_B, ChannelKind.FORUM)
    msg = OutgoingMessage(content="body", thread_title="T", cover_image_url="https://e.com/c.png")

    task_a = asyncio.create_task(deliverer.deliver(feed_a, msg))
    task_b: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(forum_a.create_entered.wait(), 1.0)  # A now holds _cover_lock

        task_b = asyncio.create_task(deliverer.deliver(feed_b, msg))
        try:
            outcome_b = await asyncio.wait_for(task_b, 0.5)
        except TimeoutError:
            raise AssertionError(
                "feed B's Cover-image delivery was blocked indefinitely by feed A's stuck send "
                "holding the global _cover_lock"
            ) from None
        assert outcome_b is DeliveryOutcome.DELIVERED
        assert forum_b.posts, "feed B should have created its Forum post"
    finally:
        await _cancel(*(t for t in (task_a, task_b) if t is not None))
        db.close()


async def test_a_failed_baseline_on_a_new_address_leaves_the_old_address() -> None:
    # DEFECT: edit_feed stored the new address and only then recorded the Seen items of its
    # source; if that second write failed, the Feed kept the new address with no starting
    # point and its next Check posted every old Item.
    db = Database(":memory:")
    old, new = "https://a.example/feed", "https://b.example/feed"
    listings = {
        old: ParsedFeed(
            "A", "https://a.example/", "", (Item("a1", "A1", "", "", "", "", None, (), ""),)
        ),
        new: ParsedFeed(
            "B",
            "https://b.example/",
            "",
            tuple(Item(key, key, "", "", "", "", None, (), "") for key in ("b1", "b2")),
        ),
    }

    class Clock:
        def now(self) -> int:
            return 1_700_000_000

    class Web:
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            return FetchResult(False, url.encode(), None, None, url)

    service = FeedService(
        db,
        Web(),  # type: ignore[arg-type]
        Clock(),
        Journal(db, Clock()),
        parse=lambda body, url, content_type="": listings[body.decode()],
    )
    actor = Actor(id=7, name="Alex")
    feed, _ = await service.add_feed(1, CHAN_A, ChannelKind.MESSAGES, old, actor=actor)

    def disk_full(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    real = db.record_seen
    db.record_seen = disk_full  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError):
            await service.edit_feed(1, feed.id, url=new, actor=actor)
    finally:
        db.record_seen = real  # type: ignore[method-assign]

    stored = db.get_feed(feed.id)
    assert stored is not None
    assert stored.url == old
    db.close()
