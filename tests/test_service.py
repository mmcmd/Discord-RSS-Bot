from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Iterator
from typing import Any

import pytest

from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import (
    DEFAULT_FORUM_TITLE_TEMPLATE,
    DEFAULT_INTERVAL_S,
    DEFAULT_TEXT_TEMPLATE,
    MAX_BUTTONS,
    MAX_EMBED_FIELDS,
    MAX_FORUM_TAGS,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    Actor,
    ButtonSpec,
    Change,
    ChannelKind,
    EmbedSpec,
    Feed,
    FieldSpec,
    FilterField,
    FilterList,
    Item,
    ItemStatus,
    LogEntry,
    LogKind,
    OutgoingMessage,
    ParsedFeed,
    PauseReason,
    PostAs,
)
from rssbot.opml import OpmlEntry, build_opml, parse_opml
from rssbot.parse import ParseError
from rssbot.ports import DeliveryOutcome, FetchError, FetchResult, ImageData
from rssbot.render import render_default, render_item
from rssbot.scheduler import STARTED_KEY, Scheduler
from rssbot.service import (
    IMPORT_CONCURRENCY,
    MAX_FILTERS,
    MAX_IMPORT_FEEDS,
    MAX_MENTION_ROLES,
    NO_SUCH_FEED,
    SITE_REFRESH_S,
    DuplicateFeed,
    FeedService,
    FeedStatus,
    ServiceError,
    status_line,
    status_of,
)
from rssbot.template import PLACEHOLDERS

START = 1_700_000_000
SERVER = 1
OTHER_SERVER = 2
CHANNEL = 500
OTHER_CHANNEL = 501
URL = "https://example.com/feed.xml"
URL2 = "https://other.example/rss"
MESSAGES = ChannelKind.MESSAGES
FORUM = ChannelKind.FORUM
ALEX = Actor(id=7, name="Alex", avatar_url="https://cdn.example/alex.png")
SAM = Actor(id=8, name="Sam")


# -- fakes --


class FakeClock:
    def __init__(self) -> None:
        self.t = START

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds)
        await asyncio.sleep(0)


def item(key: str, published: int | None = None, **kwargs: object) -> Item:
    values: dict[str, object] = {
        "key": key,
        "title": f"Title {key}",
        "link": f"https://example.com/{key}",
        "summary": f"Summary {key}",
        "content": "",
        "author": "",
        "published": published,
        "categories": (),
        "image": "",
    }
    values.update(kwargs)
    return Item(**values)  # type: ignore[arg-type]


def listing(
    *keys: str, title: str = "Example News", link: str = "https://example.com/"
) -> ParsedFeed:
    return ParsedFeed(title=title, link=link, image="", items=tuple(item(k) for k in keys))


class Web:
    """What every address serves. Stands in for the fetcher and the parser."""

    def __init__(self) -> None:
        self.listings: dict[str, ParsedFeed] = {}
        self.errors: dict[str, Exception] = {}  # address -> what the fetch raises
        self.unparsable: dict[str, Exception] = {}  # address -> what the parser raises
        self.pages: dict[str, bytes] = {}  # home pages
        self.calls: list[tuple[str, str | None]] = []
        self.served_type = ""  # the Content-Type every fetch reports
        self.parsed_types: list[str] = []  # the Content-Type each parse was handed
        self.in_flight = 0
        self.most_in_flight = 0

    def _etag(self, url: str) -> str:
        return repr(self.listings[url])

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append((url, etag))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if url in self.errors:
                raise self.errors[url]
            if url in self.pages:
                return FetchResult(False, self.pages[url], None, None, url)
            if url in self.unparsable:
                return FetchResult(False, url.encode(), None, None, url)
            if url not in self.listings:
                raise FetchError("The address answered with error 404.")
            if etag is not None and etag == self._etag(url):
                return FetchResult(True, b"", etag, None, url)
            return FetchResult(
                False, url.encode(), self._etag(url), "yesterday", url, self.served_type
            )
        finally:
            self.in_flight -= 1

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        raise FetchError("no images here")

    def parse(self, body: bytes, url: str, content_type: str = "") -> ParsedFeed:
        self.parsed_types.append(content_type)
        if url in self.unparsable:
            raise self.unparsable[url]
        return self.listings[body.decode()]


class Posts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        self.sent.append(message.content)
        return DeliveryOutcome.DELIVERED

    async def notify(self, server_id: int, text: str) -> None:
        pass

    async def announce(self, server_id: int, entries: object, actor: object) -> None:
        pass


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def web() -> Web:
    web = Web()
    web.listings[URL] = listing("a", "b", "c")
    web.listings[URL2] = listing("x", "y", title="Other Site", link="https://other.example/")
    return web


class Reports:
    """The fake notifier: every report made in a Logs channel, as (entries, actor)."""

    def __init__(self) -> None:
        self.sent: list[tuple[list[LogEntry], Actor]] = []

    async def notify(self, server_id: int, text: str) -> None:
        pass

    async def announce(self, server_id: int, entries: Any, actor: Actor) -> None:
        self.sent.append((list(entries), actor))


@pytest.fixture
def reports() -> Reports:
    return Reports()


@pytest.fixture
def journal(db: Database, clock: FakeClock, reports: Reports) -> Journal:
    return Journal(db, clock, reports)


@pytest.fixture
def service(db: Database, web: Web, clock: FakeClock, journal: Journal) -> FeedService:
    return FeedService(db, web, clock, journal, parse=web.parse, rand=lambda: 0.0)


async def add(service: FeedService, url: str = URL, **kwargs: object) -> Feed:
    channel = kwargs.pop("channel_id", CHANNEL)
    kind = kwargs.pop("channel_kind", MESSAGES)
    feed, _ = await service.add_feed(SERVER, channel, kind, url, actor=ALEX, **kwargs)  # type: ignore[arg-type]
    return feed


def scheduler(db: Database, web: Web, clock: FakeClock, posts: Posts) -> Scheduler:
    return Scheduler(
        db,
        web,
        posts,
        Journal(db, clock, posts),
        clock,
        web.parse,
        render_item,
        render_default,
        rand=lambda: 0.0,
    )


async def error(awaitable: Awaitable[object]) -> str:
    with pytest.raises(ServiceError) as caught:
        await awaitable
    return str(caught.value)


# -- add --


async def test_add_stores_what_the_source_says(service: FeedService, db: Database) -> None:
    feed, count = await service.add_feed(SERVER, CHANNEL, MESSAGES, f"  {URL}  ", actor=ALEX)
    assert count == 3
    assert feed == db.get_feed(feed.id)
    assert (feed.server_id, feed.channel_id, feed.channel_kind) == (SERVER, CHANNEL, MESSAGES)
    assert (feed.name, feed.url) == ("Example News", URL)
    assert (feed.source_title, feed.source_link) == ("Example News", "https://example.com/")
    assert feed.etag is not None
    assert feed.last_modified == "yesterday"
    assert feed.interval_s == DEFAULT_INTERVAL_S
    assert feed.next_check_at == START + DEFAULT_INTERVAL_S
    assert feed.post_as is PostAs.BOT
    assert feed.text_template == DEFAULT_TEXT_TEMPLATE
    assert feed.forum_title_template == DEFAULT_FORUM_TITLE_TEMPLATE


async def test_add_records_everything_listed_as_seen(service: FeedService, db: Database) -> None:
    feed = await add(service)
    states = db.seen_states(feed.id, ["a", "b", "c", STARTED_KEY])
    assert states == dict.fromkeys(["a", "b", "c", STARTED_KEY], (ItemStatus.SEEN, 0))


async def test_add_leaves_nothing_to_post_but_later_items_are_posted(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    feed = await add(service)
    posts = Posts()
    checks = scheduler(db, web, clock, posts)

    clock.t += feed.interval_s + 1
    await checks.tick()
    assert posts.sent == []
    assert web.calls[-1] == (URL, feed.etag)  # the stored validators were used

    web.listings[URL] = listing("new", "a", "b", "c")
    clock.t += feed.interval_s * 3
    await checks.tick()
    assert len(posts.sent) == 1
    assert "Title new" in posts.sent[0]


async def test_add_to_an_empty_source_still_posts_its_first_item(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    web.listings[URL] = listing()
    feed, count = await service.add_feed(SERVER, CHANNEL, MESSAGES, URL, actor=ALEX)
    assert count == 0
    posts = Posts()
    checks = scheduler(db, web, clock, posts)
    web.listings[URL] = listing("first")
    clock.t += feed.interval_s + 1
    await checks.tick()
    assert len(posts.sent) == 1


async def test_a_failed_add_is_logged_with_the_site_and_never_the_address(
    service: FeedService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="rssbot.service")

    with pytest.raises(ServiceError):
        await add(service, "https://www.private.example/feed/secret-key?token=hunter2")

    (line,) = [record.getMessage() for record in caplog.records]
    assert line == "Could not fetch from private.example: The address answered with error 404."


async def test_no_log_line_carries_the_private_key_of_an_address(
    service: FeedService, web: Web, caplog: pytest.LogCaptureFixture
) -> None:
    # caplog does not go through the redacting formatter: the source must not write the key.
    caplog.set_level("DEBUG")
    unreadable = "https://example.com/feed?key=SECRET"
    web.unparsable[unreadable] = RuntimeError("boom")
    with pytest.raises(ServiceError):
        await add(service, unreadable)

    odd = "https://odd.example/feed?key=SECRET"
    web.errors[odd] = RuntimeError("not a FetchError")
    await service.import_opml(SERVER, CHANNEL, MESSAGES, opml(("Odd", odd)), actor=ALEX)

    assert "Could not parse example.com" in caplog.text
    assert "OPML import: could not add odd.example" in caplog.text
    assert "SECRET" not in caplog.text


async def test_add_hands_the_served_content_type_to_the_parser(
    service: FeedService, web: Web
) -> None:
    web.listings[URL] = listing("a")
    web.served_type = "application/rss+xml; charset=koi8-r"

    await add(service)

    assert web.parsed_types == ["application/rss+xml; charset=koi8-r"]


async def test_add_puts_https_before_a_bare_address(service: FeedService, web: Web) -> None:
    web.listings["https://bare.example/feed"] = listing("a", title="")
    feed = await add(service, "bare.example/feed")
    assert feed.url == "https://bare.example/feed"
    assert feed.name == "bare.example"  # no source title: the host


async def test_add_name_is_given_or_cut(service: FeedService, web: Web) -> None:
    assert (await add(service, name="  My   feed ")).name == "My feed"
    web.listings[URL2] = listing("x", title="T" * 150)
    assert (await add(service, URL2)).name == "T" * 100
    assert (await add(service, URL2, channel_id=OTHER_CHANNEL, name="N" * 150)).name == "N" * 100


async def test_add_with_interval(service: FeedService) -> None:
    feed = await add(service, interval_s=3600)
    assert feed.interval_s == 3600
    assert feed.next_check_at == START + 3600


def raising_rand() -> float:
    raise RuntimeError("no randomness today")


@pytest.mark.parametrize(
    ("rand", "sooner"),
    [
        (lambda: 0.0, 0),
        (lambda: 0.5, 900),
        (lambda: 1.0, 1800),
        (lambda: 7.0, 1800),  # out of range: held to the nearest end
        (lambda: -3.0, 0),
        (lambda: float("nan"), 0),
        (raising_rand, 0),
    ],
)
async def test_add_books_the_first_check_up_to_half_an_interval_sooner(
    db: Database, web: Web, clock: FakeClock, rand: Callable[[], float], sooner: int
) -> None:
    service = FeedService(db, web, clock, Journal(db, clock), parse=web.parse, rand=rand)
    feed = await add(service, interval_s=3600)
    assert feed.next_check_at == START + 3600 - sooner


async def test_feeds_added_together_are_not_all_due_together(
    db: Database, web: Web, clock: FakeClock
) -> None:
    import random

    urls = [f"https://example.com/feed{n}.xml" for n in range(100)]
    for url in urls:
        web.listings[url] = listing("a")
    service = FeedService(
        db, web, clock, Journal(db, clock), parse=web.parse, rand=random.Random(7).random
    )
    result = await service.import_opml(
        SERVER, CHANNEL, MESSAGES, opml(*((u, u) for u in urls)), actor=ALEX
    )
    assert len(result.added) == 100

    due = [feed.next_check_at - START for feed in db.list_feeds(SERVER)]
    assert all(DEFAULT_INTERVAL_S // 2 <= d <= DEFAULT_INTERVAL_S for d in due)
    # Spread over the half interval, not bunched: no minute holds more than a few of them.
    per_minute = [sum(d // 60 == minute for d in due) for minute in {d // 60 for d in due}]
    assert max(per_minute) <= 100 * 60 * 5 // (DEFAULT_INTERVAL_S // 2) + 5
    assert max(due) - min(due) > DEFAULT_INTERVAL_S // 4


@pytest.mark.parametrize("interval", [MIN_INTERVAL_S - 1, MAX_INTERVAL_S + 1, 0, -5])
async def test_add_refuses_a_bad_interval(
    service: FeedService, db: Database, web: Web, interval: int
) -> None:
    message = await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, URL, interval_s=interval, actor=ALEX)
    )
    assert "between 5 minutes and 24 hours" in message
    assert web.calls == []
    assert db.count_feeds(SERVER) == 0


@pytest.mark.parametrize(
    "url", ["", "   ", "ftp://example.com/feed", "https://", "https://exa mple.com/x", "x" * 3000]
)
async def test_add_refuses_a_bad_address(service: FeedService, web: Web, url: str) -> None:
    assert "Feed address" in await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, url, actor=ALEX)
    )
    assert web.calls == []


@pytest.mark.parametrize(
    "typed",
    [
        "example.com/feed",
        "example.com:8080/feed",
        "example.com/feed?src=https://x.example/",
        "example.com/redirect#https://x.example/",
        "example.com/https://x.example/feed",
    ],
)
async def test_add_gives_an_address_without_a_scheme_https(
    service: FeedService, web: Web, typed: str
) -> None:
    web.listings["https://" + typed] = listing("a")
    feed = await add(service, typed)
    assert feed.url == "https://" + typed


@pytest.mark.parametrize(
    "typed", ["feed://example.com/feed", "feed:https://example.com/feed", "ftp://example.com/feed"]
)
async def test_add_refuses_an_address_with_another_scheme(
    service: FeedService, web: Web, typed: str
) -> None:
    assert "starting with http or https" in await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, typed, actor=ALEX)
    )
    assert web.calls == []


