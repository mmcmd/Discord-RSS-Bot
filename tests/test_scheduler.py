from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import (
    CATCH_UP_LIMIT,
    MAX_DELIVERY_ATTEMPTS,
    Actor,
    ChannelKind,
    Feed,
    FilterField,
    FilterList,
    Item,
    ItemStatus,
    LogEntry,
    LogKind,
    OutgoingMessage,
    ParsedFeed,
    PauseReason,
)
from rssbot.ports import DeliveryOutcome, FetchError, FetchResult
from rssbot.scheduler import HOLD_OFF_S, LOG_KEEP_S, STARTED_KEY, Scheduler, SystemClock

D = DeliveryOutcome
SEEN, DELIVERED, PENDING, SKIPPED = (
    ItemStatus.SEEN,
    ItemStatus.DELIVERED,
    ItemStatus.PENDING,
    ItemStatus.SKIPPED,
)

START = 1_700_000_000
DAY = 24 * 60 * 60
INTERVAL = 600
SERVER = 9001  # far from any Feed id, so that TrippedDatabase can tell them apart
CHANNEL = 500
HANG = "hang"


# -- fakes --


class FakeClock:
    def __init__(self) -> None:
        self.t = START
        self.sleeps: list[float] = []
        self.sleep_errors: list[BaseException] = []
        self.broken = False

    def now(self) -> int:
        if self.broken:
            raise RuntimeError("no clock")
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.sleep_errors:
            raise self.sleep_errors.pop(0)
        self.t += int(seconds)
        await asyncio.sleep(0)


class Sources:
    """What every address serves right now. Stands in for the fetcher and the parser."""

    def __init__(self) -> None:
        self.listings: dict[str, ParsedFeed] = {}
        self.etags: dict[str, str] = {}
        self.modified: dict[str, str] = {}
        self.fetch_script: dict[str, Any] = {}  # address -> HANG or an exception
        self.parse_script: dict[str, Any] = {}  # address -> an exception, or a callable to run
        self.fetches: list[tuple[str, str | None, str | None]] = []
        self.parses: list[str] = []
        self.active = 0
        self.peak = 0
        self.delay = 0.0
        self.gate: asyncio.Event | None = None
        self.during_fetch: Callable[[str], None] | None = None
        self.pages: dict[str, str] = {}  # article address -> its HTML

    def serve(self, url: str, *items: Item, title: str = "Site", link: str = "") -> None:
        self.listings[url] = ParsedFeed(title=title, link=link, image="", items=tuple(items))

    def fetched(self, url: str) -> int:
        return sum(1 for fetched, _, _ in self.fetches if fetched == url)

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.fetches.append((url, etag, last_modified))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.during_fetch is not None:
                self.during_fetch(url)
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            step = self.fetch_script.get(url)
            if step == HANG:
                await asyncio.Event().wait()
            if isinstance(step, BaseException):
                raise step
            if url in self.pages:
                return FetchResult(False, self.pages[url].encode(), None, None, url)
            current = self.etags.get(url)
            if current is not None and current == etag:
                return FetchResult(True, b"", current, self.modified.get(url), url)
            return FetchResult(False, url.encode(), current, self.modified.get(url), url)
        finally:
            self.active -= 1

    async def fetch_image(self, url: str, *, max_bytes: int = 0):  # pragma: no cover
        raise AssertionError("not used")

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        assert body == url.encode()
        self.parses.append(url)
        step = self.parse_script.get(url)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step()
        return self.listings[url]


class FakeDeliverer:
    """Delivers everything, unless the script says otherwise for a message's text.

    A script entry is what every attempt gets, or a list with one entry per attempt:
    an outcome, HANG, an exception to raise, or a coroutine function to run.
    """

    def __init__(self) -> None:
        self.script: dict[str, Any] = {}
        self.attempts: list[str] = []
        self.posted: list[str] = []

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        text = message.content
        self.attempts.append(text)
        step = self.script.get(text, D.DELIVERED)
        if isinstance(step, list):
            step = step.pop(0) if step else D.DELIVERED
        if step == HANG:
            await asyncio.Event().wait()
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = await step()
        if step is D.DELIVERED:
            self.posted.append(text)
        return step


class Renderer:
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.broken: set[str] = set()  # the keys of Items it cannot render
        self.rendered: list[Item] = []

    def __call__(self, feed: Feed, item: Item) -> OutgoingMessage:
        if item.key in self.broken:
            raise ValueError("cannot render this")
        self.rendered.append(item)
        return OutgoingMessage(content=self.prefix + item.key)


class FakeNotifier:
    def __init__(self) -> None:
        self.reports: list[tuple[int, LogEntry, Actor]] = []  # each of one Log entry
        self.error: BaseException | None = None
        self.hang = False

    async def notify(self, server_id: int, text: str) -> None:
        raise AssertionError("the Check loop reports through Log entries")

    async def announce(self, server_id: int, entries: Any, actor: Actor) -> None:
        [entry] = entries
        self.reports.append((server_id, entry, actor))
        if self.hang:
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error


class TrippedDatabase:
    """A Database in which chosen calls raise, or are followed by something else happening."""

    def __init__(self, inner: Database) -> None:
        self.inner = inner
        self.broken_calls: set[str] = set()
        self.broken_feeds: set[int] = set()
        self.fail_if: Callable[[str, tuple[Any, ...]], bool] | None = None
        self.after: dict[str, Callable[[], None]] = {}
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            if (
                name in self.broken_calls
                or (args and args[0] in self.broken_feeds)
                or (self.fail_if is not None and self.fail_if(name, args))
            ):
                raise sqlite3.OperationalError("disk I/O error")
            result = method(*args, **kwargs)
            hook = self.after.pop(name, None)
            if hook is not None:
                hook()
            return result

        return call


class Bench:
    def __init__(self, *, tripped: bool = False, **tunables: Any) -> None:
        self.db = Database(":memory:")
        self.store: Any = TrippedDatabase(self.db) if tripped else self.db
        self.clock = FakeClock()
        self.sources = Sources()
        self.deliverer = FakeDeliverer()
        self.notifier = FakeNotifier()
        self.render = Renderer()
        self.render_default = Renderer("plain:")
        tunables.setdefault("rand", lambda: 0.0)
        tunables.setdefault("check_timeout_s", 5.0)
        tunables.setdefault("item_allowance_s", 0.0)
        tunables.setdefault("grace_s", 0.05)
        tunables.setdefault("restart_delay_s", 0.0)
        self.journal = Journal(
            self.store,
            self.clock,
            self.notifier,
            announce_timeout_s=tunables.pop("announce_timeout_s", 10.0),
        )
        self.scheduler = Scheduler(
            self.store,
            self.sources,
            self.deliverer,
            self.journal,
            self.clock,
            parse=self.sources.parse,
            render=self.render,
            render_default=self.render_default,
            **tunables,
        )

    def add(self, name: str, *items: Item, interval_s: int = INTERVAL) -> Feed:
        """A new Feed whose source lists these Items, newest first."""
        url = f"https://{name}.example/rss"
        self.sources.serve(url, *items)
        return self.db.create_feed(
            server_id=SERVER,
            channel_id=CHANNEL,
            channel_kind=ChannelKind.MESSAGES,
            name=name,
            url=url,
            now=self.clock.t,
            interval_s=interval_s,
        )

    async def started(self, name: str, *items: Item, interval_s: int = INTERVAL) -> Feed:
        """A Feed that has had its first Check and is due again."""
        feed = self.add(name, *items, interval_s=interval_s)
        await self.scheduler.check_feed(feed.id)
        assert self.deliverer.attempts == []
        self.wait(feed)
        return feed

    def publish(self, feed: Feed, *items: Item) -> None:
        """Put more Items at the top of the Feed's source."""
        old = self.sources.listings[feed.url]
        self.sources.listings[feed.url] = dataclasses.replace(old, items=items + old.items)

    def wait(self, *feeds: Feed) -> None:
        """Let time pass until these Feeds are due."""
        self.clock.t = max(self.clock.t, *(self.feed(feed).next_check_at for feed in feeds))

    def feed(self, feed: Feed) -> Feed:
        stored = self.db.get_feed(feed.id)
        assert stored is not None
        return stored

    def state(self, feed: Feed, key: str) -> tuple[ItemStatus, int] | None:
        return self.db.seen_states(feed.id, [key]).get(key)

    def status(self, feed: Feed, key: str) -> ItemStatus | None:
        state = self.state(feed, key)
        return state[0] if state else None

    def logged(self) -> list[LogEntry]:
        """The Server's Log entries, oldest first."""
        return self.db.list_log_entries(SERVER, limit=1000)[::-1]

    def seen_rows(self, feed: Feed) -> list[tuple[Any, ...]]:
        rows = self.db._conn.execute(
            "SELECT * FROM seen_items WHERE feed_id = ? ORDER BY key", (feed.id,)
        )
        return [tuple(row) for row in rows]


@pytest.fixture
def make() -> Iterator[Callable[..., Bench]]:
    benches: list[Bench] = []

    def build(**tunables: Any) -> Bench:
        benches.append(Bench(**tunables))
        return benches[-1]

    yield build
    for bench in benches:
        bench.db.close()


