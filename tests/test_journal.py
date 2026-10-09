from __future__ import annotations

import asyncio
import gc
import logging
import sqlite3
from collections.abc import Callable, Iterator

import pytest

from rssbot.db import Database
from rssbot.journal import Journal, log_line
from rssbot.models import Actor, Change, ChannelKind, Feed, LogEntry, LogKind

SERVER = 123
ALEX = Actor(987, "alex", "https://cdn.example/alex.png")
NOW = 5000


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        raise AssertionError("the Journal never sleeps")


class FakeNotifier:
    def __init__(self) -> None:
        self.reports: list[tuple[int, list[LogEntry], Actor]] = []
        self.error: BaseException | None = None
        self.hang = False
        self.deaf = False  # hangs and swallows its cancellation
        self.cancelled = 0

    async def notify(self, server_id: int, text: str) -> None:
        raise AssertionError("the Journal reports Log entries, not notes")

    async def announce(self, server_id: int, entries: list[LogEntry], actor: Actor) -> None:
        self.reports.append((server_id, list(entries), actor))
        while self.hang or self.deaf:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                if not self.deaf:
                    raise
        if self.error is not None:
            raise self.error


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def journal(db: Database, clock: Clock, notifier: FakeNotifier) -> Journal:
    return Journal(db, clock, notifier, announce_timeout_s=0.05)