async def test_add_refuses_when_the_fetch_fails(
    service: FeedService, db: Database, web: Web
) -> None:
    web.errors[URL] = FetchError("The site did not answer in time.")
    message = await error(service.add_feed(SERVER, CHANNEL, MESSAGES, URL, actor=ALEX))
    assert message == "The site did not answer in time."
    assert db.count_feeds(SERVER) == 0


async def test_add_refuses_what_is_not_a_feed(service: FeedService, db: Database, web: Web) -> None:
    web.unparsable[URL] = ParseError("That address returned a web page, not an RSS or Atom feed.")
    message = await error(service.add_feed(SERVER, CHANNEL, MESSAGES, URL, actor=ALEX))
    assert message == "That address returned a web page, not an RSS or Atom feed."
    web.unparsable[URL] = RuntimeError("boom")
    assert "did not return a feed" in await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, URL, actor=ALEX)
    )
    assert db.count_feeds(SERVER) == 0


async def test_add_refuses_past_the_most_feeds_a_server_may_have(
    service: FeedService, db: Database, web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rssbot.service.MAX_FEEDS_PER_SERVER", 2)
    await add(service)
    await add(service, URL2)
    assert "already has 2 Feeds, the most allowed" in await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, "https://third.example/", actor=ALEX)
    )
    assert db.count_feeds(SERVER) == 2
    await service.add_feed(OTHER_SERVER, OTHER_CHANNEL, MESSAGES, URL, actor=SAM)  # not shared


async def test_adds_at_the_same_time_do_not_pass_the_most_feeds_a_server_may_have(
    service: FeedService, db: Database, web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rssbot.service.MAX_FEEDS_PER_SERVER", 3)
    urls = [f"https://many.example/{number}" for number in range(6)]
    for url in urls:
        web.listings[url] = listing("a")
    await add(service)

    results = await asyncio.gather(
        *(service.add_feed(SERVER, CHANNEL, MESSAGES, url, actor=ALEX) for url in urls),
        return_exceptions=True,
    )

    assert db.count_feeds(SERVER) == 3
    assert sum(isinstance(result, ServiceError) for result in results) == 4


async def test_import_does_not_pass_the_most_feeds_a_server_may_have(
    service: FeedService, db: Database, web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rssbot.service.MAX_FEEDS_PER_SERVER", 3)
    entries = []
    for number in range(8):
        url = f"https://many.example/{number}"
        web.listings[url] = listing("a")
        entries.append((f"Feed {number}", url))
    await add(service)

    result = await service.import_opml(SERVER, CHANNEL, MESSAGES, opml(*entries), actor=ALEX)

    assert db.count_feeds(SERVER) == 3
    assert len(result.added) == 2
    assert len(result.failed) == 6
    assert all("the most allowed" in failed.reason for failed in result.failed)


async def test_add_refuses_the_same_address_twice_in_one_channel(service: FeedService) -> None:
    await add(service)
    with pytest.raises(DuplicateFeed):
        await add(service)
    other = await add(service, channel_id=OTHER_CHANNEL)  # another channel: another Feed
    assert other.channel_id == OTHER_CHANNEL


async def test_add_posting_as_the_site_discovers_its_identity(
    service: FeedService, web: Web
) -> None:
    web.pages["https://example.com/"] = (
        b'<html><head><link rel="apple-touch-icon" href="/icon.png"></head></html>'
    )
    feed = await add(service, post_as=PostAs.SITE)
    assert feed.post_as is PostAs.SITE
    assert (feed.site_name, feed.site_icon) == ("Example News", "https://example.com/icon.png")
    assert feed.site_checked_at == START


async def test_add_posting_under_a_custom_name(service: FeedService) -> None:
    feed = await add(
        service,
        post_as=PostAs.CUSTOM,
        custom_name="Herald",
        custom_avatar="https://x.example/a.png",
    )
    assert (feed.post_as, feed.custom_name) == (PostAs.CUSTOM, "Herald")
    assert feed.custom_avatar == "https://x.example/a.png"
    message = await error(
        service.add_feed(SERVER, CHANNEL, MESSAGES, URL2, post_as=PostAs.CUSTOM, actor=ALEX)
    )
    assert "custom name is required" in message


async def test_the_real_parser_and_renderer_are_the_defaults(
    db: Database, web: Web, clock: FakeClock
) -> None:
    body = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Real</title>'
        b"<link>https://real.example/</link>"
        b"<item><title>One</title><link>https://real.example/1</link><guid>1</guid></item>"
        b"</channel></rss>"
    )
    web.pages["https://real.example/rss"] = body
    service = FeedService(db, web, clock, Journal(db, clock))
    feed, count = await service.add_feed(SERVER, CHANNEL, MESSAGES, "real.example/rss", actor=ALEX)
    assert (feed.name, count) == ("Real", 1)
    message, newest = await service.preview(SERVER, feed.id)
    assert newest.title == "One"
    assert "https://real.example/1" in message.content


# -- lookups --


async def test_get_feed_guards_the_server(service: FeedService) -> None:
    feed = await add(service)
    assert service.get_feed(SERVER, feed.id) == feed
    with pytest.raises(ServiceError, match=NO_SUCH_FEED):
        service.get_feed(OTHER_SERVER, feed.id)
    with pytest.raises(ServiceError, match=NO_SUCH_FEED):
        service.get_feed(SERVER, feed.id + 100)


Call = Callable[[FeedService, int, int], object]

OPERATIONS: dict[str, Call] = {
    "get_feed": lambda s, server, feed: s.get_feed(server, feed),
    "edit_feed": lambda s, server, feed: s.edit_feed(server, feed, name="x", actor=ALEX),
    "remove_feed": lambda s, server, feed: s.remove_feed(server, feed, actor=ALEX),
    "pause_feed": lambda s, server, feed: s.pause_feed(server, feed, actor=ALEX),
    "resume_feed": lambda s, server, feed: s.resume_feed(server, feed, actor=ALEX),
    "preview": lambda s, server, feed: s.preview(server, feed),
    "placeholder_values": lambda s, server, feed: s.placeholder_values(server, feed),
    "test_listing": lambda s, server, feed: s.test_listing(server, feed),
    "render_item": lambda s, server, feed: s.render_item(server, feed, item("a")),
    "record_posted": lambda s, server, feed: s.record_posted(server, feed, item("a")),
    "set_text": lambda s, server, feed: s.set_text(server, feed, "x", actor=ALEX),
    "set_embed": lambda s, server, feed: s.set_embed(server, feed, title="x", actor=ALEX),
    "remove_embed": lambda s, server, feed: s.remove_embed(server, feed, actor=ALEX),
    "add_field": lambda s, server, feed: s.add_field(server, feed, "n", "v", actor=ALEX),
    "remove_field": lambda s, server, feed: s.remove_field(server, feed, 1, actor=ALEX),
    "add_button": lambda s, server, feed: s.add_button(
        server, feed, "l", "https://x.example", actor=ALEX
    ),
    "remove_button": lambda s, server, feed: s.remove_button(server, feed, 1, actor=ALEX),
    "reset_template": lambda s, server, feed: s.reset_template(server, feed, actor=ALEX),
    "set_forum_title": lambda s, server, feed: s.set_forum_title(server, feed, "x", actor=ALEX),
    "set_mentions": lambda s, server, feed: s.set_mentions(server, feed, [1], actor=ALEX),
    "set_post_as": lambda s, server, feed: s.set_post_as(server, feed, PostAs.SITE, actor=ALEX),
    "refresh_site_identity": lambda s, server, feed: s.refresh_site_identity(server, feed),
    "set_forum_tags": lambda s, server, feed: s.set_forum_tags(server, feed, [1], actor=ALEX),
    "set_forum_cover": lambda s, server, feed: s.set_forum_cover(server, feed, False, actor=ALEX),
    "list_filters": lambda s, server, feed: s.list_filters(server, feed),
    "add_filters": lambda s, server, feed: s.add_filters(
        server, feed, FilterList.BLOCK, FilterField.ANY, ["x"], actor=ALEX
    ),
    "remove_filter": lambda s, server, feed: s.remove_filter(server, feed, 1, actor=ALEX),
}


def test_the_guard_test_covers_every_operation_that_takes_a_feed() -> None:
    import inspect

    takes_feed = {
        name
        for name, member in inspect.getmembers(FeedService, inspect.isfunction)
        if not name.startswith("_") and "feed_id" in inspect.signature(member).parameters
    }
    assert takes_feed == set(OPERATIONS)


@pytest.mark.parametrize("name", sorted(OPERATIONS))
async def test_no_operation_touches_another_servers_feed(
    service: FeedService, db: Database, web: Web, name: str
) -> None:
    feed = await add(service, post_as=PostAs.CUSTOM, custom_name="Herald")
    await service.add_field(SERVER, feed.id, "n", "v", actor=ALEX)
    await service.add_button(SERVER, feed.id, "l", "https://x.example", actor=ALEX)
    await service.add_filters(SERVER, feed.id, FilterList.BLOCK, FilterField.ANY, ["w"], actor=ALEX)
    before = (db.get_feed(feed.id), db.list_filters(feed.id))
    calls = len(web.calls)

    with pytest.raises(ServiceError) as caught:
        result = OPERATIONS[name](service, OTHER_SERVER, feed.id)
        if asyncio.iscoroutine(result):
            await result
    assert str(caught.value) == NO_SUCH_FEED
    assert (db.get_feed(feed.id), db.list_filters(feed.id)) == before
    assert len(web.calls) == calls


async def test_lists_and_search(service: FeedService) -> None:
    one = await add(service, name="Zebra")
    two = await add(service, URL2, channel_id=OTHER_CHANNEL, name="apple")
    assert service.list_feeds(SERVER) == [two, one]
    assert service.list_feeds(OTHER_SERVER) == []
    assert service.list_channel_feeds(SERVER, CHANNEL) == [one]
    assert service.list_channel_feeds(OTHER_SERVER, CHANNEL) == []
    assert service.search_feeds(SERVER, "") == [two, one]
    assert service.search_feeds(SERVER, " ZEB ") == [one]
    assert service.search_feeds(SERVER, "other.example") == [two]
    assert service.search_feeds(SERVER, "", limit=1) == [two]
    assert service.search_feeds(OTHER_SERVER, "") == []


# -- preview and placeholders --


async def test_preview_renders_the_newest_item_with_the_feeds_template(
    service: FeedService, db: Database, web: Web
) -> None:
    feed = await add(service)
    await service.set_text(SERVER, feed.id, "NEW: {{title}}", actor=ALEX)
    items = (item("old", 100), item("undated"), item("new", 300), item("tie", 300))
    web.listings[URL] = ParsedFeed("Example News", "", "", items)
    stored = db.get_feed(feed.id)
    calls = len(web.calls)

    message, newest = await service.preview(SERVER, feed.id)
    assert newest.key == "new"
    assert message.content == "NEW: Title new"
    assert web.calls[calls:] == [(URL, None)]  # no validators
    assert db.get_feed(feed.id) == stored
    assert db.seen_states(feed.id, ["old", "undated", "new", "tie"]) == {}


async def test_preview_without_dates_takes_the_first_listed(service: FeedService) -> None:
    feed = await add(service)
    _, newest = await service.preview(SERVER, feed.id)
    assert newest.key == "a"


async def test_a_previewed_item_recorded_as_posted_is_not_posted_by_the_next_check(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    feed = await add(service)
    web.listings[URL] = listing("new", "a", "b", "c")
    _, newest = await service.preview(SERVER, feed.id)
    assert newest.key == "new"
    await service.record_posted(SERVER, feed.id, newest)

    posts = Posts()
    clock.t += feed.interval_s + 1
    await scheduler(db, web, clock, posts).tick()
    assert posts.sent == []


def keys(items: Iterable[Item]) -> list[str]:
    return [found.key for found in items]


async def test_a_test_listing_offers_the_newest_items_the_filters_let_through(
    service: FeedService, db: Database, web: Web
) -> None:
    feed = await add(service)
    items = (
        item("old", 100),
        item("undated"),
        item("new", 300),
        item("tie", 300),
        item("ad", 400, author="Sponsored by Acme"),
        *(item(f"m{n}", 200 + n) for n in range(4)),
        item("ad2", 50, author="Sponsored by Zed"),
    )
    web.listings[URL] = ParsedFeed("Example News", "", "", items)
    stored = db.get_feed(feed.id)

    listing = await service.test_listing(SERVER, feed.id)
    assert keys(listing.choices) == ["ad", "new", "tie", "m3", "m2"]
    assert (listing.held_back, listing.listed) == ((), 10)

    await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.AUTHOR, ["Sponsored"], actor=ALEX
    )
    listing = await service.test_listing(SERVER, feed.id)
    assert keys(listing.choices) == ["new", "tie", "m3", "m2", "m1"]
    assert keys(listing.held_back) == ["ad", "ad2"]
    assert db.get_feed(feed.id) == stored
    assert db.seen_states(feed.id, ["new", "ad"]) == {}


async def test_a_test_listing_looks_as_far_back_as_it_takes(service: FeedService, web: Web) -> None:
    feed = await add(service)
    items = (*(item(f"n{n}", 900 - n) for n in range(30)), item("linux", 1, title="Linux 7"))
    web.listings[URL] = ParsedFeed("Example News", "", "", items)
    await service.add_filters(
        SERVER, feed.id, FilterList.MUST_HAVE, FilterField.TITLE, ["linux"], actor=ALEX
    )
    listing = await service.test_listing(SERVER, feed.id)
    assert keys(listing.choices) == ["linux"]
    assert (len(listing.held_back), listing.listed) == (30, 31)
    assert listing.held_back[0].key == "n0"  # newest first

    await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.TITLE, ["7"], actor=ALEX
    )
    listing = await service.test_listing(SERVER, feed.id)
    assert (listing.choices, len(listing.held_back)) == ((), 31)