def item(key: str, published: int | None = None, **fields: Any) -> Item:
    values: dict[str, Any] = {
        "key": key,
        "title": key,
        "link": f"https://site.example/{key}",
        "summary": "",
        "content": "",
        "author": "",
        "published": published,
        "categories": (),
        "image": "",
    }
    values.update(fields)
    return Item(**values)


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "waited too long"
        await asyncio.sleep(0.001)


# -- the first Check and new Items --


async def test_first_check_records_what_is_listed_and_posts_nothing(make):
    bench = make()
    feed = bench.add("news", item("a"), item("b"), item("c"))

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == []
    assert [bench.state(feed, key) for key in "abc"] == [(SEEN, 0)] * 3
    stored = bench.feed(feed)
    assert stored.last_success_at == START
    assert stored.next_check_at == START + INTERVAL
    assert (stored.fail_count, stored.failing_since, stored.last_error) == (0, None, "")


async def test_check_feed_works_on_its_own_and_ignores_an_unknown_feed(make):
    bench = make()
    feed = bench.add("news", item("a"))

    await bench.scheduler.check_feed(feed.id)
    await bench.scheduler.check_feed(feed.id + 100)

    assert bench.status(feed, "a") is SEEN
    assert bench.sources.fetched(feed.url) == 1


async def test_new_items_are_posted_oldest_first_and_only_once(make):
    bench = make()
    feed = await bench.started("news", item("old", 100))
    bench.publish(feed, item("n2", 300), item("n3", 400), item("n1", 200))

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["n1", "n2", "n3"]
    assert [bench.state(feed, key) for key in ("n1", "n2", "n3")] == [(DELIVERED, 1)] * 3
    assert bench.status(feed, "old") is SEEN

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["n1", "n2", "n3"]


async def test_without_dates_the_order_of_the_source_decides(make):
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("n3"), item("n2"), item("n1"))  # newest first, as sources do

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["n1", "n2", "n3"]


async def test_an_item_without_a_date_keeps_its_place_among_its_neighbours(make):
    bench = make()
    feed = await bench.started("news", item("old", 100))
    bench.publish(feed, item("n4"), item("n3", 300), item("n2"), item("n1", 200))

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["n1", "n2", "n3", "n4"]


async def test_a_key_listed_twice_is_one_item(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"), item("a"))

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["a"]


async def test_the_first_item_of_a_source_that_listed_nothing_is_posted(make):
    bench = make()
    feed = bench.add("releases")  # nothing there yet
    await bench.scheduler.tick()
    bench.publish(feed, item("first"))
    bench.wait(feed)

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["first"]


async def test_a_feed_that_already_has_seen_items_is_not_started_a_second_time(make):
    bench = make()
    feed = bench.add("news", item("b"), item("a"))
    bench.db.record_seen(feed.id, [("a", SEEN)], START)  # as recorded when the Feed was added

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["b"]
    assert bench.status(feed, STARTED_KEY) is SEEN


async def test_a_feed_whose_seen_items_were_all_removed_starts_over(make):
    bench = make()
    feed = await bench.started("news", item("a"))
    bench.db.prune_seen(feed.id, START + 10 * DAY)
    bench.publish(feed, item("b"))

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == []
    assert bench.status(feed, "a") is SEEN and bench.status(feed, "b") is SEEN


# -- Filters and Catch-up --


async def test_items_that_fail_the_filters_are_recorded_and_not_posted(make):
    bench = make()
    feed = await bench.started("news")
    bench.db.add_filter(feed.id, FilterList.BLOCK, FilterField.TITLE, "sponsored")
    bench.db.add_filter(feed.id, FilterList.MUST_HAVE, FilterField.ANY, "python")
    bench.publish(
        feed,
        item("ad", title="Sponsored: Python course"),
        item("other", title="Rust news"),
        item("good", title="Python news"),
    )

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["good"]
    assert bench.state(feed, "ad") == (SEEN, 0)
    assert bench.state(feed, "other") == (SEEN, 0)
    assert bench.status(feed, "good") is DELIVERED


async def test_catch_up_posts_the_newest_and_skips_the_rest(make):
    bench = make()
    feed = await bench.started("news", item("old", 1))
    new = [item(f"n{n:02}", 1000 + n) for n in range(25)]
    bench.publish(feed, *new[5:], *new[:5])  # in no useful order: the dates decide

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == [f"n{n:02}" for n in range(25 - CATCH_UP_LIMIT, 25)]
    assert len(bench.deliverer.posted) == CATCH_UP_LIMIT
    skipped = [key for key in (i.key for i in new) if bench.status(feed, key) is SKIPPED]
    assert skipped == [f"n{n:02}" for n in range(25 - CATCH_UP_LIMIT)]

    bench.wait(feed)
    await bench.scheduler.tick()
    assert len(bench.deliverer.attempts) == CATCH_UP_LIMIT  # skipped for good


async def test_catch_up_without_dates_keeps_the_top_of_the_source(make):
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, *(item(f"n{n:02}") for n in range(24, -1, -1)))  # n24 is the newest

    await bench.scheduler.tick()

    assert bench.deliverer.posted == [f"n{n:02}" for n in range(15, 25)]
    assert bench.status(feed, "n14") is SKIPPED


async def test_retries_come_on_top_of_catch_up(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("r", 1))
    bench.deliverer.script["r"] = [D.RETRY]
    await bench.scheduler.tick()
    new = [item(f"n{n:02}", 100 + n) for n in range(CATCH_UP_LIMIT + 2)]
    bench.publish(feed, *reversed(new))
    bench.wait(feed)

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["r", *(i.key for i in new[2:])]
    assert [bench.status(feed, i.key) for i in new[:2]] == [SKIPPED, SKIPPED]


async def test_filtered_items_do_not_use_up_catch_up(make):
    bench = make()
    feed = await bench.started("news")
    bench.db.add_filter(feed.id, FilterList.BLOCK, FilterField.TITLE, "noise")
    wanted = [item(f"w{n}", 100 + n) for n in range(CATCH_UP_LIMIT)]
    noise = [item(f"x{n}", 200 + n, title="noise") for n in range(5)]
    bench.publish(feed, *noise, *wanted)

    await bench.scheduler.tick()

    assert bench.deliverer.posted == [i.key for i in wanted]
    assert all(bench.status(feed, i.key) is SEEN for i in noise)


# -- deliveries that fail --


