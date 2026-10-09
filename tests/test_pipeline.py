"""The whole pipeline without Discord: real Check loop, fetcher, parser, renderer and database
against a local feed server, with a Deliverer that records what would be posted."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestServer

from rssbot.db import Database
from rssbot.fetch import HttpFetcher
from rssbot.journal import Journal
from rssbot.models import Actor, ChannelKind, Feed, ItemStatus, OutgoingMessage
from rssbot.parse import parse_feed
from rssbot.ports import DeliveryOutcome
from rssbot.render import render_default, render_item
from rssbot.scheduler import Scheduler
from rssbot.service import FeedService

SERVER = 1
ALEX = Actor(id=7, name="Alex")


def rss(*numbers: int) -> bytes:
    items = "".join(
        f"<item><guid>g{n}</guid><title>Item {n}</title><link>https://example.com/{n}</link>"
        f"<description>&lt;p&gt;Body {n} &lt;img src='https://example.com/{n}.png'&gt;&lt;/p&gt;"
        f"</description></item>"
        for n in numbers
    )
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel><title>Example</title>"
        f"<link>https://example.com/</link>{items}</channel></rss>"
    ).encode()


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds)


class Recorder:
    """Stands in for Discord. Fails for any message whose text contains a word in `fail`."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, OutgoingMessage]] = []
        self.fail: set[str] = set()

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        if any(word in message.content for word in self.fail):
            raise RuntimeError("boom")
        self.sent.append((feed.id, message))
        return DeliveryOutcome.DELIVERED


class Notes:
    async def notify(self, server_id: int, text: str) -> None:
        pass

    async def announce(self, server_id: int, entries: object, actor: object) -> None:
        pass


class Site:
    """A local web server whose paths serve whatever the test sets."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes | int] = {}
        app = web.Application()
        app.router.add_get("/{name}", self._serve)
        self.server = TestServer(app)

    async def _serve(self, request: web.Request) -> web.Response:
        body = self.bodies.get(request.match_info["name"], 404)
        if isinstance(body, int):
            return web.Response(status=body)
        return web.Response(body=body, content_type="application/rss+xml")

    def url(self, name: str) -> str:
        return str(self.server.make_url(f"/{name}"))


async def test_new_items_are_posted_and_a_broken_feed_or_item_stops_nothing() -> None:
    site = Site()
    await site.server.start_server()
    db = Database(":memory:")
    clock = Clock()
    recorder = Recorder()
    fetcher = HttpFetcher(allow_private=True)
    try:
        journal = Journal(db, clock, Notes())
        service = FeedService(db, fetcher, clock, journal)
        scheduler = Scheduler(
            db, fetcher, recorder, journal, clock, parse_feed, render_item, render_default
        )
        site.bodies = {"text": rss(2, 1), "forum": rss(2, 1), "bad": rss(1)}
        text, listed = await service.add_feed(
            SERVER, 10, ChannelKind.MESSAGES, site.url("text"), actor=ALEX
        )
        forum, _ = await service.add_feed(
            SERVER, 20, ChannelKind.FORUM, site.url("forum"), actor=ALEX
        )
        bad, _ = await service.add_feed(
            SERVER, 30, ChannelKind.MESSAGES, site.url("bad"), actor=ALEX
        )
        assert listed == 2

        # Nothing present when a Feed is added is ever posted.
        clock.t += 700
        await scheduler.tick()
        assert recorder.sent == []

        # One source breaks, one Item cannot be delivered; everything else still posts.
        site.bodies = {"text": rss(5, 4, 3, 2, 1), "forum": rss(3, 2, 1), "bad": b"<html>oops"}
        recorder.fail = {"Item 4"}
        clock.t += 700
        await scheduler.tick()

        posted = [(feed_id, m.content) for feed_id, m in recorder.sent if feed_id == text.id]
        assert posted == [
            (text.id, "📰 | **Item 3**\nhttps://example.com/3"),
            (text.id, "📰 | **Item 5**\nhttps://example.com/5"),
        ]
        (forum_post,) = [m for feed_id, m in recorder.sent if feed_id == forum.id]
        assert forum_post.thread_title == "Item 3"
        assert forum_post.cover_image_url == "https://example.com/3.png"

        broken = db.get_feed(bad.id)
        assert broken is not None and broken.fail_count == 1 and broken.paused is None
        states = db.seen_states(text.id, [_key(db, text.id, n) for n in (3, 4, 5)])
        assert sorted(status for status, _ in states.values()) == sorted(
            [ItemStatus.DELIVERED, ItemStatus.PENDING, ItemStatus.DELIVERED]
        )

        # The failed Item is retried on the next Check and posted once it works.
        recorder.fail = set()
        recorder.sent.clear()
        clock.t += 700
        await scheduler.tick()
        assert [m.content for _, m in recorder.sent] == ["📰 | **Item 4**\nhttps://example.com/4"]
    finally:
        await fetcher.close()
        await site.server.close()
        db.close()


def _key(db: Database, feed_id: int, number: int) -> str:
    parsed = parse_feed(rss(number), "")
    return parsed.items[0].key