async def test_a_test_listing_errors(service: FeedService, web: Web) -> None:
    feed = await add(service)
    web.listings[URL] = listing()
    assert "does not list any Items" in await error(service.test_listing(SERVER, feed.id))
    web.errors[URL] = FetchError("The site is down.")
    assert await error(service.test_listing(SERVER, feed.id)) == "The site is down."


async def test_render_item_uses_the_feeds_template(service: FeedService) -> None:
    feed = await add(service)
    await service.set_text(SERVER, feed.id, "NEW: {{title}}", actor=ALEX)
    assert service.render_item(SERVER, feed.id, item("b")).content == "NEW: Title b"


async def test_preview_errors(service: FeedService, web: Web) -> None:
    feed = await add(service)
    web.listings[URL] = listing()
    assert "does not list any Items" in await error(service.preview(SERVER, feed.id))
    web.errors[URL] = FetchError("The site is down.")
    assert await error(service.preview(SERVER, feed.id)) == "The site is down."


async def test_preview_with_a_renderer_that_breaks(
    db: Database, web: Web, clock: FakeClock
) -> None:
    def broken(feed: Feed, item: Item) -> OutgoingMessage:
        raise RuntimeError("boom")

    service = FeedService(db, web, clock, Journal(db, clock), parse=web.parse, render=broken)
    feed = await add(service)
    assert "could not be made into a message" in await error(service.preview(SERVER, feed.id))
    with pytest.raises(ServiceError, match="could not be made into a message"):
        service.render_item(SERVER, feed.id, item("a"))


async def test_placeholder_values(service: FeedService, web: Web) -> None:
    feed = await add(service)
    web.listings[URL] = ParsedFeed("Example News", "", "", (item("a", summary="s" * 500),))
    values = await service.placeholder_values(SERVER, feed.id)
    assert tuple(name for name, _ in values) == PLACEHOLDERS
    as_dict = dict(values)
    assert as_dict["title"] == "Title a"
    assert as_dict["feed_title"] == "Example News"
    assert len(as_dict["summary"]) == 200
    assert as_dict["summary"].endswith("…")


async def test_placeholder_values_are_empty_when_the_source_cannot_be_read(
    service: FeedService, web: Web
) -> None:
    feed = await add(service)
    web.errors[URL] = FetchError("The site is down.")
    assert await service.placeholder_values(SERVER, feed.id) == [(n, "") for n in PLACEHOLDERS]
    del web.errors[URL]
    web.listings[URL] = listing()
    assert await service.placeholder_values(SERVER, feed.id) == [(n, "") for n in PLACEHOLDERS]


# -- edit --


async def test_edit_name_and_interval(service: FeedService, web: Web, clock: FakeClock) -> None:
    feed = await add(service, interval_s=3600)
    calls = len(web.calls)
    clock.t += 60
    edited, old_channel = await service.edit_feed(
        SERVER, feed.id, name="  New  name ", interval_s=600, url=URL, actor=ALEX
    )
    assert old_channel is None
    assert (edited.name, edited.interval_s) == ("New name", 600)
    assert edited.next_check_at == clock.t + 600  # sooner than it was booked
    assert (edited.url, edited.etag) == (URL, feed.etag)
    assert len(web.calls) == calls  # the same address is not fetched again

    longer, _ = await service.edit_feed(SERVER, feed.id, interval_s=7200, actor=ALEX)
    assert longer.next_check_at == edited.next_check_at  # never pushed further away


async def test_edit_with_nothing_changes_nothing(service: FeedService) -> None:
    feed = await add(service)
    assert await service.edit_feed(SERVER, feed.id, actor=ALEX) == (feed, None)


async def test_edit_validation(service: FeedService, db: Database, web: Web) -> None:
    feed = await add(service)
    assert "name cannot be empty" in await error(
        service.edit_feed(SERVER, feed.id, name="  ", actor=ALEX)
    )
    assert "between 5 minutes" in await error(
        service.edit_feed(SERVER, feed.id, interval_s=10, actor=ALEX)
    )
    assert "Feed address" in await error(
        service.edit_feed(SERVER, feed.id, url="ftp://x.example", actor=ALEX)
    )
    with pytest.raises(ValueError):
        await service.edit_feed(SERVER, feed.id, channel_id=OTHER_CHANNEL, actor=ALEX)
    assert (await service.edit_feed(SERVER, feed.id, name="n" * 150, actor=ALEX))[
        0
    ].name == "n" * 100


async def test_edit_url_is_refused_when_it_cannot_be_read(
    service: FeedService, db: Database, web: Web
) -> None:
    feed = await add(service)
    web.errors[URL2] = FetchError("The site is down.")
    message = await error(service.edit_feed(SERVER, feed.id, url=URL2, name="Renamed", actor=ALEX))
    assert message == "The site is down."
    assert db.get_feed(feed.id) == feed  # nothing of the call was applied

    del web.errors[URL2]
    web.unparsable[URL2] = ParseError("Not a feed.")
    assert await error(service.edit_feed(SERVER, feed.id, url=URL2, actor=ALEX)) == "Not a feed."
    assert db.get_feed(feed.id) == feed


async def test_edit_url_only_keeps_a_channel_moved_meanwhile(
    service: FeedService, db: Database, web: Web
) -> None:
    feed = await add(service)
    real_fetch = web.fetch

    async def fetch_while_moved(url: str, **kwargs: Any) -> FetchResult:
        if url == URL2:  # another edit moves the Feed while this one reads the new address
            db.update_feed(feed.id, channel_id=OTHER_CHANNEL, channel_kind=FORUM)
        return await real_fetch(url, **kwargs)

    web.fetch = fetch_while_moved  # type: ignore[method-assign]
    edited, old_channel = await service.edit_feed(SERVER, feed.id, url=URL2, actor=ALEX)
    assert old_channel is None
    assert (edited.url, edited.channel_id, edited.channel_kind) == (URL2, OTHER_CHANNEL, FORUM)