async def test_a_delivery_that_keeps_failing_is_retried_and_then_skipped(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = D.RETRY

    for attempt in range(1, MAX_DELIVERY_ATTEMPTS):
        bench.wait(feed)
        await bench.scheduler.tick()
        assert bench.state(feed, "a") == (PENDING, attempt)
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.state(feed, "a") == (SKIPPED, MAX_DELIVERY_ATTEMPTS)

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.attempts == ["a"] * MAX_DELIVERY_ATTEMPTS
    assert bench.feed(feed).fail_count == 0  # an Item that will not post is not a failed Check


async def test_items_given_up_on_are_counted_until_the_feed_posts_again(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    for key in "abc":
        bench.deliverer.script[key] = D.RETRY

    for _ in range(MAX_DELIVERY_ATTEMPTS - 1):
        bench.wait(feed)
        await bench.scheduler.tick()
    assert (bench.feed(feed).skipped_count, bench.feed(feed).skipped_since) == (0, None)
    bench.wait(feed)
    await bench.scheduler.tick()
    gave_up_at = bench.clock.t
    assert (bench.feed(feed).skipped_count, bench.feed(feed).skipped_since) == (3, gave_up_at)

    # A Check with nothing to post changes nothing, and neither does one more Item lost.
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.feed(feed).skipped_count == 3
    bench.publish(feed, item("e", 5), item("d", 4))
    bench.deliverer.script["d"] = D.REJECTED
    bench.deliverer.script["plain:d"] = D.REJECTED
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["e"]
    assert (bench.feed(feed).skipped_count, bench.feed(feed).skipped_since) == (4, gave_up_at)

    bench.publish(feed, item("f", 6))
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["e", "f"]
    assert (bench.feed(feed).skipped_count, bench.feed(feed).skipped_since) == (0, None)


async def test_items_beyond_catch_up_are_not_counted_as_given_up_on(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, *(item(f"k{n:02}", n) for n in range(CATCH_UP_LIMIT + 5)))

    await bench.scheduler.tick()
    assert len(bench.deliverer.posted) == CATCH_UP_LIMIT
    assert bench.feed(feed).skipped_count == 0


async def test_a_retried_item_does_not_hold_up_the_ones_after_it(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    bench.deliverer.script["a"] = [D.RETRY]

    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["b", "c"]
    assert bench.state(feed, "a") == (PENDING, 1)

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["b", "c", "a"]
    assert bench.status(feed, "a") is DELIVERED


async def test_a_retry_is_posted_in_its_place_among_newer_items(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a", 1))
    bench.deliverer.script["a"] = [D.RETRY]
    await bench.scheduler.tick()
    bench.publish(feed, item("c", 3), item("b", 2))
    bench.wait(feed)

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["a", "b", "c"]


async def test_an_item_left_to_retry_makes_the_next_check_read_the_source_in_full(make):
    bench = make()
    feed = bench.add("news")
    bench.sources.etags[feed.url] = '"v1"'
    await bench.scheduler.tick()
    assert bench.feed(feed).etag == '"v1"'

    bench.publish(feed, item("a"))
    bench.sources.etags[feed.url] = '"v2"'
    bench.deliverer.script["a"] = [D.RETRY]
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.feed(feed).etag is None  # with "v2" stored the source would say: not modified

    bench.wait(feed)
    await bench.scheduler.tick()
    feed_fetches = [fetch for fetch in bench.sources.fetches if fetch[0] == feed.url]
    assert feed_fetches[-1] == (feed.url, None, None)
    assert bench.deliverer.posted == ["a"]
    assert bench.feed(feed).etag == '"v2"'


async def test_a_rejected_message_is_sent_again_in_the_default_rendering(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("b", 2), item("a", 1))
    bench.deliverer.script["a"] = D.REJECTED

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["a", "plain:a", "b"]
    assert bench.deliverer.posted == ["plain:a", "b"]
    assert bench.status(feed, "a") is DELIVERED


@pytest.mark.parametrize("second", [D.REJECTED, D.RETRY, RuntimeError("boom")], ids=str)
async def test_an_item_refused_in_both_renderings_is_skipped_alone(make, second):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("b", 2), item("a", 1))
    bench.deliverer.script["a"] = D.REJECTED
    bench.deliverer.script["plain:a"] = second

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["a", "plain:a", "b"]
    assert bench.deliverer.posted == ["b"]
    assert bench.status(feed, "a") is SKIPPED
    assert bench.feed(feed).fail_count == 0

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.attempts == ["a", "plain:a", "b"]  # not tried again


async def test_a_render_that_raises_falls_back_to_the_default_rendering(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("b", 2), item("a", 1))
    bench.render.broken.add("a")

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["plain:a", "b"]


async def test_the_default_rendering_gets_its_three_tries_when_the_template_raised(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.render.broken.add("a")
    bench.deliverer.script["plain:a"] = [D.RETRY, D.REJECTED]

    await bench.scheduler.tick()
    assert bench.state(feed, "a") == (PENDING, 1)

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.status(feed, "a") is SKIPPED  # refused, and there is nothing plainer to send
    assert bench.deliverer.attempts == ["plain:a", "plain:a"]


async def test_an_item_that_cannot_be_rendered_at_all_is_skipped_alone(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    bench.render.broken.add("b")
    bench.render_default.broken.add("b")

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["a", "c"]
    assert bench.deliverer.posted == ["a", "c"]
    assert bench.status(feed, "b") is SKIPPED


async def test_a_deliverer_that_raises_on_one_item_still_gets_the_rest_delivered(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, *(item(f"k{n}", n) for n in range(5, 0, -1)))
    bench.deliverer.script["k3"] = [RuntimeError("boom")]

    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["k1", "k2", "k4", "k5"]
    assert bench.state(feed, "k3") == (PENDING, 1)  # counted like a temporary failure
    assert bench.feed(feed).fail_count == 0

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["k1", "k2", "k4", "k5", "k3"]


async def test_a_deliverer_that_answers_nonsense_counts_as_a_temporary_failure(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = [None]

    await bench.scheduler.tick()

    assert bench.state(feed, "a") == (PENDING, 1)


# -- Paused feeds --


@pytest.mark.parametrize(
    ("outcome", "reason", "words"),
    [
        (D.LOST_CHANNEL, PauseReason.LOST_CHANNEL, "can no longer post"),
        (D.NEEDS_TAG, PauseReason.NEEDS_TAG, "tag"),
    ],
)
async def test_a_feed_that_cannot_post_is_paused_until_it_is_resumed(make, outcome, reason, words):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    bench.deliverer.script["b"] = outcome

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["a", "b"]  # the Check stopped there
    stored = bench.feed(feed)
    assert stored.paused is reason
    assert (stored.fail_count, stored.last_error) == (0, "")  # paused, not broken
    assert bench.status(feed, "a") is DELIVERED
    assert bench.status(feed, "c") is None
    [(server_id, entry, actor)] = bench.notifier.reports
    assert (server_id, actor) == (SERVER, Actor.bot())
    assert bench.logged() == [entry]  # saved, with the bot as the one who did it
    assert (entry.kind, entry.actor_id, entry.actor_name) == (LogKind.FEED_AUTO_PAUSED, None, "")
    assert (entry.feed_id, entry.feed_name, entry.channel_id) == (feed.id, "news", CHANNEL)
    assert (entry.feed_url, entry.at) == (feed.url, bench.clock.t)
    assert words in entry.detail and "resume" in entry.detail

    # Paused: not checked, and not reported again.
    fetches = len(bench.sources.fetches)
    bench.clock.t += DAY
    await bench.scheduler.tick()
    await bench.scheduler.check_feed(feed.id)
    assert len(bench.sources.fetches) == fetches
    assert len(bench.notifier.reports) == 1

    # Resumed: it is due at once, and what was not delivered is posted.
    del bench.deliverer.script["b"]
    bench.db.update_feed(feed.id, paused=None)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a", "b", "c"]
    assert len(bench.notifier.reports) == 1


async def test_the_default_rendering_finding_the_channel_gone_pauses_too(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = D.REJECTED
    bench.deliverer.script["plain:a"] = D.LOST_CHANNEL

    await bench.scheduler.tick()

    assert bench.feed(feed).paused is PauseReason.LOST_CHANNEL
    assert bench.status(feed, "a") is PENDING  # still there to post after the Feed is resumed


@pytest.mark.parametrize("fault", ["raises", "hangs", "is not async"])
async def test_a_notifier_that_misbehaves_breaks_nothing(make, fault):
    bench = make(announce_timeout_s=0.05)
    feed = await bench.started("news")
    other = await bench.started("other")
    bench.publish(feed, item("a"))
    bench.publish(other, item("o"))
    bench.deliverer.script["a"] = D.LOST_CHANNEL
    if fault == "raises":
        bench.notifier.error = RuntimeError("no such channel")
    elif fault == "hangs":
        bench.notifier.hang = True
    else:
        bench.notifier.announce = lambda server_id, entries, actor: None

    await asyncio.wait_for(bench.scheduler.tick(), 2)

    assert bench.feed(feed).paused is PauseReason.LOST_CHANNEL
    assert bench.deliverer.posted == ["o"]
    assert [entry.kind for entry in bench.logged()] == [
        LogKind.FEED_AUTO_PAUSED
    ]  # saved all the same


async def test_a_report_whose_log_entry_cannot_be_saved_breaks_nothing(make, caplog):
    bench = make(tripped=True)
    feed = await bench.started("news")
    other = await bench.started("other")
    bench.publish(feed, item("a"))
    bench.publish(other, item("o"))
    bench.deliverer.script["a"] = D.LOST_CHANNEL
    bench.store.broken_calls.add("add_log_entry")

    await bench.scheduler.tick()

    assert bench.feed(feed).paused is PauseReason.LOST_CHANNEL  # the Check's state is stored
    assert bench.feed(feed).next_check_at <= bench.clock.t  # not held back as a broken Check
    assert bench.deliverer.posted == ["o"]
    assert (bench.logged(), bench.notifier.reports) == ([], [])
    assert "could not save the Log entry" in caplog.text


# -- failed Checks --


def fetch_refused(bench: Bench, feed: Feed) -> None:
    bench.sources.fetch_script[feed.url] = FetchError("The site answered with error 500.")


def fetch_raises(bench: Bench, feed: Feed) -> None:
    bench.sources.fetch_script[feed.url] = RuntimeError("boom")


def fetch_hangs(bench: Bench, feed: Feed) -> None:
    bench.sources.fetch_script[feed.url] = HANG


def parse_raises(bench: Bench, feed: Feed) -> None:
    bench.sources.parse_script[feed.url] = ValueError("That address did not return a feed.")


FAILURES = {
    "fetch error": (fetch_refused, "The site answered with error 500."),
    "fetch that raises anything": (fetch_raises, "Unexpected error (RuntimeError)"),
    "fetch that hangs": (fetch_hangs, "took longer than 0.3 seconds"),
    "parse error": (parse_raises, "That address did not return a feed."),
}


@pytest.mark.parametrize("failure", FAILURES)
async def test_a_failed_check_is_counted_and_touches_nothing_else(make, failure):
    bench = make(check_timeout_s=0.3)
    broken = bench.add("broken", item("old"))
    healthy = bench.add("healthy", item("h-old"))
    bench.sources.etags[broken.url] = '"v1"'
    await bench.scheduler.tick()
    bench.publish(broken, item("new"))
    bench.publish(healthy, item("h-new"))
    bench.sources.etags[broken.url] = '"v2"'
    bench.wait(broken, healthy)
    before = bench.seen_rows(broken)
    sabotage, words = FAILURES[failure]
    sabotage(bench, broken)

    await bench.scheduler.tick()

    stored = bench.feed(broken)
    assert stored.fail_count == 1
    assert stored.failing_since == bench.clock.t
    assert words in stored.last_error
    assert stored.next_check_at == bench.clock.t + INTERVAL
    assert stored.last_success_at == START
    assert stored.etag == '"v1"'
    assert stored.paused is None and not stored.warned
    assert bench.seen_rows(broken) == before
    # The other Feed in the same tick never noticed.
    assert bench.deliverer.posted == ["h-new"]
    assert bench.feed(healthy).fail_count == 0

    # When the source works again, nothing was lost.
    bench.sources.fetch_script.clear()
    bench.sources.parse_script.clear()
    bench.wait(broken)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["h-new", "new"]
    stored = bench.feed(broken)
    assert (stored.fail_count, stored.failing_since, stored.last_error) == (0, None, "")


async def test_a_delivery_that_hangs_fails_the_check_and_the_item_is_skipped(make):
    bench = make(check_timeout_s=0.2)
    feed = await bench.started("news", item("old"))
    other = await bench.started("other")
    bench.publish(feed, item("b", 2), item("a", 1))
    bench.publish(other, item("o"))
    bench.deliverer.script["a"] = HANG

    await bench.scheduler.tick()

    stored = bench.feed(feed)
    assert stored.fail_count == 1
    assert "took longer" in stored.last_error
    assert stored.next_check_at == bench.clock.t + INTERVAL
    # Nothing says whether Discord took it, so it is not sent again (docs/adr/0003).
    assert bench.state(feed, "a") == (SKIPPED, 1)
    assert stored.skipped_count == 1
    assert bench.state(feed, "b") is None
    assert bench.state(feed, "old") == (SEEN, 0)
    assert bench.deliverer.posted == ["o"]

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.attempts.count("a") == 1
    assert bench.deliverer.posted == ["o", "b"]
    assert bench.feed(feed).fail_count == 0


async def test_the_time_limit_keeps_what_was_already_delivered(make):
    bench = make(check_timeout_s=0.2)
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    bench.deliverer.script["b"] = [HANG]

    await bench.scheduler.tick()
    assert bench.status(feed, "a") is DELIVERED
    assert bench.feed(feed).fail_count == 1
    assert bench.state(feed, "b") == (SKIPPED, 1)

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a", "c"]
    assert bench.deliverer.attempts.count("b") == 1


async def test_an_item_left_as_being_sent_by_a_crash_is_skipped_not_sent_again(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("b", 2), item("a", 1))
    # What a crash in the middle of a delivery leaves behind.
    bench.db.record_seen(feed.id, [("a", ItemStatus.SENDING)], bench.clock.t)
    bench.db.bump_attempts(feed.id, "a")

    await bench.scheduler.tick()
    assert bench.deliverer.attempts == ["b"]
    assert bench.state(feed, "a") == (SKIPPED, 1)
    assert bench.feed(feed).skipped_count == 1  # "b" was posted, but "a" was lost in this Check


async def slow_delivery() -> DeliveryOutcome:
    await asyncio.sleep(0.25)
    return D.DELIVERED


async def test_a_check_short_of_time_starts_no_more_items_and_is_not_a_failed_check(make):
    bench = make(check_timeout_s=1.0, item_allowance_s=0.6)
    feed = await bench.started("news")
    keys = [f"n{n}" for n in range(6)]
    bench.publish(feed, *(item(key, n) for n, key in reversed(list(enumerate(keys)))))
    bench.sources.etags[feed.url] = '"v2"'
    for key in keys:
        bench.deliverer.script[key] = slow_delivery

    await bench.scheduler.tick()

    posted = list(bench.deliverer.posted)
    assert 1 <= len(posted) < len(keys)
    assert posted == keys[: len(posted)]  # the oldest, in order
    assert bench.deliverer.attempts == posted  # nothing was started and then cut off
    stored = bench.feed(feed)
    assert (stored.fail_count, stored.failing_since, stored.last_error) == (0, None, "")
    assert stored.next_check_at == bench.clock.t + INTERVAL
    assert stored.last_success_at == bench.clock.t
    # The Items it did not get to are as new as before: not Skipped, no attempt used.
    assert [bench.state(feed, key) for key in keys[len(posted) :]] == [None] * (6 - len(posted))

    bench.deliverer.script.clear()
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == keys  # the rest, and nothing twice
    assert [bench.state(feed, key) for key in keys] == [(DELIVERED, 1)] * 6
    assert bench.feed(feed).etag == '"v2"'


async def test_catch_up_still_skips_when_the_check_runs_short_of_time(make):
    bench = make(check_timeout_s=1.0, item_allowance_s=0.6)
    feed = await bench.started("news")
    keys = [f"n{n:02}" for n in range(CATCH_UP_LIMIT + 2)]
    bench.publish(feed, *(item(key, n) for n, key in reversed(list(enumerate(keys)))))
    for key in keys:
        bench.deliverer.script[key] = slow_delivery

    await bench.scheduler.tick()

    posted = list(bench.deliverer.posted)
    assert 1 <= len(posted) < CATCH_UP_LIMIT
    assert posted == keys[2 : 2 + len(posted)]
    assert [bench.status(feed, key) for key in keys[:2]] == [SKIPPED, SKIPPED]
    assert all(bench.state(feed, key) is None for key in keys[2 + len(posted) :])

    bench.deliverer.script.clear()
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == keys[2:]
    assert [bench.status(feed, key) for key in keys[:2]] == [SKIPPED, SKIPPED]


async def test_failed_checks_back_off_by_doubling_up_to_an_hour(make):
    bench = make()
    feed = await bench.started("news")
    fetch_refused(bench, feed)

    waits = []
    for count in range(1, 7):
        bench.wait(feed)
        await bench.scheduler.tick()
        stored = bench.feed(feed)
        assert stored.fail_count == count
        waits.append(stored.next_check_at - bench.clock.t)

    assert waits == [600, 1200, 2400, 3600, 3600, 3600]
    assert bench.feed(feed).failing_since == START + INTERVAL


async def test_a_source_that_asks_for_fewer_requests_has_not_failed(make):
    bench = make()
    feed = await bench.started("news")
    bench.sources.fetch_script[feed.url] = FetchError(
        "The site asked the bot to slow down (error 429).", retry_after=90.0, slow_down=True
    )

    for _ in range(3):
        bench.wait(feed)
        await bench.scheduler.tick()
        stored = bench.feed(feed)
        assert (stored.fail_count, stored.failing_since, stored.last_error) == (0, None, "")
        assert stored.next_check_at == bench.clock.t + 90


async def test_a_rate_limited_feed_is_marked_until_the_source_answers_otherwise(make):
    bench = make()
    feed = await bench.started("news")
    worked_at = bench.feed(feed).last_success_at
    bench.sources.fetch_script[feed.url] = FetchError("slow down", slow_down=True)

    bench.wait(feed)
    await bench.scheduler.tick()
    began = bench.clock.t
    stored = bench.feed(feed)
    assert (stored.rate_limited_since, stored.last_checked_at) == (began, began)
    assert stored.last_success_at == worked_at

    bench.wait(feed)
    await bench.scheduler.tick()
    stored = bench.feed(feed)
    assert (stored.rate_limited_since, stored.last_checked_at) == (began, bench.clock.t)

    del bench.sources.fetch_script[feed.url]
    bench.wait(feed)
    await bench.scheduler.tick()
    stored = bench.feed(feed)
    assert stored.rate_limited_since is None
    assert stored.last_checked_at == stored.last_success_at == bench.clock.t


async def test_a_failed_check_is_a_check_and_ends_the_rate_limit(make):
    bench = make()
    feed = await bench.started("news")
    bench.sources.fetch_script[feed.url] = FetchError("slow down", slow_down=True)
    bench.wait(feed)
    await bench.scheduler.tick()
    fetch_refused(bench, feed)

    bench.wait(feed)
    await bench.scheduler.tick()

    stored = bench.feed(feed)
    assert (stored.fail_count, stored.rate_limited_since) == (1, None)
    assert stored.last_checked_at == bench.clock.t


@pytest.mark.parametrize(
    ("asked", "waited"),
    [(None, INTERVAL), (0.0, INTERVAL), (5.0, 60), (21600.0, 21600)],
)
async def test_a_slowed_feed_waits_as_long_as_it_was_asked_to(make, asked, waited):
    bench = make()
    feed = await bench.started("news")
    bench.sources.fetch_script[feed.url] = FetchError(
        "The site asked the bot to slow down (error 429).", retry_after=asked, slow_down=True
    )

    bench.wait(feed)
    await bench.scheduler.tick()

    assert bench.feed(feed).next_check_at == bench.clock.t + waited


async def test_slowing_down_keeps_an_earlier_failure_on_record(make):
    bench = make()
    feed = await bench.started("news")
    fetch_refused(bench, feed)
    bench.wait(feed)
    await bench.scheduler.tick()
    bench.sources.fetch_script[feed.url] = FetchError("slow down", slow_down=True)

    bench.wait(feed)
    await bench.scheduler.tick()

    stored = bench.feed(feed)
    assert (stored.fail_count, stored.last_error) == (1, "The site answered with error 500.")


def asks_to_wait(seconds: Any) -> FetchError:
    error = FetchError("The site answered with error 429.")
    error.retry_after = seconds
    return error


async def test_a_source_that_says_when_to_come_back_is_not_checked_sooner(make):
    bench = make()
    feed = await bench.started("news")
    bench.sources.fetch_script[feed.url] = asks_to_wait(7200.5)

    await bench.scheduler.tick()

    stored = bench.feed(feed)
    assert (stored.fail_count, stored.last_error) == (1, "The site answered with error 429.")
    assert stored.next_check_at == bench.clock.t + 7201
    fetches = bench.sources.fetched(feed.url)
    bench.clock.t += 7200  # long past the usual wait after one failed Check
    await bench.scheduler.tick()
    assert bench.sources.fetched(feed.url) == fetches
    bench.clock.t += 1
    await bench.scheduler.tick()
    assert bench.sources.fetched(feed.url) == fetches + 1


@pytest.mark.parametrize("seconds", [30, 0, -5, None, float("nan"), float("inf"), "soon"])
async def test_a_shorter_or_senseless_wait_leaves_the_usual_backoff(make, seconds):
    bench = make()
    feed = await bench.started("news")
    bench.sources.fetch_script[feed.url] = asks_to_wait(seconds)

    waits = []
    for _ in range(2):
        bench.wait(feed)
        await bench.scheduler.tick()
        waits.append(bench.feed(feed).next_check_at - bench.clock.t)

    assert waits == [600, 1200]


async def test_a_feed_checked_less_often_than_hourly_keeps_its_interval_when_it_fails(make):
    bench = make()
    feed = await bench.started("news", interval_s=7200)
    fetch_refused(bench, feed)

    waits = []
    for _ in range(3):
        bench.wait(feed)
        await bench.scheduler.tick()
        waits.append(bench.feed(feed).next_check_at - bench.clock.t)

    assert waits == [7200, 7200, 7200]


async def test_a_broken_feed_is_reported_once_and_so_is_its_recovery(make):
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.sources.fetch_script[feed.url] = FetchError("The site answered with error 503.")
    await bench.scheduler.tick()
    since = bench.clock.t

    while bench.clock.t < since + 3 * DAY:
        bench.wait(feed)
        await bench.scheduler.tick()
        assert len(bench.notifier.reports) == (1 if bench.clock.t - since >= DAY else 0)

    [(server_id, broken, actor)] = bench.notifier.reports
    assert (server_id, actor) == (SERVER, Actor.bot())
    assert (broken.kind, broken.actor_id) == (LogKind.FEED_BROKEN, None)
    assert (broken.feed_id, broken.feed_name, broken.channel_id) == (feed.id, "news", CHANNEL)
    assert "error 503" in broken.detail and "still being checked" in broken.detail
    stored = bench.feed(feed)
    assert stored.warned and stored.paused is None  # a Broken feed is still checked
    assert stored.failing_since == since

    del bench.sources.fetch_script[feed.url]
    bench.publish(feed, item("new"))
    bench.wait(feed)
    await bench.scheduler.tick()
    _, working, actor = bench.notifier.reports[1]
    assert (working.kind, working.actor_id, actor) == (
        LogKind.FEED_WORKING_AGAIN,
        None,
        Actor.bot(),
    )
    assert (working.feed_id, working.feed_name, working.detail) == (feed.id, "news", "")
    assert bench.deliverer.posted == ["new"]
    stored = bench.feed(feed)
    assert not stored.warned
    assert (stored.fail_count, stored.failing_since, stored.last_error) == (0, None, "")

    bench.wait(feed)
    await bench.scheduler.tick()
    assert len(bench.notifier.reports) == 2
    assert bench.logged() == [broken, working]


async def test_a_feed_that_fails_again_after_recovering_is_reported_again(make):
    bench = make()
    feed = await bench.started("news")
    for _ in range(2):
        fetch_refused(bench, feed)
        bench.wait(feed)
        await bench.scheduler.tick()
        bench.clock.t += DAY
        bench.wait(feed)
        await bench.scheduler.tick()
        bench.sources.fetch_script.clear()
        bench.wait(feed)
        await bench.scheduler.tick()

    kinds = [entry.kind for _, entry, _ in bench.notifier.reports]
    assert kinds == [LogKind.FEED_BROKEN, LogKind.FEED_WORKING_AGAIN] * 2


# -- one Feed against the others --


def bad_fetch(bench: Bench, feed: Feed) -> None:
    bench.sources.fetch_script[feed.url] = RuntimeError("boom")


def bad_parse(bench: Bench, feed: Feed) -> None:
    bench.sources.parse_script[feed.url] = RuntimeError("boom")


def bad_database(bench: Bench, feed: Feed) -> None:
    bench.store.broken_feeds.add(feed.id)


def bad_render(bench: Bench, feed: Feed) -> None:
    for key in ("bad-1", "bad-2", "bad-3"):
        bench.render.broken.add(key)
        bench.render_default.broken.add(key)


def bad_deliver(bench: Bench, feed: Feed) -> None:
    for key in ("bad-1", "bad-2", "bad-3"):
        bench.deliverer.script[key] = RuntimeError("boom")


def bad_from_render_to_note(bench: Bench, feed: Feed) -> None:
    """One Check in which the renderer, the default renderer, the deliverer and the notifier
    all raise, one after the other."""
    bench.render.broken.update({"bad-1", "bad-2", "bad-3"})
    bench.render_default.broken.add("bad-1")
    bench.deliverer.script["plain:bad-2"] = RuntimeError("boom")
    bench.deliverer.script["plain:bad-3"] = D.LOST_CHANNEL
    bench.notifier.error = RuntimeError("boom")


@pytest.mark.parametrize(
    "sabotage",
    [bad_fetch, bad_parse, bad_database, bad_render, bad_deliver, bad_from_render_to_note],
)
async def test_one_feed_that_raises_everywhere_does_not_stop_nine_healthy_ones(make, sabotage):
    bench = make(tripped=True)
    bad = await bench.started("bad")
    good = [await bench.started(f"good{n}") for n in range(9)]
    bench.publish(bad, item("bad-3", 3), item("bad-2", 2), item("bad-1", 1))
    for n, feed in enumerate(good):
        bench.publish(feed, item(f"good-{n}"))
    bench.wait(bad, *good)
    sabotage(bench, bad)

    await bench.scheduler.tick()

    assert sorted(bench.deliverer.posted) == [f"good-{n}" for n in range(9)]
    for feed in good:
        stored = bench.feed(feed)
        assert stored.fail_count == 0
        assert stored.last_success_at == bench.clock.t

    # And the loop goes on: the next ticks are no different.
    for n, feed in enumerate(good):
        bench.publish(feed, item(f"more-{n}"))
    bench.clock.t += DAY
    await bench.scheduler.tick()
    assert sorted(bench.deliverer.posted)[9:] == [f"more-{n}" for n in range(9)]


@pytest.mark.parametrize(
    "call",
    [
        "due_feeds",
        "get_feed",
        "get_server",
        "update_feed",
        "has_seen_items",
        "seen_states",
        "touch_seen",
        "prune_seen",
        "list_filters",
        "record_seen",
        "bump_attempts",
        "purge_removed_servers",
    ],
)
async def test_any_database_call_may_raise_without_breaking_or_losing_anything(make, call):
    bench = make(tripped=True)
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("new"))
    bench.store.broken_calls.add(call)

    await bench.scheduler.tick()
    await bench.scheduler.check_feed(feed.id)
    await bench.scheduler.tick()

    bench.store.broken_calls.clear()
    bench.clock.t += DAY
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["new"]  # posted, and exactly once
    assert bench.status(feed, "new") is DELIVERED


async def test_a_clock_that_raises_does_not_break_a_tick(make):
    bench = make()
    feed = bench.add("news", item("a"))
    bench.clock.broken = True

    await bench.scheduler.tick()
    await bench.scheduler.check_feed(feed.id)

    bench.clock.broken = False
    await bench.scheduler.check_feed(feed.id)
    assert bench.status(feed, "a") is SEEN


async def test_nothing_is_fetched_while_the_feed_cannot_be_saved_and_it_is_not_hammered(make):
    bench = make(tripped=True)
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    fetches = len(bench.sources.fetches)
    bench.store.broken_calls.add("update_feed")

    await bench.scheduler.tick()
    assert len(bench.sources.fetches) == fetches
    tries = bench.store.calls.count("update_feed")

    # The Feed is still due, but it is not tried on every tick.
    bench.clock.t += 30
    await bench.scheduler.tick()
    assert bench.store.calls.count("update_feed") == tries
    bench.clock.t += HOLD_OFF_S
    await bench.scheduler.tick()
    assert bench.store.calls.count("update_feed") == tries + 1

    bench.store.broken_calls.clear()
    bench.clock.t += HOLD_OFF_S
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a"]


def writes_status(status: ItemStatus) -> Callable[[str, tuple[Any, ...]], bool]:
    def check(name: str, args: tuple[Any, ...]) -> bool:
        return name == "record_seen" and any(written is status for _, written in args[1])

    return check


async def test_an_item_that_cannot_be_recorded_is_not_posted(make):
    bench = make(tripped=True)
    feed = await bench.started("news")
    bench.publish(feed, item("b", 2), item("a", 1))
    bench.store.fail_if = writes_status(PENDING)

    await bench.scheduler.tick()
    assert bench.deliverer.attempts == []
    assert bench.state(feed, "a") is None

    bench.store.fail_if = None
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a", "b"]


async def test_an_item_that_cannot_be_recorded_does_not_stop_the_next_one(make):
    bench = make(tripped=True)
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    bench.store.fail_if = lambda name, args: name == "record_seen" and args[1][0][0] == "b"

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == ["a", "c"]
    assert bench.feed(feed).fail_count == 0

    bench.store.fail_if = None
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a", "c", "b"]


async def test_a_delivery_that_cannot_be_recorded_is_not_repeated_without_end(make):
    bench = make(tripped=True)
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.store.fail_if = writes_status(DELIVERED)

    for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
        bench.wait(feed)
        await bench.scheduler.tick()

    # Posted once. The record of that was lost, so it is skipped rather than posted again.
    assert bench.deliverer.posted == ["a"]
    assert bench.state(feed, "a") == (SKIPPED, 1)


# -- concurrency --


async def test_no_more_feeds_are_checked_at_once_than_the_limit(make):
    bench = make(max_concurrent=3)
    feeds = [bench.add(f"feed{n}") for n in range(10)]
    bench.sources.delay = 0.01

    await bench.scheduler.tick()
    assert len(bench.sources.fetches) == 10
    assert bench.sources.peak == 3

    # Direct calls and a second tick share the same limit.
    bench.wait(*feeds)
    bench.sources.peak = 0
    await asyncio.gather(
        bench.scheduler.tick(),
        bench.scheduler.tick(),
        *(bench.scheduler.check_feed(feed.id) for feed in feeds),
    )
    assert len(bench.sources.fetches) == 20
    assert bench.sources.peak == 3


async def test_a_feed_is_never_checked_twice_at_once(make):
    bench = make()
    feed = bench.add("news", item("a"))
    bench.sources.gate = asyncio.Event()
    first = asyncio.create_task(bench.scheduler.check_feed(feed.id))
    await until(lambda: bench.sources.active == 1)

    await asyncio.wait_for(bench.scheduler.check_feed(feed.id), 1)  # comes back at once
    bench.clock.t += DAY  # long past any time the first Check may have booked
    await asyncio.wait_for(bench.scheduler.tick(), 1)
    both = asyncio.gather(bench.scheduler.tick(), bench.scheduler.check_feed(feed.id))
    await asyncio.wait_for(both, 1)
    assert len(bench.sources.fetches) == 1

    bench.sources.gate.set()
    await first
    assert bench.status(feed, "a") is SEEN


async def test_the_next_check_is_booked_before_the_work_starts(make):
    bench = make()
    feed = await bench.started("news")
    booked = []
    bench.sources.during_fetch = lambda url: booked.append(bench.feed(feed).next_check_at)

    await bench.scheduler.tick()

    # Had the process died in that fetch, the Feed would not be first in line on restart.
    assert booked == [bench.clock.t + INTERVAL]


async def test_a_check_that_ignores_its_cancellation_is_left_behind(make):
    bench = make(check_timeout_s=0.2, grace_s=0.05)
    stubborn = await bench.started("stubborn")
    other = await bench.started("other")
    bench.publish(stubborn, item("s"))
    bench.publish(other, item("o"))
    release = asyncio.Event()

    async def will_not_stop() -> DeliveryOutcome:
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.005)
        return D.DELIVERED

    bench.deliverer.script["s"] = will_not_stop
    try:
        await asyncio.wait_for(bench.scheduler.tick(), 2)  # the tick comes back all the same

        assert bench.deliverer.posted == ["o"]
        stored = bench.feed(stubborn)
        assert stored.fail_count == 1 and "took longer" in stored.last_error

        # While that Check is still running the Feed is not worked on a second time.
        bench.wait(stubborn)
        await bench.scheduler.tick()
        assert bench.sources.fetched(stubborn.url) == 2  # its first Check, and the stubborn one
        stored = bench.feed(stubborn)
        assert stored.fail_count == 2 and "never finished" in stored.last_error
    finally:
        release.set()

    # Once it lets go, what it delivered is on record and the Feed is checked as before.
    await until(lambda: bench.status(stubborn, "s") is DELIVERED)
    await asyncio.sleep(0.01)
    bench.wait(stubborn)
    await bench.scheduler.tick()
    assert bench.feed(stubborn).fail_count == 0
    assert bench.deliverer.posted == ["o", "s"]


async def test_a_parse_that_never_returns_costs_one_thread_not_one_per_check(make):
    bench = make(check_timeout_s=0.2)
    feed = await bench.started("news")
    other = await bench.started("other")
    bench.publish(other, item("o"))
    release = threading.Event()
    bench.sources.parse_script[feed.url] = release.wait
    try:
        for count in range(1, 4):
            bench.wait(feed)
            await bench.scheduler.tick()
            assert bench.feed(feed).fail_count == count
        assert "never finished" in bench.feed(feed).last_error
        assert bench.sources.parses.count(feed.url) == 2  # the first Check, and the stuck one
        assert bench.deliverer.posted == ["o"]
    finally:
        release.set()

    # The thread came back after all: the Feed is checked as before.
    del bench.sources.parse_script[feed.url]
    bench.publish(feed, item("a"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        bench.wait(feed)
        await bench.scheduler.tick()
        if bench.feed(feed).fail_count == 0:
            break
    assert bench.deliverer.posted == ["o", "a"]


# -- Feeds that change under a Check --


async def test_a_feed_deleted_or_paused_after_it_was_listed_is_left_alone(make):
    bench = make(tripped=True)
    gone = bench.add("gone", item("a"))
    paused = bench.add("paused", item("b"))
    kept = bench.add("kept", item("c"))

    def change() -> None:
        bench.db.delete_feed(gone.id)
        bench.db.update_feed(paused.id, paused=PauseReason.MANUAL)

    bench.store.after["due_feeds"] = change

    await bench.scheduler.tick()

    assert [url for url, _, _ in bench.sources.fetches] == [kept.url]
    assert bench.feed(paused).next_check_at == START
    assert bench.status(paused, "b") is None


@pytest.mark.parametrize("change", ["deleted", "paused", "pointed elsewhere"])
async def test_a_feed_changed_while_it_is_fetched_posts_nothing(make, change):
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("new"))
    due = bench.feed(feed).next_check_at

    def act(url: str) -> None:
        if change == "deleted":
            bench.db.delete_feed(feed.id)
        elif change == "paused":
            bench.db.update_feed(feed.id, paused=PauseReason.MANUAL)
        else:
            bench.db.update_feed(feed.id, url="https://elsewhere.example/rss")

    bench.sources.during_fetch = act

    await bench.scheduler.tick()

    assert bench.deliverer.attempts == []
    stored = bench.db.get_feed(feed.id)
    if change == "deleted":
        assert stored is None
    else:
        assert (stored.fail_count, stored.last_error) == (0, "")
        assert stored.next_check_at == due  # as if the Check had not been started
        assert bench.state(feed, "new") is None


async def test_the_feeds_of_a_removed_server_are_not_checked(make):
    bench = make()
    feed = bench.add("news", item("a"))
    bench.db.mark_server_removed(SERVER, START)

    await bench.scheduler.tick()
    await bench.scheduler.check_feed(feed.id)

    assert bench.sources.fetches == []


# -- cancellation and the loop --


async def test_cancelling_mid_check_keeps_what_was_delivered_and_is_passed_on(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("c", 3), item("b", 2), item("a", 1))
    due = bench.feed(feed).next_check_at
    bench.deliverer.script["b"] = [HANG]
    task = asyncio.create_task(bench.scheduler.tick())
    await until(lambda: bench.deliverer.attempts == ["a", "b"])

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bench.state(feed, "a") == (DELIVERED, 1)
    assert bench.state(feed, "b") == (SKIPPED, 1)  # cut off: possibly posted, so not sent again
    assert bench.state(feed, "c") is None
    stored = bench.feed(feed)
    assert stored.fail_count == 0  # a shutdown is not a failed Check
    assert stored.next_check_at == due

    # The next start carries on where this one stopped: "a" is not posted again.
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a", "c"]


async def test_cancelling_check_feed_is_passed_on(make):
    bench = make()
    feed = bench.add("news")
    fetch_hangs(bench, feed)
    task = asyncio.create_task(bench.scheduler.check_feed(feed.id))
    await until(lambda: bench.sources.active == 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bench.sources.active == 0  # the fetch was stopped as well
    assert bench.feed(feed).fail_count == 0


async def test_cancelling_while_a_note_is_sent_keeps_what_the_check_stored(make):
    bench = make()
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = D.LOST_CHANNEL
    bench.notifier.hang = True
    task = asyncio.create_task(bench.scheduler.tick())
    await until(lambda: len(bench.notifier.reports) == 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bench.feed(feed).paused is PauseReason.LOST_CHANNEL


async def test_cancelling_while_waiting_for_a_free_slot_is_passed_on(make):
    bench = make(max_concurrent=1)
    first, second = bench.add("first"), bench.add("second", item("a"))
    bench.sources.gate = asyncio.Event()
    running = asyncio.create_task(bench.scheduler.check_feed(first.id))
    await until(lambda: bench.sources.active == 1)
    waiting = asyncio.create_task(bench.scheduler.check_feed(second.id))
    await asyncio.sleep(0.01)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    # It left nothing behind: the Feed can be checked as soon as there is room.
    bench.sources.gate.set()
    await running
    await bench.scheduler.check_feed(second.id)
    assert bench.status(second, "a") is SEEN


async def test_run_ticks_until_it_is_cancelled(make):
    bench = make(tick_s=30.0)
    feed = bench.add("news", item("a"))
    task = asyncio.create_task(bench.scheduler.run())
    await until(lambda: len(bench.clock.sleeps) >= 3)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bench.clock.sleeps[:3] == [30.0, 30.0, 30.0]
    assert bench.status(feed, "a") is SEEN


async def test_run_stops_promptly_when_cancelled_in_the_middle_of_a_check(make):
    bench = make()
    feed = bench.add("news")
    fetch_hangs(bench, feed)
    task = asyncio.create_task(bench.scheduler.run())
    await until(lambda: bench.sources.active == 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)

    assert bench.sources.active == 0


async def test_run_can_be_cancelled_even_if_the_clock_never_waits(make):
    bench = make()

    async def no_wait(seconds: float) -> None:
        return None

    bench.clock.sleep = no_wait
    task = asyncio.create_task(bench.scheduler.run())
    await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


async def test_run_supervised_starts_the_loop_again_after_an_unexpected_exception(make, caplog):
    bench = make()
    feed = bench.add("news", item("a"))
    bench.clock.sleep_errors = [RuntimeError("the clock broke"), KeyError("again")]
    task = asyncio.create_task(bench.scheduler.run_supervised())
    await until(lambda: len(bench.clock.sleeps) >= 6)  # it went on ticking after both

    assert not task.done()
    assert caplog.text.count("stopped unexpectedly") == 2
    assert bench.status(feed, "a") is SEEN
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# -- validators and what the source says about itself --


async def test_validators_are_stored_and_sent_and_not_modified_is_a_quiet_success(make):
    bench = make()
    feed = bench.add("news", item("a"))
    bench.sources.etags[feed.url] = '"v1"'
    bench.sources.modified[feed.url] = "Mon, 01 Jan 2024 00:00:00 GMT"

    await bench.scheduler.tick()
    assert bench.sources.fetches == [(feed.url, None, None)]
    stored = bench.feed(feed)
    assert (stored.etag, stored.last_modified) == ('"v1"', "Mon, 01 Jan 2024 00:00:00 GMT")

    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.sources.fetches[-1] == (feed.url, '"v1"', "Mon, 01 Jan 2024 00:00:00 GMT")
    assert bench.sources.parses == [feed.url]  # nothing to parse the second time
    stored = bench.feed(feed)
    assert stored.last_success_at == bench.clock.t
    assert stored.next_check_at == bench.clock.t + INTERVAL
    assert (stored.etag, stored.fail_count) == ('"v1"', 0)

    bench.publish(feed, item("b"))
    bench.sources.etags[feed.url] = '"v2"'
    bench.wait(feed)
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["b"]
    assert bench.feed(feed).etag == '"v2"'


async def test_the_title_and_link_of_the_source_are_kept_up_to_date(make):
    bench = make()
    feed = bench.add("news")
    bench.sources.serve(feed.url, title="Old name", link="https://old.example/")
    await bench.scheduler.tick()
    stored = bench.feed(feed)
    assert (stored.source_title, stored.source_link) == ("Old name", "https://old.example/")

    bench.sources.serve(feed.url, title="New name", link="https://new.example/")
    bench.wait(feed)
    await bench.scheduler.tick()
    stored = bench.feed(feed)
    assert (stored.source_title, stored.source_link) == ("New name", "https://new.example/")


# -- Seen items over time --


async def test_seen_items_are_kept_while_listed_and_forgotten_30_days_after(make):
    bench = make()
    feed = await bench.started("news", item("stays"), item("goes"))
    bench.sources.serve(feed.url, item("stays"))

    bench.clock.t = START + 30 * DAY
    await bench.scheduler.tick()
    assert bench.status(feed, "goes") is SEEN

    bench.clock.t = START + 30 * DAY + INTERVAL
    await bench.scheduler.tick()
    assert bench.status(feed, "goes") is None
    assert bench.status(feed, "stays") is SEEN

    bench.clock.t = START + 100 * DAY
    await bench.scheduler.tick()
    assert bench.status(feed, "stays") is SEEN
    assert bench.deliverer.attempts == []


async def test_a_long_run_of_not_modified_does_not_forget_what_is_still_listed(make):
    bench = make()
    feed = bench.add("news", item("a"), item("b"))
    bench.sources.etags[feed.url] = '"v1"'
    await bench.scheduler.tick()
    for day in range(1, 41):
        bench.clock.t = START + day * DAY
        await bench.scheduler.tick()
    assert bench.sources.parses == [feed.url]

    bench.publish(feed, item("c"))
    bench.sources.etags[feed.url] = '"v2"'
    bench.clock.t += DAY
    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["c"]


async def test_a_source_that_listed_nothing_for_a_month_still_posts_its_next_item(make):
    bench = make()
    feed = await bench.started("status", item("incident-1"))
    bench.sources.serve(feed.url)
    for day in range(1, 36):
        bench.clock.t = START + day * DAY
        await bench.scheduler.tick()
    assert bench.status(feed, "incident-1") is None
    assert bench.status(feed, STARTED_KEY) is SEEN

    bench.publish(feed, item("incident-2"))
    bench.clock.t += DAY
    await bench.scheduler.tick()

    assert bench.deliverer.posted == ["incident-2"]


# -- housekeeping, jitter, logging --


async def test_long_removed_servers_are_purged_at_most_once_an_hour(make):
    bench = make()
    for server_id, days in ((2, 31), (3, 29)):
        bench.db.ensure_server(server_id)
        bench.db.mark_server_removed(server_id, START - days * DAY)

    await bench.scheduler.tick()
    assert bench.db.get_server(2) is None
    assert bench.db.get_server(3) is not None

    bench.db.ensure_server(4)
    bench.db.mark_server_removed(4, START - 40 * DAY)
    bench.clock.t += 3599
    await bench.scheduler.tick()
    assert bench.db.get_server(4) is not None
    bench.clock.t += 1
    await bench.scheduler.tick()
    assert bench.db.get_server(4) is None


async def test_log_entries_over_a_year_old_are_deleted_once_a_day(make, caplog):
    caplog.set_level(logging.INFO, logger="rssbot.scheduler")
    bench = make()

    def save(days_ago: int) -> int:
        entry = bench.db.add_log_entry(
            server_id=SERVER,
            at=bench.clock.t - days_ago * DAY,
            actor_id=7,
            actor_name="Alex",
            kind=LogKind.FEED_REMOVED,
        )
        return entry.id

    old, recent = save(LOG_KEEP_S // DAY + 1), save(LOG_KEEP_S // DAY - 3)
    await bench.scheduler.tick()
    assert [entry.id for entry in bench.logged()] == [recent]
    assert "Deleted the Log entries from over a year ago: 1" in caplog.text
    assert old not in [entry.id for entry in bench.logged()]

    caplog.clear()
    older = save(400)
    bench.clock.t += DAY - 1
    await bench.scheduler.tick()
    assert older in [entry.id for entry in bench.logged()]  # not again within the day
    bench.clock.t += 1
    await bench.scheduler.tick()
    assert older not in [entry.id for entry in bench.logged()]

    caplog.clear()
    bench.clock.t += DAY
    await bench.scheduler.tick()
    assert "Deleted" not in caplog.text  # nothing to delete, nothing to say


async def test_a_prune_that_fails_does_not_stop_the_loop(make):
    bench = make(tripped=True)
    feed = await bench.started("news")
    bench.publish(feed, item("a"))
    bench.store.broken_calls.add("prune_log_entries")
    bench.clock.t += 2 * DAY
    await bench.scheduler.tick()
    assert bench.deliverer.posted == ["a"]


def raising_rand() -> float:
    raise RuntimeError("no entropy")


@pytest.mark.parametrize(
    ("rand", "tenths"),
    [
        (lambda: 0.0, 0),
        (lambda: 0.5, 5),
        (lambda: 1.0, 10),
        (lambda: 7.0, 10),
        (lambda: -3.0, 0),
        (lambda: float("nan"), 0),
        (raising_rand, 0),
    ],
    ids=["none", "half", "all", "too much", "negative", "nan", "raises"],
)
async def test_every_booked_time_gets_up_to_a_tenth_more(make, rand, tenths):
    bench = make(rand=rand)
    feed = bench.add("news")

    await bench.scheduler.tick()
    assert bench.feed(feed).next_check_at == START + INTERVAL + INTERVAL * tenths // 100

    fetch_refused(bench, feed)
    for wait in (600, 1200, 2400, 3600):
        bench.wait(feed)
        await bench.scheduler.tick()
        assert bench.feed(feed).next_check_at == bench.clock.t + wait + wait * tenths // 100


async def test_real_jitter_stays_within_a_tenth_and_spreads_the_feeds(make):
    bench = make(rand=random.Random(7).random)
    feeds = [bench.add(f"feed{n}") for n in range(40)]

    await bench.scheduler.tick()

    waits = [bench.feed(feed).next_check_at - START for feed in feeds]
    assert all(INTERVAL <= wait <= INTERVAL * 1.1 for wait in waits)
    assert len(set(waits)) > 10


async def test_the_log_has_one_line_per_failed_check_and_per_skipped_item(make, caplog):
    caplog.set_level(logging.INFO, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("a"))
    await bench.scheduler.tick()
    # A healthy Check says nothing above INFO, and that only if it posted something.
    [record] = caplog.records
    assert record.levelno == logging.INFO and record.getMessage().startswith("check feed=")
    caplog.clear()

    fetch_refused(bench, feed)
    bench.wait(feed)
    await bench.scheduler.tick()
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "news" in record.getMessage() and "error 500" in record.getMessage()

    caplog.clear()
    bench.sources.fetch_script.clear()
    bench.publish(feed, item("b"))
    bench.deliverer.script["b"] = D.RETRY
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        bench.wait(feed)
        await bench.scheduler.tick()
    skipped = [r for r in caplog.records if r.getMessage().startswith("item.skipped")]
    [record] = skipped
    assert record.levelno == logging.WARNING and 'item="b"' in record.getMessage()
    failed = [r for r in caplog.records if r.getMessage().startswith("deliver.failed")]
    assert len(failed) == MAX_DELIVERY_ATTEMPTS  # one per attempt, each its own warning


async def test_system_clock_tells_the_time_and_sleeps():
    clock = SystemClock()
    assert abs(clock.now() - time.time()) < 2
    assert isinstance(clock.now(), int)
    await clock.sleep(0)


# -- the container log --


def messages(caplog, level: int | None = None) -> list[str]:
    return [r.getMessage() for r in caplog.records if level is None or r.levelno == level]


async def test_a_check_that_posted_is_one_info_line_and_a_quiet_one_is_debug(make, caplog):
    caplog.set_level(logging.DEBUG, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    caplog.clear()

    bench.publish(feed, item("a"))
    await bench.scheduler.tick()
    bench.wait(feed)
    await bench.scheduler.tick()

    checks = [r for r in caplog.records if r.getMessage().startswith("check feed=")]
    posted, nothing_new = checks
    assert posted.levelno == logging.INFO
    assert posted.getMessage().startswith(
        f'check feed={feed.id} name="news" server={SERVER} posted=1 skipped=0 took_ms='
    )
    assert nothing_new.levelno == logging.DEBUG
    assert "posted=0 skipped=0" in nothing_new.getMessage()
    ticks = [m for m in messages(caplog) if m.startswith("tick ")]
    assert ticks and all(m.startswith("tick due=") for m in ticks)
    assert any(m == "tick due=0" for m in ticks) or any(m == "tick due=1" for m in ticks)


async def test_a_not_modified_check_is_logged_at_debug_only(make, caplog):
    bench = make()
    feed = bench.add("news", item("old"))
    bench.sources.etags[feed.url] = '"v1"'
    await bench.scheduler.check_feed(feed.id)
    bench.wait(feed)
    caplog.set_level(logging.DEBUG, logger="rssbot.scheduler")
    await bench.scheduler.tick()

    [line] = [m for m in messages(caplog) if m.startswith("check feed=")]
    assert "not_modified=yes" in line
    caplog.set_level(logging.INFO, logger="rssbot.scheduler")
    caplog.clear()
    bench.wait(feed)
    await bench.scheduler.tick()
    assert caplog.records == []


async def test_a_failed_check_warns_with_the_error_and_the_count_in_a_row(make, caplog):
    caplog.set_level(logging.INFO, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    fetch_refused(bench, feed)
    for _ in range(2):
        bench.wait(feed)
        await bench.scheduler.tick()

    [first, second] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert first.getMessage() == (
        f'check.failed feed={feed.id} name="news" server={SERVER} failures=1 '
        'error="The site answered with error 500."'
    )
    assert " failures=2 " in second.getMessage()


async def test_a_feed_becoming_rate_limited_warns_once_and_stays_quiet_after(make, caplog):
    caplog.set_level(logging.INFO, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.sources.fetch_script[feed.url] = FetchError("slow down", slow_down=True)
    for _ in range(3):
        bench.wait(feed)
        await bench.scheduler.tick()

    [line] = [m for m in messages(caplog, logging.WARNING) if m.startswith("check.rate_limited")]
    assert line.startswith(f'check.rate_limited feed={feed.id} name="news" server={SERVER} ')


async def test_a_skipped_item_warns_with_its_reason_and_is_logged_once(make, caplog):
    caplog.set_level(logging.DEBUG, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("b"))
    bench.deliverer.script["b"] = D.RETRY
    for _ in range(MAX_DELIVERY_ATTEMPTS):
        bench.wait(feed)
        await bench.scheduler.tick()

    skipped = [m for m in messages(caplog, logging.WARNING) if m.startswith("item.skipped")]
    assert skipped == [
        f'item.skipped feed={feed.id} name="news" item="b" '
        f'reason="its delivery failed {MAX_DELIVERY_ATTEMPTS} times"'
    ]
    [last] = [m for m in messages(caplog) if m.startswith("check feed=")][-1:]
    assert "skipped=1" in last


async def test_a_failed_delivery_warns_and_a_delivery_is_a_debug_line(make, caplog):
    caplog.set_level(logging.DEBUG, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    bench.publish(feed, item("a"))
    await bench.scheduler.tick()
    delivered = [r for r in caplog.records if r.getMessage().startswith("deliver ")]
    assert [r.levelno for r in delivered] == [logging.DEBUG]
    assert "outcome=delivered" in delivered[0].getMessage()

    caplog.clear()
    bench.publish(feed, item("b"))
    bench.deliverer.script["b"] = D.RETRY
    bench.wait(feed)
    await bench.scheduler.tick()
    [failed] = [r for r in caplog.records if r.getMessage().startswith("deliver.failed")]
    assert failed.levelno == logging.WARNING and "outcome=retry" in failed.getMessage()


async def test_a_pause_by_the_bot_is_a_warning_from_the_journal_and_a_plain_check_line(
    make, caplog
):
    bench = make()
    feed = await bench.started("news", item("old"))
    caplog.set_level(logging.DEBUG)
    caplog.clear()
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = D.LOST_CHANNEL
    await bench.scheduler.tick()

    warnings = messages(caplog, logging.WARNING)
    assert sum(m.startswith("feed.auto_pause") for m in warnings) == 1  # the Journal's
    assert not any(m.startswith("check feed=") for m in warnings)
    [check] = [r for r in caplog.records if r.getMessage().startswith("check feed=")]
    assert check.levelno == logging.DEBUG  # nothing was posted
    assert "paused=lost_channel" in check.getMessage()


async def test_a_hostile_feed_name_cannot_break_or_forge_a_line(make, caplog):
    caplog.set_level(logging.DEBUG, logger="rssbot.scheduler")
    bench = make()
    feed = await bench.started("news", item("old"))
    hostile = 'x" server=999 by="evil"\nWARNING fake line'
    bench.db.update_feed(feed.id, name=hostile)
    fetch_refused(bench, feed)
    bench.wait(feed)
    await bench.scheduler.tick()
    bench.sources.fetch_script.clear()
    bench.publish(feed, item("a"))
    bench.deliverer.script["a"] = D.RETRY
    bench.wait(feed)
    await bench.scheduler.tick()

    lines = [m for m in messages(caplog) if "evil" in m]
    assert lines
    for line in lines:
        assert "\n" not in line
        assert '\\" server=999 by=\\"evil\\"' in line  # the quotes are escaped, still one value


async def test_nothing_secret_is_logged_when_a_fetch_fails(make, caplog):
    from rssbot.logsetup import RedactingFormatter

    caplog.set_level(logging.DEBUG)
    bench = make()
    feed = bench.add("news", item("old"))
    secret_url = "https://example.com/feed?key=SECRET"
    bench.db.update_feed(feed.id, url=secret_url)
    bench.sources.serve(secret_url, item("old"))
    bench.sources.fetch_script[secret_url] = RuntimeError(f"cannot connect to {secret_url}")
    await bench.scheduler.tick()

    assert caplog.records
    formatter = RedactingFormatter()
    for record in caplog.records:
        if record.name.startswith("rssbot"):
            assert "SECRET" not in formatter.format(record)
    assert "SECRET" not in caplog.text.split("RuntimeError")[0]


# -- cover image fallback (docs/adr/0004) --


def article(image: str) -> str:
    return f'<html><head><meta property="og:image" content="{image}"></head><body></body></html>'


async def test_an_item_without_an_image_gets_the_og_image_of_its_page(make):
    bench = make()
    feed = await bench.started("a", item("old"))
    new = item("new", link="https://site.example/new")
    bench.sources.pages[new.link] = article("https://cdn.example/pic.jpg")
    bench.publish(feed, new)
    bench.wait(feed)
    await bench.scheduler.check_feed(feed.id)
    assert [i.image for i in bench.render.rendered] == ["https://cdn.example/pic.jpg"]


async def test_an_item_with_an_image_does_not_fetch_its_page(make):
    bench = make()
    feed = await bench.started("a", item("old"))
    new = item("new", image="https://cdn.example/feed.jpg")
    bench.publish(feed, new)
    bench.wait(feed)
    await bench.scheduler.check_feed(feed.id)
    assert [i.image for i in bench.render.rendered] == ["https://cdn.example/feed.jpg"]
    assert bench.sources.fetched(new.link) == 0


async def test_a_page_that_cannot_be_read_still_posts_the_item(make):
    bench = make()
    feed = await bench.started("a", item("old"))
    new = item("new")
    bench.sources.fetch_script[new.link] = FetchError("blocked")
    bench.publish(feed, new)
    bench.wait(feed)
    await bench.scheduler.check_feed(feed.id)
    assert [i.image for i in bench.render.rendered] == [""]


async def test_a_page_that_hangs_is_given_up_on_and_the_item_still_posts(make):
    bench = make(cover_timeout_s=0.05)
    feed = await bench.started("a", item("old"))
    new = item("new")
    bench.sources.fetch_script[new.link] = HANG
    bench.publish(feed, new)
    bench.wait(feed)
    await bench.scheduler.check_feed(feed.id)
    assert [i.image for i in bench.render.rendered] == [""]


async def test_pages_are_fetched_at_most_ten_at_a_time(make):
    bench = make()
    feed = await bench.started("a", item("old"))
    new = [item(f"n{i}", published=START + i) for i in range(CATCH_UP_LIMIT)]
    bench.publish(feed, *new)
    bench.sources.delay = 0.01
    bench.wait(feed)
    await bench.scheduler.check_feed(feed.id)
    assert 1 < bench.sources.peak <= 10