@pytest.fixture
def lines(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    """Call it for the container log lines the Journal has written for Log entries so far."""
    caplog.set_level(logging.INFO, logger="rssbot.journal")
    return lambda: [
        r.getMessage()
        for r in caplog.records
        if r.name == "rssbot.journal" and r.levelno in (logging.INFO, logging.WARNING)
        and r.getMessage().split(" ")[0] in {kind.value for kind in LogKind}
    ]


def make_feed(db: Database, name: str = "BBC News", server_id: int = SERVER) -> Feed:
    return db.create_feed(
        server_id=server_id,
        channel_id=678,
        channel_kind=ChannelKind.MESSAGES,
        name=name,
        url="https://example.com/rss",
        now=0,
    )


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# -- saving --


def test_record_saves_the_entry_dated_now(db: Database, journal: Journal) -> None:
    changes = [Change("Check interval", "10 minutes", "30 minutes")]
    entry = journal.record(
        SERVER,
        ALEX,
        LogKind.FEED_EDITED,
        feed_id=45,
        feed_name="BBC News",
        channel_id=678,
        feed_url="https://example.com/rss",
        changes=changes,
        detail="removed Button 2",
    )
    assert entry == LogEntry(
        id=entry.id,
        server_id=SERVER,
        at=NOW,
        actor_id=987,
        actor_name="alex",
        kind=LogKind.FEED_EDITED,
        feed_id=45,
        feed_name="BBC News",
        channel_id=678,
        feed_url="https://example.com/rss",
        changes=tuple(changes),
        detail="removed Button 2",
    )
    assert db.list_log_entries(SERVER, limit=10) == [entry]


def test_record_feed_takes_name_channel_and_address_from_the_feed(
    db: Database, journal: Journal
) -> None:
    feed = make_feed(db)
    entry = journal.record_feed(ALEX, LogKind.FEED_PAUSED, feed, detail="for the holidays")
    assert (entry.server_id, entry.feed_id, entry.feed_name) == (SERVER, feed.id, "BBC News")
    assert (entry.channel_id, entry.feed_url) == (678, "https://example.com/rss")
    assert (entry.kind, entry.detail, entry.changes) == (
        LogKind.FEED_PAUSED,
        "for the holidays",
        (),
    )
    assert db.feed_attribution(feed.id).added is None
    assert db.list_log_entries(SERVER, feed_id=feed.id, limit=10) == [entry]


def test_record_writes_one_line_to_the_container_log(
    db: Database, journal: Journal, lines: Callable[[], list[str]]
) -> None:
    feed = make_feed(db)
    journal.record_feed(ALEX, LogKind.FEED_PAUSED, feed)
    assert lines() == [
        f'feed.pause server=123 feed={feed.id} name="BBC News" channel=678 by="alex" by_id=987'
    ]


@pytest.mark.parametrize("kind", list(LogKind))
def test_only_the_bots_problem_reports_are_written_at_warning(
    db: Database, journal: Journal, caplog: pytest.LogCaptureFixture, kind: LogKind
) -> None:
    caplog.set_level(logging.INFO, logger="rssbot.journal")
    feed = make_feed(db)
    journal.record_feed(ALEX, kind, feed)
    journal.record_many(ALEX, kind, [feed])
    problem = kind in (LogKind.FEED_AUTO_PAUSED, LogKind.FEED_BROKEN)
    expected = logging.WARNING if problem else logging.INFO
    assert [r.levelno for r in caplog.records] == [expected, expected]


def test_the_bot_as_actor_is_by_bot(
    db: Database, journal: Journal, lines: Callable[[], list[str]]
) -> None:
    feed = make_feed(db)
    entry = journal.record_feed(
        Actor.bot(), LogKind.FEED_AUTO_PAUSED, feed, detail="the bot can no longer post there"
    )
    assert (entry.actor_id, entry.actor_name) == (None, "")
    assert lines() == [
        f'feed.auto_pause server=123 feed={feed.id} name="BBC News" channel=678 by=bot '
        'detail="the bot can no longer post there"'
    ]
    assert db.list_log_entries(SERVER, by_bot=True, limit=10) == [entry]


def test_a_line_leaves_out_what_it_has_nothing_to_say_about(
    journal: Journal, lines: Callable[[], list[str]]
) -> None:
    journal.record(SERVER, ALEX, LogKind.GRANT_GIVEN, detail="Manager to <@&5>")
    journal.record(
        SERVER,
        ALEX,
        LogKind.LOGS_CHANNEL_CHANGED,
        channel_id=9,
        changes=[Change("Logs channel", "", "#logs"), Change("x", "1", "2")],
    )
    assert lines() == [
        'grant.give server=123 by="alex" by_id=987 detail="Manager to <@&5>"',
        'logs_channel.change server=123 channel=9 by="alex" by_id=987 '
        'changes="Logs channel:  -> #logs; x: 1 -> 2"',
    ]


@pytest.mark.parametrize(
    "nasty",
    [
        'News" by="admin" by_id=1',
        "News\nfeed.remove server=1 by=bot",
        "News\r\nINFO rssbot.journal: feed.remove server=1",
        "back\\slash\\",
        'quote at the end"',
        "line separator paragraph\x85nel\x0bvt\x0cff\x1b[31m\x00\x7f",
    ],
)
def test_no_name_can_break_or_forge_a_log_line(
    db: Database, journal: Journal, lines: Callable[[], list[str]], nasty: str
) -> None:
    feed = make_feed(db, name=nasty)
    journal.record_feed(Actor(987, nasty), LogKind.FEED_EDITED, feed, detail=nasty)
    (line,) = lines()
    assert line.splitlines() == [line] and line.isprintable()  # one line, nothing invisible

    # Read back the way a log reader would: outside quotes only the bot's own pairs exist.
    values, outside, inside, position = [], "", None, 0
    while position < len(line):
        char = line[position]
        if inside is None:
            if char == '"':
                inside = ""
            else:
                outside += char
        elif char == "\\":
            position += 1
            inside += line[position]
        elif char == '"':
            values.append(inside)
            inside = None
        else:
            inside += char
        position += 1
    assert inside is None  # every quote is closed
    assert outside == f"feed.edit server=123 feed={feed.id} name= channel=678 by= by_id=987 detail="
    assert len(values) == 3
    if nasty.isprintable():
        assert values == [nasty, nasty, nasty]  # and nothing was lost


def test_unusual_but_harmless_names_are_kept_readable(
    journal: Journal, lines: Callable[[], list[str]]
) -> None:
    journal.record(SERVER, Actor(1, "Zoë 🦊"), LogKind.FEED_ADDED, feed_name="Le Monde – À la une")
    assert lines() == ['feed.add server=123 name="Le Monde – À la une" by="Zoë 🦊" by_id=1']


def test_log_line_escapes_characters_beyond_the_basic_plane() -> None:
    entry = LogEntry(1, SERVER, 0, None, "", LogKind.FEED_BROKEN, feed_name="a\U000e0001b")
    assert log_line(entry) == 'feed.broken server=123 name="a\\U000e0001b" by=bot'


def test_record_many_saves_one_entry_and_one_line_per_feed(
    db: Database, journal: Journal, lines: Callable[[], list[str]]
) -> None:
    feeds = [make_feed(db, name=f"Feed {n}") for n in range(3)]
    entries = journal.record_many(ALEX, LogKind.FEED_ADDED, iter(feeds), detail="OPML import")
    assert [e.feed_id for e in entries] == [f.id for f in feeds]
    assert {(e.at, e.actor_id, e.kind, e.detail) for e in entries} == {
        (NOW, 987, LogKind.FEED_ADDED, "OPML import")
    }
    assert db.list_log_entries(SERVER, limit=10) == entries[::-1]
    assert len(lines()) == 3 and all(line.startswith("feed.add ") for line in lines())
    assert {i: a.added for i, a in db.feed_attributions(SERVER).items()} == {
        e.feed_id: e for e in entries
    }
    assert journal.record_many(ALEX, LogKind.FEED_ADDED, []) == []


def test_a_failed_save_raises_and_logs_nothing(
    db: Database, journal: Journal, lines: Callable[[], list[str]]
) -> None:
    db.close()
    with pytest.raises(sqlite3.Error):
        journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    assert lines() == []


async def test_saving_does_not_depend_on_a_logs_channel_or_a_notifier(
    db: Database, clock: Clock
) -> None:
    journal = Journal(db, clock)  # no notifier at all
    entry = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    assert db.get_server(SERVER).logs_channel_id is None  # the Server was created along the way
    await journal.announce(entry, ALEX)
    journal.announce_soon([entry], ALEX)
    await journal.drain()
    assert db.list_log_entries(SERVER, limit=10) == [entry]


# -- reporting --


ANNOUNCED = [
    LogKind.FEED_ADDED,
    LogKind.FEED_REMOVED,
    LogKind.FEED_PAUSED,
    LogKind.FEED_RESUMED,
    LogKind.FEED_EDITED,
    LogKind.GRANT_GIVEN,
    LogKind.GRANT_TAKEN,
    LogKind.LOGS_CHANNEL_CHANGED,
    LogKind.FEED_AUTO_PAUSED,
    LogKind.FEED_BROKEN,
    LogKind.FEED_WORKING_AGAIN,
]


def test_which_kinds_are_announced() -> None:
    assert [kind for kind in LogKind if kind.announced] == ANNOUNCED
    assert {kind for kind in LogKind if not kind.announced} == {
        LogKind.TEMPLATE_CHANGED,
        LogKind.FILTER_CHANGED,
        LogKind.POST_AS_CHANGED,
        LogKind.MENTIONS_CHANGED,
        LogKind.FORUM_TAGS_CHANGED,
    }


@pytest.mark.parametrize("kind", list(LogKind))
async def test_announce_posts_only_announced_kinds(
    journal: Journal, notifier: FakeNotifier, kind: LogKind
) -> None:
    entry = journal.record(SERVER, ALEX, kind)
    await journal.announce(entry, ALEX)
    assert notifier.reports == ([(SERVER, [entry], ALEX)] if kind.announced else [])


async def test_entries_announced_together_are_one_report(
    db: Database, journal: Journal, notifier: FakeNotifier
) -> None:
    entries = journal.record_many(
        ALEX, LogKind.FEED_ADDED, [make_feed(db, name=f"F{n}") for n in range(50)]
    )
    small = journal.record(SERVER, ALEX, LogKind.TEMPLATE_CHANGED)
    await journal.announce([*entries, small], ALEX)
    assert notifier.reports == [(SERVER, entries, ALEX)]  # one call, without the saved-only one

    await journal.announce([], ALEX)
    await journal.announce((small,), ALEX)
    assert len(notifier.reports) == 1


async def test_entries_of_two_servers_are_reported_to_each(
    journal: Journal, notifier: FakeNotifier
) -> None:
    here = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    there = journal.record(999, ALEX, LogKind.FEED_ADDED)
    await journal.announce([here, there], ALEX)
    assert notifier.reports == [(SERVER, [here], ALEX), (999, [there], ALEX)]


async def test_a_notifier_that_raises_costs_only_the_report(
    journal: Journal, notifier: FakeNotifier, caplog: pytest.LogCaptureFixture
) -> None:
    notifier.error = RuntimeError("no such channel")
    entry = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    await journal.announce(entry, ALEX)
    assert any("Could not report" in text and "123" in text for text in warnings(caplog))

    notifier.announce = lambda server_id, entries, actor: None  # type: ignore[assignment]
    await journal.announce(entry, ALEX)  # not even awaitable: still nothing raised
    assert len(warnings(caplog)) == 2


async def test_a_notifier_that_hangs_is_dropped_after_the_timeout(
    journal: Journal, notifier: FakeNotifier, caplog: pytest.LogCaptureFixture
) -> None:
    notifier.hang = True
    entry = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    await asyncio.wait_for(journal.announce(entry, ALEX), timeout=2)
    assert any("took too long" in text for text in warnings(caplog))
    await asyncio.sleep(0)
    assert notifier.cancelled == 1  # the hung call was cancelled, not left running


async def test_a_notifier_that_ignores_cancellation_cannot_hold_the_journal(
    journal: Journal, notifier: FakeNotifier, caplog: pytest.LogCaptureFixture
) -> None:
    notifier.deaf = True
    entry = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    await asyncio.wait_for(journal.announce(entry, ALEX), timeout=2)
    assert any("took too long" in text for text in warnings(caplog))
    notifier.deaf = False  # let the stray task end with the test


async def test_cancelling_announce_cancels_the_notifier_call(
    journal: Journal, notifier: FakeNotifier
) -> None:
    notifier.hang = True
    journal = Journal(journal._db, journal._clock, notifier, announce_timeout_s=60)
    entry = journal.record(SERVER, ALEX, LogKind.FEED_ADDED)
    task = asyncio.create_task(journal.announce(entry, ALEX))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert notifier.cancelled == 1


async def test_announce_soon_returns_at_once_and_drain_waits(
    db: Database, clock: Clock, notifier: FakeNotifier
) -> None:
    journal = Journal(db, clock, notifier, announce_timeout_s=60)
    notifier.hang = True
    entry = journal.record(SERVER, ALEX, LogKind.FEED_PAUSED)
    journal.announce_soon(entry, ALEX)
    assert notifier.reports == []  # nothing has run yet: the caller was not kept waiting
    assert len(journal._announcing) == 1  # and the task is held on to

    drained = asyncio.create_task(journal.drain())
    await asyncio.sleep(0.01)
    assert notifier.reports == [(SERVER, [entry], ALEX)] and not drained.done()
    notifier.hang = False
    for task in journal._announcing:
        task.cancel()  # stands in for Discord answering at last
    await asyncio.wait_for(drained, timeout=2)
    assert journal._announcing == set()


async def test_announce_soon_survives_garbage_collection_and_failures(
    journal: Journal, notifier: FakeNotifier
) -> None:
    notifier.error = RuntimeError("boom")
    entries = [journal.record(SERVER, ALEX, LogKind.FEED_ADDED) for _ in range(3)]
    for entry in entries:
        journal.announce_soon(entry, ALEX)
    gc.collect()
    await journal.drain()
    assert [report[1] for report in notifier.reports] == [[e] for e in entries]
    assert journal._announcing == set()
    await journal.drain()  # nothing outstanding: returns at once


async def test_announce_soon_starts_nothing_for_saved_only_kinds(
    journal: Journal, notifier: FakeNotifier
) -> None:
    journal.announce_soon(journal.record(SERVER, ALEX, LogKind.FILTER_CHANGED), ALEX)
    assert journal._announcing == set()
    await journal.drain()
    assert notifier.reports == []


def test_a_line_names_only_the_site_of_a_changed_address(
    journal: Journal, lines: Callable[[], list[str]]
) -> None:
    journal.record(
        SERVER,
        ALEX,
        LogKind.FEED_EDITED,
        changes=[
            Change(
                "Address", "https://a.example/feed?key=s3cret", "HTTP://B.example:8080/p/s3cret"
            ),
            Change("Name", "http is a protocol", "https://"),
        ],
    )
    [line] = lines()
    assert line.endswith(
        'changes="Address: https://a.example/… -> http://b.example/…; '
        'Name: http is a protocol -> (an address)"'
    )
    assert "s3cret" not in line
    [saved] = journal._db.list_log_entries(SERVER, limit=1)
    assert saved.changes[0].before == "https://a.example/feed?key=s3cret"  # the entry keeps it