async def test_edit_url_resets_and_rebaselines(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    feed = await add(service, name="Mine")
    db.update_feed(
        feed.id, fail_count=4, failing_since=START, warned=True, last_error="The site is down."
    )
    clock.t += 1000
    edited, old_channel = await service.edit_feed(
        SERVER, feed.id, url="other.example/rss", actor=ALEX
    )

    assert old_channel is None
    assert edited.url == URL2
    assert edited.name == "Mine"
    assert (edited.source_title, edited.source_link) == ("Other Site", "https://other.example/")
    assert edited.etag is not None and edited.etag != feed.etag
    assert edited.next_check_at == clock.t + edited.interval_s
    assert (edited.fail_count, edited.failing_since, edited.warned) == (0, None, False)
    assert edited.last_error == ""
    states = db.seen_states(feed.id, ["x", "y", STARTED_KEY])
    assert set(states) == {"x", "y", STARTED_KEY}
    assert states["x"] == (ItemStatus.SEEN, 0)

    # Nothing the new address lists now is posted; what it lists later is.
    posts = Posts()
    checks = scheduler(db, web, clock, posts)
    clock.t += edited.interval_s + 1
    await checks.tick()
    assert posts.sent == []
    web.listings[URL2] = listing("z", "x", "y", title="Other Site")
    clock.t += edited.interval_s * 3
    await checks.tick()
    assert len(posts.sent) == 1
    assert "Title z" in posts.sent[0]


async def test_edit_url_rebaselines_a_feed_that_was_never_checked(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    # As a Feed made by other means would be: no Seen items at all.
    feed = db.create_feed(
        server_id=SERVER, channel_id=CHANNEL, channel_kind=MESSAGES, name="n", url=URL, now=START
    )
    await service.edit_feed(SERVER, feed.id, url=URL2, actor=ALEX)
    posts = Posts()
    web.listings[URL2] = listing("z", "x", "y")
    clock.t += DEFAULT_INTERVAL_S * 2
    await scheduler(db, web, clock, posts).tick()
    assert len(posts.sent) == 1


async def test_edit_url_refreshes_the_site_identity(service: FeedService, web: Web) -> None:
    feed = await add(service, post_as=PostAs.SITE)
    assert feed.site_name == "Example News"
    web.pages["https://other.example/"] = (
        b'<head><link rel="icon" type="image/png" href="https://other.example/i.png"></head>'
    )
    edited, _ = await service.edit_feed(SERVER, feed.id, url=URL2, actor=ALEX)
    assert (edited.site_name, edited.site_icon) == ("Other Site", "https://other.example/i.png")


async def test_edit_channel_clears_tags_and_reports_the_old_channel(
    service: FeedService, db: Database
) -> None:
    feed = await add(service, channel_kind=FORUM)
    await service.set_forum_tags(SERVER, feed.id, [7, 8], actor=ALEX)
    edited, old_channel = await service.edit_feed(
        SERVER, feed.id, channel_id=OTHER_CHANNEL, channel_kind=MESSAGES, actor=ALEX
    )
    assert old_channel == CHANNEL
    assert (edited.channel_id, edited.channel_kind) == (OTHER_CHANNEL, MESSAGES)
    assert edited.forum_tag_ids == ()

    same, old_channel = await service.edit_feed(
        SERVER, feed.id, channel_id=OTHER_CHANNEL, channel_kind=MESSAGES, actor=ALEX
    )
    assert old_channel is None
    assert same == edited


@pytest.mark.parametrize(
    ("reason", "resumed"),
    [
        (PauseReason.LOST_CHANNEL, True),
        (PauseReason.NEEDS_TAG, True),
        (PauseReason.MANUAL, False),
    ],
)
async def test_edit_channel_resumes_a_feed_paused_over_its_channel(
    service: FeedService, db: Database, clock: FakeClock, reason: PauseReason, resumed: bool
) -> None:
    feed = await add(service)
    db.update_feed(feed.id, paused=reason)
    clock.t += 5
    edited, _ = await service.edit_feed(
        SERVER, feed.id, channel_id=OTHER_CHANNEL, channel_kind=FORUM, actor=ALEX
    )
    assert edited.paused == (None if resumed else reason)
    if resumed:
        assert edited.next_check_at == clock.t
        assert db.due_feeds(clock.t) == [edited]


async def test_edit_refuses_a_duplicate_in_the_target_channel(service: FeedService) -> None:
    await add(service)
    elsewhere = await add(service, channel_id=OTHER_CHANNEL)
    second = await add(service, URL2)
    with pytest.raises(DuplicateFeed):
        await service.edit_feed(
            SERVER, elsewhere.id, channel_id=CHANNEL, channel_kind=MESSAGES, actor=ALEX
        )
    with pytest.raises(DuplicateFeed):
        await service.edit_feed(SERVER, second.id, url=URL, actor=ALEX)


# -- Template --


async def test_set_text(service: FeedService) -> None:
    feed = await add(service)
    assert (
        await service.set_text(SERVER, feed.id, "{{title}} !", actor=ALEX)
    ).text_template == "{{title}} !"
    assert "at most 2000" in await error(service.set_text(SERVER, feed.id, "x" * 2001, actor=ALEX))
    assert "not a Placeholder name" in await error(
        service.set_text(SERVER, feed.id, "{{nope}}", actor=ALEX)
    )
    assert service.get_feed(SERVER, feed.id).text_template == "{{title}} !"
    assert (
        await service.set_text(SERVER, feed.id, "x" * 2000, actor=ALEX)
    ).text_template == "x" * 2000
    assert (
        await service.set_text(SERVER, feed.id, "  \n ", actor=ALEX)
    ).text_template == ""  # Embed only


async def test_set_embed_part_by_part(service: FeedService) -> None:
    feed = await add(service)
    feed = await service.set_embed(
        SERVER,
        feed.id,
        title="{{title}}",
        description="{{description:300}}",
        url=" {{link}} ",
        image="https://x.example/{{image}}",
        footer="{{feed_title}}",
        colour="#ff8800",
        actor=ALEX,
    )
    assert feed.embed == EmbedSpec(
        title="{{title}}",
        description="{{description:300}}",
        url="{{link}}",
        image="https://x.example/{{image}}",
        footer="{{feed_title}}",
        colour=0xFF8800,
    )
    feed = await service.add_field(SERVER, feed.id, "By", "{{author}}", actor=ALEX)
    feed = await service.set_embed(SERVER, feed.id, title="T", footer="", colour="", actor=ALEX)
    assert feed.embed is not None
    assert (feed.embed.title, feed.embed.footer, feed.embed.colour) == ("T", "", None)
    assert feed.embed.description == "{{description:300}}"  # untouched
    assert len(feed.embed.fields) == 1


@pytest.mark.parametrize(
    ("colour", "stored"),
    [("ff8800", 0xFF8800), ("#FF8800", 0xFF8800), (" #000000 ", 0), (0x123456, 0x123456), (0, 0)],
)
async def test_embed_colours(service: FeedService, colour: int | str, stored: int) -> None:
    feed = await add(service)
    await service.set_embed(SERVER, feed.id, title="T", actor=ALEX)  # a colour alone is not stored
    feed = await service.set_embed(SERVER, feed.id, colour=colour, actor=ALEX)
    assert feed.embed is not None and feed.embed.colour == stored


@pytest.mark.parametrize("colour", ["red", "#ff88", "#ff88000", "gg8800", -1, 0x1000000, True])
async def test_embed_refuses_a_bad_colour(service: FeedService, colour: int | str) -> None:
    feed = await add(service)
    assert "hex code" in await error(service.set_embed(SERVER, feed.id, colour=colour, actor=ALEX))
    assert service.get_feed(SERVER, feed.id).embed is None


@pytest.mark.parametrize(
    ("part", "value", "said"),
    [
        ("title", "x" * 257, "at most 256"),
        ("title", "{{nope}}", "not a Placeholder name"),
        ("description", "x" * 4097, "at most 4096"),
        ("description", "{{title:0}}", "length limit"),
        ("footer", "x" * 2049, "at most 2048"),
        ("url", "example.com", "must start with http://, https:// or a Placeholder"),
        ("url", "https://x.example/" + "x" * 2048, "at most 2048"),
        ("image", "javascript:alert(1)", "must start with"),
        ("image", "{{nope}}", "not a Placeholder name"),
    ],
)
async def test_embed_validation(service: FeedService, part: str, value: str, said: str) -> None:
    feed = await add(service)
    assert said in await error(service.set_embed(SERVER, feed.id, **{part: value}, actor=ALEX))
    assert service.get_feed(SERVER, feed.id).embed is None


async def test_remove_embed(service: FeedService) -> None:
    feed = await add(service)
    await service.set_embed(SERVER, feed.id, title="T", actor=ALEX)
    assert (await service.remove_embed(SERVER, feed.id, actor=ALEX)).embed is None
    assert (await service.remove_embed(SERVER, feed.id, actor=ALEX)).embed is None


async def test_fields(service: FeedService) -> None:
    feed = await add(service)
    feed = await service.add_field(
        SERVER, feed.id, "One", "{{author}}", actor=ALEX
    )  # makes the Embed
    feed = await service.add_field(SERVER, feed.id, "Two", "{{date}}", inline=True, actor=ALEX)
    feed = await service.add_field(SERVER, feed.id, "Three", "3", actor=ALEX)
    assert feed.embed == EmbedSpec(
        fields=(
            FieldSpec("One", "{{author}}"),
            FieldSpec("Two", "{{date}}", True),
            FieldSpec("Three", "3"),
        )
    )
    feed = await service.remove_field(SERVER, feed.id, 2, actor=ALEX)
    assert feed.embed is not None
    assert [f.name for f in feed.embed.fields] == ["One", "Three"]
    for position in (0, 3, -1):
        message = await error(service.remove_field(SERVER, feed.id, position, actor=ALEX))
        assert message == f"There is no Field number {position}."


async def test_removing_the_last_field_of_a_made_up_embed_removes_the_embed(
    service: FeedService, journal: Journal, db: Database
) -> None:
    feed = await add(service)
    feed = await service.add_field(SERVER, feed.id, "By", "{{author}}", actor=ALEX)
    assert feed.embed is not None

    feed = await service.remove_field(SERVER, feed.id, 1, actor=ALEX)

    assert feed.embed is None
    assert service.get_feed(SERVER, feed.id).embed is None
    await journal.drain()
    details = [entry.detail for entry in db.list_log_entries(SERVER, limit=10)]
    assert "removed Field 1" in details


async def test_removing_the_last_field_keeps_an_embed_with_something_to_show(
    service: FeedService,
) -> None:
    feed = await add(service)
    feed = await service.set_embed(SERVER, feed.id, title="{{title}}", actor=ALEX)
    feed = await service.add_field(SERVER, feed.id, "By", "{{author}}", actor=ALEX)

    feed = await service.remove_field(SERVER, feed.id, 1, actor=ALEX)

    assert feed.embed == EmbedSpec(title="{{title}}")


async def test_clearing_what_an_embed_shows_removes_it(service: FeedService) -> None:
    # Nothing to show is posted as no Embed, so it is stored as none (as with the last Field).
    feed = await add(service)
    feed = await service.set_embed(SERVER, feed.id, title="{{title}}", colour="#ff8800", actor=ALEX)
    assert feed.embed is not None

    feed = await service.set_embed(SERVER, feed.id, title="", actor=ALEX)

    assert feed.embed is None
    assert service.get_feed(SERVER, feed.id).embed is None


async def test_an_embed_with_only_a_link_colour_or_timestamp_is_not_stored(
    service: FeedService,
) -> None:
    feed = await add(service)
    for part in ({"url": "{{link}}"}, {"colour": "#ff8800"}, {"timestamp": False}):
        assert (await service.set_embed(SERVER, feed.id, actor=ALEX, **part)).embed is None  # type: ignore[arg-type]


async def test_an_embed_that_stays_none_is_not_logged(
    service: FeedService, journal: Journal, db: Database
) -> None:
    feed = await add(service)
    await service.set_embed(SERVER, feed.id, colour="#ff8800", actor=ALEX)
    await journal.drain()
    assert not [e for e in db.list_log_entries(SERVER, limit=10) if "Embed" in e.detail]


async def test_field_validation(service: FeedService) -> None:
    feed = await add(service)
    assert "no Field number 1" in await error(service.remove_field(SERVER, feed.id, 1, actor=ALEX))
    assert "name cannot be empty" in await error(
        service.add_field(SERVER, feed.id, " ", "v", actor=ALEX)
    )
    assert "value cannot be empty" in await error(
        service.add_field(SERVER, feed.id, "n", "", actor=ALEX)
    )
    assert "at most 256" in await error(
        service.add_field(SERVER, feed.id, "n" * 257, "v", actor=ALEX)
    )
    assert "at most 1024" in await error(
        service.add_field(SERVER, feed.id, "n", "v" * 1025, actor=ALEX)
    )
    assert "not a Placeholder" in await error(
        service.add_field(SERVER, feed.id, "n", "{{no}}", actor=ALEX)
    )
    assert service.get_feed(SERVER, feed.id).embed is None
    for number in range(MAX_EMBED_FIELDS):
        await service.add_field(SERVER, feed.id, f"n{number}", "v", actor=ALEX)
    assert f"at most {MAX_EMBED_FIELDS} Fields" in await error(
        service.add_field(SERVER, feed.id, "n", "v", actor=ALEX)
    )


async def test_buttons(service: FeedService) -> None:
    feed = await add(service)
    feed = await service.add_button(SERVER, feed.id, "Read", "{{link}}", actor=ALEX)
    feed = await service.add_button(SERVER, feed.id, "Site", "HTTPS://example.com", actor=ALEX)
    feed = await service.add_button(
        SERVER, feed.id, "{{feed_title}}", "http://x.example/{{title}}", actor=ALEX
    )
    assert feed.buttons[:2] == (
        ButtonSpec("Read", "{{link}}"),
        ButtonSpec("Site", "HTTPS://example.com"),
    )
    feed = await service.remove_button(SERVER, feed.id, 1, actor=ALEX)
    assert [b.label for b in feed.buttons] == ["Site", "{{feed_title}}"]
    for position in (0, 3):
        message = await error(service.remove_button(SERVER, feed.id, position, actor=ALEX))
        assert message == f"There is no Button number {position}."


async def test_button_validation(service: FeedService) -> None:
    feed = await add(service)

    def button(label: str, url: str) -> Awaitable[Feed]:
        return service.add_button(SERVER, feed.id, label, url, actor=ALEX)

    assert "label cannot be empty" in await error(button("", "{{link}}"))
    assert "link cannot be empty" in await error(button("l", " "))
    assert "at most 80" in await error(button("l" * 81, "{{link}}"))
    assert "at most 512" in await error(button("l", "https://x.example/" + "x" * 512))
    assert "must start with" in await error(button("l", "x.example"))
    assert "must start with" in await error(button("l", "see {{link}}"))
    assert "not a Placeholder" in await error(button("l", "{{no}}"))
    assert "not a Placeholder" in await error(button("{{no}}", "{{link}}"))
    assert service.get_feed(SERVER, feed.id).buttons == ()
    for number in range(MAX_BUTTONS):
        await service.add_button(SERVER, feed.id, f"b{number}", "{{link}}", actor=ALEX)
    assert f"at most {MAX_BUTTONS} Buttons" in await error(
        service.add_button(SERVER, feed.id, "l", "{{link}}", actor=ALEX)
    )


async def test_reset_template(service: FeedService) -> None:
    feed = await add(service, channel_kind=FORUM)
    await service.set_text(SERVER, feed.id, "x", actor=ALEX)
    await service.add_field(SERVER, feed.id, "n", "v", actor=ALEX)
    await service.add_button(SERVER, feed.id, "l", "{{link}}", actor=ALEX)
    await service.set_forum_title(SERVER, feed.id, "{{title}}", actor=ALEX)
    await service.set_mentions(SERVER, feed.id, [5], actor=ALEX)
    feed = await service.reset_template(SERVER, feed.id, actor=ALEX)
    assert (feed.text_template, feed.embed, feed.buttons) == (DEFAULT_TEXT_TEMPLATE, None, ())
    assert feed.forum_title_template == "{{title}}"  # not part of the Template
    assert feed.mention_role_ids == (5,)


async def test_set_forum_title(service: FeedService) -> None:
    feed = await add(service, channel_kind=FORUM)
    feed = await service.set_forum_title(
        SERVER, feed.id, "  [{{feed_title}}] {{title}} ", actor=ALEX
    )
    assert feed.forum_title_template == "[{{feed_title}}] {{title}}"
    assert "cannot be empty" in await error(
        service.set_forum_title(SERVER, feed.id, " ", actor=ALEX)
    )
    assert "at most 200" in await error(
        service.set_forum_title(SERVER, feed.id, "x" * 201, actor=ALEX)
    )
    assert "not a Placeholder" in await error(
        service.set_forum_title(SERVER, feed.id, "{{no}}", actor=ALEX)
    )


async def test_set_mentions(service: FeedService) -> None:
    feed = await add(service)
    feed = await service.set_mentions(SERVER, feed.id, [30, 10, 30, 20, 10], actor=ALEX)
    assert feed.mention_role_ids == (30, 10, 20)
    too_many = range(100, 101 + MAX_MENTION_ROLES)
    assert "at most 10 roles" in await error(
        service.set_mentions(SERVER, feed.id, too_many, actor=ALEX)
    )
    ten = range(100, 110)
    assert (
        await service.set_mentions(SERVER, feed.id, [*ten, 100], actor=ALEX)
    ).mention_role_ids == tuple(ten)
    # The role whose id is the Server's own is @everyone and is never stored.
    assert (
        await service.set_mentions(SERVER, feed.id, [SERVER, 30], actor=ALEX)
    ).mention_role_ids == (30,)
    assert (await service.set_mentions(SERVER, feed.id, [], actor=ALEX)).mention_role_ids == ()


# -- Post as --


async def test_post_as_custom(service: FeedService) -> None:
    feed = await add(service)
    feed = await service.set_post_as(
        SERVER,
        feed.id,
        PostAs.CUSTOM,
        custom_name="  The  Herald ",
        custom_avatar=" http://x.example/a.png ",
        actor=ALEX,
    )
    assert (feed.post_as, feed.custom_name) == (PostAs.CUSTOM, "The Herald")
    assert feed.custom_avatar == "http://x.example/a.png"
    feed = await service.set_post_as(
        SERVER, feed.id, PostAs.CUSTOM, custom_name="Plain", actor=ALEX
    )
    assert (feed.custom_name, feed.custom_avatar) == ("Plain", "")

    def custom(name: str, avatar: str = "") -> Awaitable[Feed]:
        return service.set_post_as(
            SERVER, feed.id, PostAs.CUSTOM, custom_name=name, custom_avatar=avatar, actor=ALEX
        )

    assert "custom name is required" in await error(custom("  "))
    assert "at most 80" in await error(custom("n" * 81))
    for avatar in ("x.example/a.png", "ftp://x.example/a.png", "data:image/png;base64,AAAA"):
        assert "custom picture" in await error(custom("ok", avatar))
    assert service.get_feed(SERVER, feed.id) == feed


async def test_post_as_site_and_back_to_bot(
    service: FeedService, web: Web, clock: FakeClock
) -> None:
    feed = await add(service)
    web.listings[URL] = ParsedFeed("Example News", "", "https://example.com/logo.png", ())
    clock.t += 50
    feed = await service.set_post_as(SERVER, feed.id, PostAs.SITE, actor=ALEX)
    assert feed.post_as is PostAs.SITE
    assert (feed.site_name, feed.site_icon) == ("Example News", "https://example.com/logo.png")
    assert feed.site_checked_at == clock.t
    assert service.channel_uses_webhook(CHANNEL)

    feed = await service.set_post_as(SERVER, feed.id, PostAs.BOT, actor=ALEX)
    assert feed.post_as is PostAs.BOT
    assert not service.channel_uses_webhook(CHANNEL)


async def test_post_as_site_when_the_source_cannot_be_read(service: FeedService, web: Web) -> None:
    feed = await add(service)
    web.errors[URL] = FetchError("The site is down.")
    web.pages["https://example.com/"] = b'<head><link rel="apple-touch-icon" href="/i.png"></head>'
    feed = await service.set_post_as(SERVER, feed.id, PostAs.SITE, actor=ALEX)
    assert feed.post_as is PostAs.SITE
    assert (feed.site_name, feed.site_icon) == ("Example News", "https://example.com/i.png")


async def test_refresh_site_identity(service: FeedService, web: Web, clock: FakeClock) -> None:
    feed = await add(service)
    assert await service.refresh_site_identity(SERVER, feed.id) is False  # posts as the bot

    web.listings[URL] = ParsedFeed("Example News", "", "https://example.com/old.png", ())
    await service.set_post_as(SERVER, feed.id, PostAs.SITE, actor=ALEX)
    web.listings[URL] = ParsedFeed("Example Renamed", "", "https://example.com/new.png", ())

    clock.t += SITE_REFRESH_S - 1
    calls = len(web.calls)
    assert await service.refresh_site_identity(SERVER, feed.id) is False
    assert len(web.calls) == calls

    clock.t += 1
    assert await service.refresh_site_identity(SERVER, feed.id) is True
    feed = service.get_feed(SERVER, feed.id)
    assert (feed.site_name, feed.site_icon) == ("Example Renamed", "https://example.com/new.png")
    assert feed.site_checked_at == clock.t
    assert await service.refresh_site_identity(SERVER, feed.id) is False


async def test_refresh_keeps_the_picture_when_the_lookup_finds_none(
    service: FeedService, web: Web, clock: FakeClock
) -> None:
    feed = await add(service)
    web.listings[URL] = ParsedFeed("Example News", "", "https://example.com/old.png", ())
    await service.set_post_as(SERVER, feed.id, PostAs.SITE, actor=ALEX)
    web.errors[URL] = FetchError("The site is down.")
    web.errors["https://example.com/"] = FetchError("The site is down.")
    clock.t += SITE_REFRESH_S
    assert await service.refresh_site_identity(SERVER, feed.id) is True
    feed = service.get_feed(SERVER, feed.id)
    assert feed.site_icon == "https://example.com/old.png"
    assert feed.site_checked_at == clock.t  # not tried again on every pass


async def test_refresh_a_site_feed_that_was_never_looked_up(
    service: FeedService, db: Database
) -> None:
    feed = await add(service)
    db.update_feed(feed.id, post_as=PostAs.SITE)
    assert await service.refresh_site_identity(SERVER, feed.id) is True
    assert service.get_feed(SERVER, feed.id).site_name == "Example News"


# -- forum options --


async def test_forum_tags(service: FeedService, db: Database, clock: FakeClock) -> None:
    feed = await add(service, channel_kind=FORUM)
    assert (await service.set_forum_tags(SERVER, feed.id, [4, 4, 2], actor=ALEX)).forum_tag_ids == (
        4,
        2,
    )
    too_many = range(MAX_FORUM_TAGS + 1)
    assert "at most 5 tags" in await error(
        service.set_forum_tags(SERVER, feed.id, too_many, actor=ALEX)
    )
    assert service.get_feed(SERVER, feed.id).forum_tag_ids == (4, 2)
    assert (await service.set_forum_tags(SERVER, feed.id, [], actor=ALEX)).forum_tag_ids == ()


async def test_forum_tags_resume_a_feed_that_needed_one(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service, channel_kind=FORUM)
    db.update_feed(feed.id, paused=PauseReason.NEEDS_TAG)
    assert (
        await service.set_forum_tags(SERVER, feed.id, [], actor=ALEX)
    ).paused is PauseReason.NEEDS_TAG
    clock.t += 9
    resumed = await service.set_forum_tags(SERVER, feed.id, [4], actor=ALEX)
    assert (resumed.paused, resumed.next_check_at) == (None, clock.t)

    db.update_feed(feed.id, paused=PauseReason.MANUAL)
    assert (
        await service.set_forum_tags(SERVER, feed.id, [5], actor=ALEX)
    ).paused is PauseReason.MANUAL


async def test_forum_cover(service: FeedService) -> None:
    feed = await add(service, channel_kind=FORUM)
    assert feed.forum_cover is True
    assert (await service.set_forum_cover(SERVER, feed.id, False, actor=ALEX)).forum_cover is False
    assert (await service.set_forum_cover(SERVER, feed.id, True, actor=ALEX)).forum_cover is True


# -- pause, resume, remove, status --


async def test_pause_and_resume(service: FeedService, db: Database, clock: FakeClock) -> None:
    feed = await add(service)
    assert (await service.pause_feed(SERVER, feed.id, actor=ALEX)).paused is PauseReason.MANUAL
    assert db.due_feeds(clock.t + 10 * feed.interval_s) == []

    clock.t += 30
    resumed = await service.resume_feed(SERVER, feed.id, actor=ALEX)
    assert (resumed.paused, resumed.next_check_at) == (None, clock.t)
    assert db.due_feeds(clock.t) == [resumed]

    db.update_feed(feed.id, paused=PauseReason.LOST_CHANNEL)
    assert (await service.resume_feed(SERVER, feed.id, actor=ALEX)).paused is None
    db.update_feed(feed.id, paused=PauseReason.LOST_CHANNEL)
    assert (await service.pause_feed(SERVER, feed.id, actor=ALEX)).paused is PauseReason.MANUAL


async def test_resume_keeps_the_wait_of_a_rate_limited_feed(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service)
    booked = clock.t + 7200
    db.update_feed(
        feed.id, paused=PauseReason.MANUAL, rate_limited_since=clock.t, next_check_at=booked
    )
    resumed = await service.resume_feed(SERVER, feed.id, actor=ALEX)
    assert (resumed.paused, resumed.next_check_at) == (None, booked)

    # Once the wait is over the Feed is due at once, like any other.
    clock.t = booked + 60
    db.update_feed(feed.id, paused=PauseReason.MANUAL)
    assert (await service.resume_feed(SERVER, feed.id, actor=ALEX)).next_check_at == clock.t


async def test_editing_the_interval_keeps_the_wait_of_a_rate_limited_feed(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service, interval_s=3600)
    booked = clock.t + 6 * 3600
    db.update_feed(feed.id, rate_limited_since=clock.t, next_check_at=booked)

    edited, _ = await service.edit_feed(SERVER, feed.id, interval_s=300, actor=ALEX)

    assert (edited.interval_s, edited.next_check_at) == (300, booked)
    assert edited.rate_limited_since == clock.t

    # Not rate-limited, a shorter interval still brings the next Check forward.
    db.update_feed(feed.id, rate_limited_since=None, next_check_at=booked)
    edited, _ = await service.edit_feed(SERVER, feed.id, interval_s=600, actor=ALEX)
    assert edited.next_check_at == clock.t + 600


async def test_moving_a_feed_out_of_a_lost_channel_keeps_the_wait_of_a_rate_limited_feed(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service)
    booked = clock.t + 7200
    db.update_feed(
        feed.id, paused=PauseReason.LOST_CHANNEL, rate_limited_since=clock.t, next_check_at=booked
    )

    moved, _ = await service.edit_feed(
        SERVER, feed.id, channel_id=OTHER_CHANNEL, channel_kind=MESSAGES, actor=ALEX
    )

    assert (moved.paused, moved.next_check_at) == (None, booked)


async def test_tagging_a_feed_keeps_the_wait_of_a_rate_limited_feed(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service, channel_kind=FORUM)
    booked = clock.t + 7200
    db.update_feed(
        feed.id, paused=PauseReason.NEEDS_TAG, rate_limited_since=clock.t, next_check_at=booked
    )

    tagged = await service.set_forum_tags(SERVER, feed.id, [9], actor=ALEX)

    assert (tagged.paused, tagged.next_check_at) == (None, booked)


async def test_make_due_leaves_rate_limited_feeds_to_wait(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    plain = await add(service)
    limited = await add(service, URL2)
    booked = clock.t + 7200
    db.update_feed(limited.id, rate_limited_since=clock.t, next_check_at=booked)

    assert service.make_due(SERVER) == (1, 1)
    assert [due.id for due in db.due_feeds(clock.t)] == [plain.id]
    stored = db.get_feed(limited.id)
    assert stored is not None and stored.next_check_at == booked


async def test_add_and_a_new_address_count_as_checked(
    service: FeedService, db: Database, clock: FakeClock
) -> None:
    feed = await add(service)
    assert feed.last_checked_at == clock.t
    db.update_feed(feed.id, rate_limited_since=clock.t)
    clock.t += 50
    edited, _ = await service.edit_feed(SERVER, feed.id, url=URL2, actor=ALEX)
    assert (edited.last_checked_at, edited.rate_limited_since) == (clock.t, None)


async def test_remove(service: FeedService, db: Database) -> None:
    plain = await add(service)
    custom = await add(service, URL2, post_as=PostAs.CUSTOM, custom_name="Herald")
    await service.add_filters(
        SERVER, plain.id, FilterList.BLOCK, FilterField.ANY, ["w"], actor=ALEX
    )

    removed = await service.remove_feed(SERVER, plain.id, actor=ALEX)
    assert (removed.feed, removed.channel_id, removed.webhook_in_use) == (plain, CHANNEL, True)
    assert db.get_feed(plain.id) is None
    assert db.list_filters(plain.id) == []
    assert not db.has_seen_items(plain.id)

    removed = await service.remove_feed(SERVER, custom.id, actor=ALEX)
    assert (removed.channel_id, removed.webhook_in_use) == (CHANNEL, False)
    assert await error(service.remove_feed(SERVER, custom.id, actor=ALEX)) == NO_SUCH_FEED


async def test_status_line(service: FeedService, db: Database) -> None:
    feed = await add(service)
    assert status_line(feed) == "Working"
    assert service.status_line(feed) == "Working"
    assert status_of(feed) is FeedStatus.WORKING
    # The Checks succeed when an Item is given up on, so the Feed is not failing.
    lost = db.update_feed(feed.id, skipped_count=3, skipped_since=START + 9)
    assert status_line(lost) == f"Working, 3 Items could not be posted since <t:{START + 9}:R>"
    one = db.update_feed(feed.id, skipped_count=1)
    assert status_line(one) == f"Working, 1 Item could not be posted since <t:{START + 9}:R>"
    assert status_of(one) is FeedStatus.WORKING
    failing = db.update_feed(
        feed.id, fail_count=2, failing_since=START + 5, last_error="The site is down."
    )
    # A Check that fails now says more than Items lost earlier.
    assert status_line(failing) == "Failing: The site is down."
    assert status_of(failing) is FeedStatus.FAILING
    # The source's latest answer says more than what failed before it.
    limited = db.update_feed(feed.id, rate_limited_since=START + 7)
    assert status_line(limited) == "Rate limited"
    assert status_of(limited) is FeedStatus.RATE_LIMITED
    words = {
        PauseReason.MANUAL: "Paused: by a member",
        PauseReason.LOST_CHANNEL: "Paused: the bot can no longer post in its channel",
        PauseReason.NEEDS_TAG: "Paused: the forum requires a tag and the Feed has none",
    }
    for reason, line in words.items():
        paused = db.update_feed(feed.id, paused=reason)
        assert status_line(paused) == line  # even while failing or rate limited
        assert status_of(paused) is FeedStatus.PAUSED


# -- Filters --


async def test_add_filters(service: FeedService) -> None:
    feed = await add(service)
    added = await service.add_filters(
        SERVER,
        feed.id,
        FilterList.BLOCK,
        FilterField.TITLE,
        ["  breaking   news ", "", "   ", "Sport", "sport", "BREAKING news"],
        actor=ALEX,
    )
    assert [(f.list, f.field, f.word) for f in added] == [
        (FilterList.BLOCK, FilterField.TITLE, "breaking news"),
        (FilterList.BLOCK, FilterField.TITLE, "Sport"),
    ]
    again = await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.TITLE, ["SPORT", "weather"], actor=ALEX
    )
    assert [f.word for f in again] == ["weather"]
    # The same word for another field is another Filter, in either list.
    elsewhere = await service.add_filters(
        SERVER, feed.id, FilterList.MUST_HAVE, FilterField.ANY, ["sport"], actor=ALEX
    )
    other_field = await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.DESCRIPTION, ["sport"], actor=ALEX
    )
    assert len(elsewhere) == len(other_field) == 1
    assert service.list_filters(SERVER, feed.id) == [*added, *again, *elsewhere, *other_field]


async def test_a_word_cannot_be_in_both_lists_for_the_same_field(service: FeedService) -> None:
    feed = await add(service)
    block, must, title = FilterList.BLOCK, FilterList.MUST_HAVE, FilterField.TITLE
    await service.add_filters(SERVER, feed.id, block, title, ["Apple pie"], actor=ALEX)
    await service.add_filters(SERVER, feed.id, must, title, ["Pear"], actor=ALEX)
    before = service.list_filters(SERVER, feed.id)

    # Compared as repeats within a list are: spacing and capitals do not matter.
    said = await error(
        service.add_filters(
            SERVER, feed.id, must, title, ["cherry", "  APPLE   pie ", "plum"], actor=ALEX
        )
    )
    assert said == "“Apple pie” is already a block word for this Feed. Remove it there first."
    assert service.list_filters(SERVER, feed.id) == before  # not even the words that were fine

    said = await error(service.add_filters(SERVER, feed.id, block, title, ["pear"], actor=ALEX))
    assert said == "“Pear” is already a must-have word for this Feed. Remove it there first."
    assert service.list_filters(SERVER, feed.id) == before

    # Once it is removed from the other list it can be added.
    await service.remove_filter(SERVER, feed.id, before[0].id, actor=ALEX)
    added = await service.add_filters(SERVER, feed.id, must, title, ["apple pie"], actor=ALEX)
    assert [(f.list, f.word) for f in added] == [(must, "apple pie")]


async def test_filter_limits(service: FeedService) -> None:
    feed = await add(service)

    def block(words: list[str]) -> Awaitable[object]:
        return service.add_filters(
            SERVER, feed.id, FilterList.BLOCK, FilterField.ANY, words, actor=ALEX
        )

    assert "at most 100 characters" in await error(block(["fine", "w" * 101]))
    assert service.list_filters(SERVER, feed.id) == []
    await block(["w" * 100])

    assert "at most 100 Filters" in await error(block([f"word{n}" for n in range(MAX_FILTERS)]))
    assert len(service.list_filters(SERVER, feed.id)) == 1
    await block([f"word{n}" for n in range(MAX_FILTERS - 1)])
    assert len(service.list_filters(SERVER, feed.id)) == MAX_FILTERS
    assert await block(["word1", " "]) == []  # nothing new: nothing to refuse
    assert "at most 100 Filters" in await error(block(["one more"]))


async def test_remove_filter_only_from_its_own_feed(service: FeedService) -> None:
    feed = await add(service)
    other = await add(service, URL2)
    block, anywhere = FilterList.BLOCK, FilterField.ANY
    (mine,) = await service.add_filters(SERVER, feed.id, block, anywhere, ["a"], actor=ALEX)
    (theirs,) = await service.add_filters(SERVER, other.id, block, anywhere, ["b"], actor=ALEX)

    message = await error(service.remove_filter(SERVER, feed.id, theirs.id, actor=ALEX))
    assert message == "That Filter no longer exists."
    assert service.list_filters(SERVER, other.id) == [theirs]

    assert await service.remove_filter(SERVER, feed.id, mine.id, actor=ALEX) == mine
    assert service.list_filters(SERVER, feed.id) == []
    assert "no longer exists" in await error(
        service.remove_filter(SERVER, feed.id, mine.id, actor=ALEX)
    )


# -- OPML --


def opml(*entries: tuple[str, str]) -> bytes:
    return build_opml([OpmlEntry(title, url) for title, url in entries], "test")


async def test_import_a_mix(service: FeedService, db: Database, web: Web) -> None:
    await add(service)  # already in the channel
    web.listings["https://good.example/a"] = listing("1", "2", title="Good A")
    web.listings["https://good.example/b"] = listing(title="Good B")
    web.errors["https://down.example/feed"] = FetchError("The site is down.")
    web.unparsable["https://page.example/"] = ParseError("That is a web page.")
    web.unparsable["https://broken.example/"] = RuntimeError("boom")
    data = opml(
        ("Mine", "https://good.example/a"),
        ("Already here", URL),
        ("Down", "https://down.example/feed"),
        ("https://good.example/b", "https://good.example/b"),  # no title in the file
        ("Page", "https://page.example/"),
        ("Gone", "https://gone.example/feed"),
        ("Broken", "https://broken.example/"),
    )

    result = await service.import_opml(SERVER, CHANNEL, FORUM, data, actor=ALEX)

    assert result.added == ("Mine", "Good B")
    assert result.skipped == 1
    assert result.left_out == 0
    assert [(f.title, f.url, f.reason) for f in result.failed] == [
        ("Down", "https://down.example/feed", "The site is down."),
        ("Page", "https://page.example/", "That is a web page."),
        ("Gone", "https://gone.example/feed", "The address answered with error 404."),
        (
            "Broken",
            "https://broken.example/",
            "That address did not return a feed that can be read.",
        ),
    ]
    feeds = {feed.name: feed for feed in service.list_channel_feeds(SERVER, CHANNEL)}
    assert set(feeds) == {"Example News", "Mine", "Good B"}
    mine = feeds["Mine"]
    assert (mine.channel_kind, mine.interval_s) == (FORUM, DEFAULT_INTERVAL_S)
    assert mine.post_as is PostAs.BOT
    assert set(db.seen_states(mine.id, ["1", "2", STARTED_KEY])) == {"1", "2", STARTED_KEY}

    # The same file again adds nothing.
    again = await service.import_opml(SERVER, CHANNEL, FORUM, data, actor=ALEX)
    assert (again.added, again.skipped, len(again.failed)) == ((), 3, 4)


async def test_import_into_another_channel_is_not_a_duplicate(service: FeedService) -> None:
    await add(service)
    result = await service.import_opml(
        SERVER, OTHER_CHANNEL, MESSAGES, opml(("Again", URL)), actor=ALEX
    )
    assert (result.added, result.skipped) == (("Again",), 0)


async def test_import_an_unexpected_error_does_not_stop_the_others(
    service: FeedService, web: Web
) -> None:
    web.errors["https://odd.example/"] = RuntimeError("not a FetchError")
    result = await service.import_opml(
        SERVER, CHANNEL, MESSAGES, opml(("Odd", "https://odd.example/"), ("Fine", URL)), actor=ALEX
    )
    assert result.added == ("Fine",)
    assert [f.reason for f in result.failed] == ["Something went wrong. It has been logged."]


async def test_import_fetches_a_few_at_a_time_and_caps_the_new_feeds(
    service: FeedService, web: Web
) -> None:
    await add(service)
    entries = [("Already here", URL)]
    for number in range(MAX_IMPORT_FEEDS + 7):
        url = f"https://many.example/{number}"
        web.listings[url] = listing(f"k{number}", title=f"Feed {number}")
        entries.append((f"Feed {number}", url))

    result = await service.import_opml(SERVER, CHANNEL, MESSAGES, opml(*entries), actor=ALEX)

    assert result.added == tuple(f"Feed {number}" for number in range(MAX_IMPORT_FEEDS))
    assert (result.skipped, result.failed, result.left_out) == (1, (), 7)
    assert web.most_in_flight == IMPORT_CONCURRENCY
    assert len(service.list_feeds(SERVER)) == MAX_IMPORT_FEEDS + 1


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (b"", "empty"),
        (b"not xml at all", "not valid XML"),
        (b"<opml><body></body></opml>", "does not contain any feeds"),
        (b'<!DOCTYPE x><opml><body><outline xmlUrl="https://a.example/"/></body></opml>', "DOCT"),
    ],
)
async def test_import_refuses_a_bad_file(service: FeedService, data: bytes, said: str) -> None:
    assert said in await error(service.import_opml(SERVER, CHANNEL, MESSAGES, data, actor=ALEX))


async def test_export(service: FeedService) -> None:
    await add(service, name="B & <b>")
    await add(service, URL2, channel_id=OTHER_CHANNEL, name="a")
    entries = parse_opml(service.export_opml(SERVER))
    assert entries == [OpmlEntry("a", URL2), OpmlEntry("B & <b>", URL)]
    with pytest.raises(ServiceError, match="This Server has no Feeds yet"):
        service.export_opml(OTHER_SERVER)


# -- Log entries --


def logged(db: Database) -> list[LogEntry]:
    """The Server's Log entries, oldest first."""
    return db.list_log_entries(SERVER, limit=1000)[::-1]


def about(entry: LogEntry) -> tuple[int | None, str, int | None, str]:
    return entry.feed_id, entry.feed_name, entry.channel_id, entry.feed_url


def by(entry: LogEntry) -> tuple[int | None, str]:
    return entry.actor_id, entry.actor_name


async def test_adding_a_feed_is_saved_and_reported(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service)
    [entry] = logged(db)
    assert entry.kind is LogKind.FEED_ADDED
    assert by(entry) == (ALEX.id, "Alex")
    assert about(entry) == (feed.id, "Example News", CHANNEL, URL)
    assert (entry.server_id, entry.at, entry.changes, entry.detail) == (SERVER, START, (), "")
    assert reports.sent == []  # the reply does not wait for the report
    await journal.drain()
    assert reports.sent == [([entry], ALEX)]


async def test_a_feed_that_could_not_be_added_is_not_saved(
    service: FeedService, db: Database
) -> None:
    await error(service.add_feed(SERVER, CHANNEL, MESSAGES, "https://gone.example/", actor=ALEX))
    assert logged(db) == []


async def test_removing_a_feed_is_saved_with_the_feed_as_it_was(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service, name="Daily")
    await service.remove_feed(SERVER, feed.id, actor=SAM)
    entry = logged(db)[-1]
    assert (entry.kind, by(entry)) == (LogKind.FEED_REMOVED, (SAM.id, "Sam"))
    assert about(entry) == (feed.id, "Daily", CHANNEL, URL)
    await journal.drain()
    assert reports.sent[-1] == ([entry], SAM)


async def test_pausing_and_resuming_are_saved(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service)
    await service.pause_feed(SERVER, feed.id, actor=SAM)
    await service.resume_feed(SERVER, feed.id, actor=ALEX)
    _, paused, resumed = logged(db)
    assert (paused.kind, by(paused), about(paused)) == (
        LogKind.FEED_PAUSED,
        (SAM.id, "Sam"),
        (feed.id, feed.name, CHANNEL, URL),
    )
    assert (resumed.kind, by(resumed)) == (LogKind.FEED_RESUMED, (ALEX.id, "Alex"))
    await journal.drain()
    assert reports.sent[1:] == [([paused], SAM), ([resumed], ALEX)]


async def test_pausing_a_feed_a_member_paused_or_resuming_one_that_runs_saves_nothing(
    service: FeedService, db: Database
) -> None:
    feed = await add(service)
    await service.resume_feed(SERVER, feed.id, actor=SAM)
    await service.pause_feed(SERVER, feed.id, actor=ALEX)
    again = await service.pause_feed(SERVER, feed.id, actor=SAM)
    assert again.paused is PauseReason.MANUAL
    assert [(e.kind, e.actor_id) for e in logged(db)[1:]] == [(LogKind.FEED_PAUSED, ALEX.id)]


@pytest.mark.parametrize("reason", [PauseReason.LOST_CHANNEL, PauseReason.NEEDS_TAG])
async def test_a_feed_the_bot_paused_can_be_resumed_or_paused_by_a_member(
    service: FeedService, db: Database, reason: PauseReason
) -> None:
    feed = await add(service)
    db.update_feed(feed.id, paused=reason)
    await service.pause_feed(SERVER, feed.id, actor=SAM)  # now it is the member's pause
    db.update_feed(feed.id, paused=reason)
    await service.resume_feed(SERVER, feed.id, actor=SAM)
    kinds = [(e.kind, e.actor_id) for e in logged(db)[1:]]
    assert kinds == [(LogKind.FEED_PAUSED, SAM.id), (LogKind.FEED_RESUMED, SAM.id)]


async def test_an_edit_is_saved_with_each_thing_that_changed(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service, interval_s=600)
    await service.edit_feed(
        SERVER,
        feed.id,
        actor=SAM,
        name="Renamed",
        url=URL2,
        channel_id=OTHER_CHANNEL,
        channel_kind=MESSAGES,
        interval_s=3600,
    )
    entry = logged(db)[-1]
    assert (entry.kind, by(entry)) == (LogKind.FEED_EDITED, (SAM.id, "Sam"))
    assert about(entry) == (feed.id, "Renamed", OTHER_CHANNEL, URL2)  # the Feed as it is now
    assert entry.changes == (
        Change("Name", "Example News", "Renamed"),
        Change("Channel", f"<#{CHANNEL}>", f"<#{OTHER_CHANNEL}>"),
        Change("Address", URL, URL2),
        Change("Check interval", "10 minutes", "1 hour"),
    )
    assert entry.detail == ""
    await journal.drain()
    assert reports.sent[-1] == ([entry], SAM)


async def test_an_edit_names_only_what_changed(service: FeedService, db: Database) -> None:
    feed = await add(service, interval_s=600)
    await service.edit_feed(SERVER, feed.id, actor=SAM, name=feed.name, url=URL, interval_s=1800)
    assert logged(db)[-1].changes == (Change("Check interval", "10 minutes", "30 minutes"),)


async def test_an_edit_that_changes_nothing_saves_nothing(
    service: FeedService, db: Database
) -> None:
    feed = await add(service)
    await service.edit_feed(
        SERVER,
        feed.id,
        actor=SAM,
        name=feed.name,
        url=URL,
        channel_id=CHANNEL,
        channel_kind=MESSAGES,
        interval_s=feed.interval_s,
    )
    await service.edit_feed(SERVER, feed.id, actor=SAM)
    assert [e.kind for e in logged(db)] == [LogKind.FEED_ADDED]


async def test_moving_a_feed_the_bot_paused_is_an_edit_and_a_resume_in_one_report(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service)
    db.update_feed(feed.id, paused=PauseReason.LOST_CHANNEL)
    await service.edit_feed(
        SERVER, feed.id, actor=SAM, channel_id=OTHER_CHANNEL, channel_kind=MESSAGES
    )
    edited, resumed = logged(db)[1:]
    assert (edited.kind, resumed.kind) == (LogKind.FEED_EDITED, LogKind.FEED_RESUMED)
    assert by(resumed) == (SAM.id, "Sam")
    assert resumed.channel_id == OTHER_CHANNEL
    await journal.drain()
    assert reports.sent[-1] == ([edited, resumed], SAM)


TEMPLATE_CHANGES: list[tuple[Callable[[FeedService, int], Awaitable[object]], str]] = [
    (
        lambda s, f: s.set_text(SERVER, f, "{{title}} secret words", actor=SAM),
        "changed the message text",
    ),
    (lambda s, f: s.set_embed(SERVER, f, title="secret words", actor=SAM), "added the Embed"),
    (lambda s, f: s.add_field(SERVER, f, "secret", "words", actor=SAM), "added Field 1"),
    (lambda s, f: s.add_field(SERVER, f, "more", "words", actor=SAM), "added Field 2"),
    (lambda s, f: s.set_embed(SERVER, f, footer="secret words", actor=SAM), "changed the Embed"),
    (lambda s, f: s.remove_field(SERVER, f, 1, actor=SAM), "removed Field 1"),
    (lambda s, f: s.add_button(SERVER, f, "secret", "{{link}}", actor=SAM), "added Button 1"),
    (lambda s, f: s.add_button(SERVER, f, "words", "{{link}}", actor=SAM), "added Button 2"),
    (lambda s, f: s.remove_button(SERVER, f, 2, actor=SAM), "removed Button 2"),
    (lambda s, f: s.remove_embed(SERVER, f, actor=SAM), "removed the Embed"),
    (lambda s, f: s.reset_template(SERVER, f, actor=SAM), "reset the Template"),
    (
        lambda s, f: s.set_forum_title(SERVER, f, "{{title}} secret words", actor=SAM),
        "changed the Forum post title",
    ),
    (lambda s, f: s.set_forum_cover(SERVER, f, False, actor=SAM), "turned the Cover image off"),
    (lambda s, f: s.set_forum_cover(SERVER, f, True, actor=SAM), "turned the Cover image on"),
]


async def test_template_changes_are_saved_naming_only_the_part(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service, channel_kind=FORUM)
    for change, _ in TEMPLATE_CHANGES:
        await change(service, feed.id)
    entries = logged(db)[1:]
    assert [entry.detail for entry in entries] == [detail for _, detail in TEMPLATE_CHANGES]
    for entry in entries:
        assert (entry.kind, by(entry)) == (LogKind.TEMPLATE_CHANGED, (SAM.id, "Sam"))
        assert about(entry) == (feed.id, feed.name, CHANNEL, URL)
        assert entry.changes == ()  # no old or new text
        assert "secret" not in entry.detail and "words" not in entry.detail
    await journal.drain()
    assert len(reports.sent) == 1  # the Feed being added: these are saved, not reported


async def test_template_changes_that_change_nothing_save_nothing(
    service: FeedService, db: Database
) -> None:
    feed = await add(service, channel_kind=FORUM)
    await service.set_text(SERVER, feed.id, DEFAULT_TEXT_TEMPLATE, actor=SAM)
    await service.remove_embed(SERVER, feed.id, actor=SAM)
    await service.reset_template(SERVER, feed.id, actor=SAM)
    await service.set_forum_title(SERVER, feed.id, DEFAULT_FORUM_TITLE_TEMPLATE, actor=SAM)
    await service.set_forum_cover(SERVER, feed.id, feed.forum_cover, actor=SAM)
    await service.set_embed(SERVER, feed.id, title="t", actor=SAM)
    await service.set_embed(SERVER, feed.id, title="t", actor=SAM)
    await error(service.remove_field(SERVER, feed.id, 4, actor=SAM))
    assert [e.detail for e in logged(db)[1:]] == ["added the Embed"]


async def test_filters_are_saved(service: FeedService, db: Database, journal: Journal) -> None:
    feed = await add(service)
    await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.ANY, ["sponsored"], actor=SAM
    )
    several = await service.add_filters(
        SERVER, feed.id, FilterList.MUST_HAVE, FilterField.TITLE, ["linux", " ", "bsd"], actor=SAM
    )
    await service.remove_filter(SERVER, feed.id, several[0].id, actor=ALEX)
    # Blank and repeated words add nothing, so there is nothing to save.
    await service.add_filters(
        SERVER, feed.id, FilterList.BLOCK, FilterField.ANY, ["Sponsored", ""], actor=SAM
    )
    entries = logged(db)[1:]
    assert [entry.detail for entry in entries] == [
        'added block Filter "sponsored"',
        'added must-have Filters "linux", "bsd" (title only)',
        'removed must-have Filter "linux" (title only)',
    ]
    assert {entry.kind for entry in entries} == {LogKind.FILTER_CHANGED}
    assert [entry.actor_id for entry in entries] == [SAM.id, SAM.id, ALEX.id]
    assert all(about(entry) == (feed.id, feed.name, CHANNEL, URL) for entry in entries)


async def test_post_as_is_saved_as_the_member_sees_it(service: FeedService, db: Database) -> None:
    feed = await add(service)
    custom = {"custom_name": "Newsdesk", "custom_avatar": "https://cdn.example/a.png"}
    await service.set_post_as(SERVER, feed.id, PostAs.BOT, actor=SAM)  # as it was
    await service.set_post_as(SERVER, feed.id, PostAs.CUSTOM, actor=SAM, **custom)
    await service.set_post_as(SERVER, feed.id, PostAs.CUSTOM, actor=SAM, **custom)  # as it was
    custom["custom_avatar"] = "https://cdn.example/b.png"
    await service.set_post_as(SERVER, feed.id, PostAs.CUSTOM, actor=SAM, **custom)
    await service.set_post_as(SERVER, feed.id, PostAs.BOT, actor=ALEX)
    entries = logged(db)[1:]
    shown = "A custom name and picture (Newsdesk)"
    assert [(entry.changes, entry.detail) for entry in entries] == [
        ((Change("Post as", "The bot", shown),), ""),
        ((), "changed the picture"),
        ((Change("Post as", shown, "The bot"),), ""),
    ]
    assert {entry.kind for entry in entries} == {LogKind.POST_AS_CHANGED}
    assert [entry.actor_id for entry in entries] == [SAM.id, SAM.id, ALEX.id]


async def test_mentions_and_forum_tags_are_saved(service: FeedService, db: Database) -> None:
    feed = await add(service, channel_kind=FORUM)
    await service.set_mentions(SERVER, feed.id, [11, 12], actor=SAM)
    await service.set_mentions(SERVER, feed.id, [11, 12], actor=SAM)  # as it was
    await service.set_mentions(SERVER, feed.id, [11], actor=SAM)
    await service.set_mentions(SERVER, feed.id, [], actor=SAM)
    await service.set_forum_tags(SERVER, feed.id, [21], actor=ALEX)
    await service.set_forum_tags(SERVER, feed.id, [21], actor=ALEX)  # as it was
    await service.set_forum_tags(SERVER, feed.id, [21, 22], actor=ALEX)
    await service.set_forum_tags(SERVER, feed.id, [], actor=ALEX)
    assert [(e.kind, e.actor_id, e.detail) for e in logged(db)[1:]] == [
        (LogKind.MENTIONS_CHANGED, SAM.id, "now mentions 2 roles"),
        (LogKind.MENTIONS_CHANGED, SAM.id, "now mentions 1 role"),
        (LogKind.MENTIONS_CHANGED, SAM.id, "now mentions no roles"),
        (LogKind.FORUM_TAGS_CHANGED, ALEX.id, "now puts 1 tag on its Forum posts"),
        (LogKind.FORUM_TAGS_CHANGED, ALEX.id, "now puts 2 tags on its Forum posts"),
        (LogKind.FORUM_TAGS_CHANGED, ALEX.id, "now puts no tags on its Forum posts"),
    ]


async def test_a_tag_that_sets_a_paused_feed_going_again_is_also_a_resume(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    feed = await add(service, channel_kind=FORUM)
    db.update_feed(feed.id, paused=PauseReason.NEEDS_TAG)
    await service.set_forum_tags(SERVER, feed.id, [21], actor=SAM)
    tags, resumed = logged(db)[1:]
    assert (tags.kind, resumed.kind) == (LogKind.FORUM_TAGS_CHANGED, LogKind.FEED_RESUMED)
    assert by(resumed) == (SAM.id, "Sam")
    await journal.drain()
    assert [e.kind for e in reports.sent[-1][0]] == [LogKind.FEED_RESUMED]  # tags are not reported


async def test_an_import_saves_an_entry_per_feed_added_and_is_one_report(
    service: FeedService, db: Database, web: Web, journal: Journal, reports: Reports
) -> None:
    await add(service)
    await journal.drain()
    reports.sent.clear()
    web.listings["https://third.example/rss"] = listing("t", title="Third")
    data = opml(
        ("Already here", URL),
        ("Other", URL2),
        ("Broken", "https://gone.example/rss"),
        ("Third", "https://third.example/rss"),
    )
    result = await service.import_opml(SERVER, CHANNEL, MESSAGES, data, actor=SAM)
    assert (result.added, result.skipped, len(result.failed)) == (("Other", "Third"), 1, 1)

    entries = logged(db)[1:]
    assert [(e.kind, e.feed_name, e.feed_url) for e in entries] == [
        (LogKind.FEED_ADDED, "Other", URL2),
        (LogKind.FEED_ADDED, "Third", "https://third.example/rss"),
    ]
    assert all(by(entry) == (SAM.id, "Sam") and entry.channel_id == CHANNEL for entry in entries)
    assert {entry.feed_id for entry in entries} == {f.id for f in db.list_feeds(SERVER)[1:]}
    await journal.drain()
    assert reports.sent == [(entries, SAM)]


async def test_an_import_that_adds_nothing_saves_and_reports_nothing(
    service: FeedService, db: Database, journal: Journal, reports: Reports
) -> None:
    data = opml(("Broken", "https://gone.example/rss"))
    result = await service.import_opml(SERVER, CHANNEL, MESSAGES, data, actor=SAM)
    assert result.added == ()
    await journal.drain()
    assert (logged(db), reports.sent) == ([], [])


async def test_what_no_member_changed_is_not_saved(
    service: FeedService, db: Database, web: Web, clock: FakeClock
) -> None:
    feed = await add(service)
    web.pages["https://example.com/"] = b"<html><title>Example</title></html>"
    db.update_feed(feed.id, post_as=PostAs.SITE)
    assert await service.refresh_site_identity(SERVER, feed.id) is True  # the bot's own doing
    service.make_due(SERVER)  # Refresh
    await service.preview(SERVER, feed.id)
    service.export_opml(SERVER)
    assert [e.kind for e in logged(db)] == [LogKind.FEED_ADDED]


class UnsavingJournal(Journal):
    """A journal whose database cannot be written."""

    def record(self, *args: Any, **kwargs: Any) -> LogEntry:
        raise sqlite3.OperationalError("disk I/O error")

    def record_many(self, *args: Any, **kwargs: Any) -> list[LogEntry]:
        raise sqlite3.OperationalError("disk I/O error")


async def test_a_log_entry_that_cannot_be_saved_does_not_fail_what_was_done(
    db: Database,
    web: Web,
    clock: FakeClock,
    reports: Reports,
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal = UnsavingJournal(db, clock, reports)
    service = FeedService(db, web, clock, journal, parse=web.parse, rand=lambda: 0.0)

    feed = await add(service)
    paused = await service.pause_feed(SERVER, feed.id, actor=SAM)
    assert paused.paused is PauseReason.MANUAL
    edited, _ = await service.edit_feed(SERVER, feed.id, actor=SAM, name="Renamed")
    assert edited.name == "Renamed"
    result = await service.import_opml(SERVER, CHANNEL, MESSAGES, opml(("Other", URL2)), actor=SAM)
    assert result.added == ("Other",)
    removed = await service.remove_feed(SERVER, feed.id, actor=SAM)
    assert removed.feed.id == feed.id and db.get_feed(feed.id) is None

    await journal.drain()
    assert (logged(db), reports.sent) == ([], [])
    assert caplog.text.count("its Log entry could not be saved") == 5


@pytest.mark.parametrize(
    ("reason", "words"),
    [
        (PauseReason.LOST_CHANNEL, "the bot can no longer post in its channel"),
        (PauseReason.NEEDS_TAG, "the forum requires a tag and the Feed has none"),
    ],
)
async def test_a_member_pausing_a_feed_the_bot_paused_says_so_in_the_history(
    service: FeedService, db: Database, journal: Journal, reason: PauseReason, words: str
) -> None:
    feed = await add(service)
    paused_by_bot = db.update_feed(feed.id, paused=reason)
    journal.record_feed(
        Actor.bot(), LogKind.FEED_AUTO_PAUSED, paused_by_bot, detail=f"Bot reason: {words}."
    )
    await service.pause_feed(SERVER, feed.id, actor=SAM)
    _, bot_entry, member_entry = logged(db)
    assert (bot_entry.kind, bot_entry.actor_id) == (LogKind.FEED_AUTO_PAUSED, None)
    assert words in bot_entry.detail
    assert (member_entry.kind, by(member_entry)) == (LogKind.FEED_PAUSED, (SAM.id, "Sam"))
    assert "Replaced the bot's pause" in member_entry.detail
    assert words in member_entry.detail
