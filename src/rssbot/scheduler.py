"""The Check loop: fetch every due Feed, work out its new Items and post them.

The bot exists because its predecessor stopped posting everything when one Item failed
(docs/adr/0002). So the rule here is that trouble stays where it started: an Item that
cannot be posted is skipped, a Feed that cannot be checked is tried again later, and
neither ever stops, holds up or spoils another Item, another Feed or the loop itself.
The comments marked "Boundary" say where each kind of trouble is stopped, and why there.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from .db import Database, FeedNotFound
from .filters import passes
from .journal import Journal, quote
from .models import (
    CATCH_UP_LIMIT,
    MAX_DELIVERY_ATTEMPTS,
    MIN_INTERVAL_S,
    Actor,
    Feed,
    Filter,
    Item,
    ItemStatus,
    LogKind,
    OutgoingMessage,
    ParsedFeed,
    PauseReason,
)
from .ports import (
    Clock,
    Deliverer,
    DeliveryOutcome,
    Fetcher,
    FetchError,
    ParseFn,
    RenderFn,
)

log = logging.getLogger(__name__)

MAX_BACKOFF_S = 60 * 60  # where the doubling of a failing Feed's wait stops
WARN_AFTER_S = 24 * 60 * 60  # how long a Feed fails before its Server is told
SEEN_KEEP_S = 30 * 24 * 60 * 60  # a Seen item is forgotten this long after the source drops it
SERVER_KEEP_S = 30 * 24 * 60 * 60  # a removed Server's data is kept this long
PURGE_EVERY_S = 60 * 60
LOG_KEEP_S = 365 * 24 * 60 * 60  # a Log entry is deleted a year after it was saved
PRUNE_EVERY_S = 24 * 60 * 60
HOLD_OFF_S = MIN_INTERVAL_S  # before a Check that broke, rather than failed, is tried again
SLOW_DOWN_MIN_S = 60  # the least a Feed waits after its source asked for fewer requests
# What one Item's delivery may need: a Cover image download (30 s at most) and the send.
ITEM_ALLOWANCE_S = 45.0
JITTER = 0.1  # every wait is stretched by up to this share of itself
MAX_ERROR_CHARS = 300

# A Seen item that stands for "this Feed has had its first Check". Without it a Feed whose
# source listed nothing at first, or nothing for 30 days, would have no Seen items and so
# would take the next Items it sees for its starting point and never post them.
STARTED_KEY = "(first check done)"


class SystemClock:
    """The real time."""

    def now(self) -> int:
        return int(time.time())

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _CheckFailed(Exception):
    """A Check failed for a reason a Manager can be told. The message becomes last_error."""

    def __init__(self, message: str, *, wait_s: int = 0, slow_down: bool = False) -> None:
        super().__init__(message)
        self.wait_s = wait_s  # the source asked not to be fetched again for this long
        self.slow_down = slow_down  # the source asked for fewer requests; nothing is broken


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What a Check that did not fail leaves to be stored."""

    feed: Feed  # as last read: it may have been edited while the fetch was under way
    etag: str | None
    last_modified: str | None
    title: str | None = None  # None: the source said "not modified", so it was not read
    link: str | None = None
    pause: PauseReason | None = None
    posting_again: bool = False  # Items were posted and none was given up on
    posted: int = 0  # Items delivered by this Check
    skipped: int = 0  # Items skipped by this Check: given up on, or beyond Catch-up
    not_modified: bool = False  # the source said nothing had changed


@dataclass(slots=True)
class _Tally:
    """What one Check did with its Items."""

    delivered: int = 0
    given_up: int = 0  # deliveries given up on; Items beyond Catch-up are not counted
    beyond: int = 0  # new Items skipped because they were beyond Catch-up


def _backoff(interval_s: int, failures: int) -> int:
    """How long to wait after this many failed Checks in a row.

    The Feed's interval, doubling up to an hour. A Feed that is checked less often than
    hourly anyway keeps its interval.
    """
    doubled = interval_s * 2 ** min(max(failures, 1) - 1, 16)
    return max(interval_s, min(doubled, MAX_BACKOFF_S))


def _asked_wait(error: FetchError) -> int:
    """For how many seconds the source asked to be left alone (Retry-After), or 0."""
    try:
        seconds = float(getattr(error, "retry_after", None) or 0)
    except (TypeError, ValueError):
        return 0
    return math.ceil(seconds) if math.isfinite(seconds) and seconds > 0 else 0


def _places(items: Sequence[Item]) -> dict[str, int]:
    """Each Item's place from the newest (0) to the oldest, by key.

    By `published`. An Item without one takes the date of the Item listed before it (at
    the top: after it), which keeps it where the source put it. Equal dates, and sources
    that give no dates at all, fall back to the source's own order: usually newest first.
    """
    dates = [item.published if isinstance(item.published, int) else None for item in items]
    for index in range(1, len(dates)):
        if dates[index] is None:
            dates[index] = dates[index - 1]
    for index in range(len(dates) - 2, -1, -1):
        if dates[index] is None:
            dates[index] = dates[index + 1]
    order = sorted(range(len(items)), key=lambda index: (-(dates[index] or 0), index))
    return {items[index].key: place for place, index in enumerate(order)}


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Look at a finished future's exception, so asyncio does not report it as lost."""
    if not future.cancelled():
        future.exception()


class Scheduler:
    """Checks every Feed when it is due and posts its new Items."""

    def __init__(
        self,
        db: Database,
        fetcher: Fetcher,
        deliverer: Deliverer,
        journal: Journal,
        clock: Clock,
        parse: ParseFn,  # synchronous and possibly slow: run in a thread
        render: RenderFn,
        render_default: RenderFn,
        *,
        tick_s: float = 30.0,
        max_concurrent: int = 5,
        check_timeout_s: float = 120.0,  # fetch, parse and every delivery of one Check
        item_allowance_s: float = ITEM_ALLOWANCE_S,  # no Item is started with less left
        grace_s: float = 5.0,  # for a cancelled Check to stop before it is left behind
        restart_delay_s: float = 5.0,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._db = db
        self._fetcher = fetcher
        self._deliverer = deliverer
        self._journal = journal
        self._clock = clock
        self._parse = parse
        self._render = render
        self._render_default = render_default
        self._tick_s = tick_s
        self._max_concurrent = max(1, max_concurrent)
        self._check_timeout_s = check_timeout_s
        self._item_allowance_s = item_allowance_s
        self._grace_s = grace_s
        self._restart_delay_s = restart_delay_s
        self._rand = rand

        self._slots = asyncio.Semaphore(self._max_concurrent)
        self._checking: set[int] = set()  # Feeds with a Check under way or waiting for a slot
        self._stuck: dict[int, asyncio.Future[Any]] = {}  # Feeds with work that would not stop
        self._held: dict[int, int] = {}  # Feeds whose Check broke: not again before this time
        self._purged_at: int | None = None
        self._pruned_at: int | None = None

    # -- the loop --

    async def run_supervised(self) -> None:
        """run(), started again whenever it stops. This is what the bot runs.

        Ends only by cancellation.
        """
        while True:
            try:
                await self.run()
                log.error("The Check loop ended by itself; starting it again")
            except Exception:
                log.exception("The Check loop stopped unexpectedly; starting it again")
            # Not the injected clock: that may be what broke.
            await asyncio.sleep(self._restart_delay_s)

    async def run(self) -> None:
        """Tick until cancelled."""
        while True:
            await self.tick()
            await self._clock.sleep(self._tick_s)
            # A tick with nothing due never waits, so without this a clock that returned at
            # once would leave no point at which the loop could be cancelled.
            await asyncio.sleep(0)

    async def tick(self) -> None:
        """One pass: Check every due Feed, a few at a time, then housekeeping.

        Never raises, except for cancellation.
        """
        due = self._due()
        log.debug("tick due=%d", len(due))
        if due:
            queue = iter(due)
            workers = [self._drain(queue) for _ in range(min(self._max_concurrent, len(due)))]
            # Boundary: not a TaskGroup, which would cancel every other Check when one
            # worker failed. Here a failure comes back as a value, and the Feeds that
            # worker had not reached are still in the queue for the others.
            for result in await asyncio.gather(*workers, return_exceptions=True):
                if isinstance(result, Exception):
                    log.error("A Check worker stopped: %r", result)
        self._housekeeping()

    async def check_feed(self, feed_id: int) -> bool:
        """One Check of one Feed, whether it is due or not. Never raises, except for cancellation.

        Returns False, at once, if that Feed is already being checked or waiting for its turn.
        """
        if feed_id in self._checking:
            return False
        self._checking.add(feed_id)
        try:
            async with self._slots:
                await self._check(feed_id)
        except Exception:
            # Boundary: the last one around a Feed. A failed Check is dealt with further
            # in; this catches a Check that broke, so that it costs only itself. The Feed
            # is kept out of the next ticks for a while, because a Check that could not
            # even save when to try again would otherwise run on every one of them.
            log.exception("check.broke feed=%s", feed_id)
            with contextlib.suppress(Exception):
                self._held[feed_id] = self._clock.now() + HOLD_OFF_S
        finally:
            self._checking.discard(feed_id)
        return True

    def _due(self) -> list[int]:
        """The ids of the Feeds to Check now, most overdue first.

        Only ids are kept: each Feed is loaded again when its turn comes.
        """
        try:
            now = self._clock.now()
            self._held = {feed_id: until for feed_id, until in self._held.items() if until > now}
            return [feed.id for feed in self._db.due_feeds(now) if feed.id not in self._held]
        except Exception:
            log.exception("Could not list the Feeds that are due")
            return []

    async def _drain(self, queue: Iterator[int]) -> None:
        for feed_id in queue:
            await self.check_feed(feed_id)

    def _housekeeping(self) -> None:
        try:
            now = self._clock.now()
            if self._purged_at is None or now - self._purged_at >= PURGE_EVERY_S:
                self._purged_at = now  # first, so a purge that fails waits an hour like any other
                gone = self._db.purge_removed_servers(now - SERVER_KEEP_S)
                if gone:
                    log.info("Deleted the data of %d Server(s) removed over 30 days ago", len(gone))
        except Exception:
            log.exception("Housekeeping failed")
        try:
            # Once a day, the Log entries that are over a year old.
            now = self._clock.now()
            if self._pruned_at is None or now - self._pruned_at >= PRUNE_EVERY_S:
                self._pruned_at = now
                deleted = self._db.prune_log_entries(now - LOG_KEEP_S)
                if deleted:
                    log.info("Deleted the Log entries from over a year ago: %d", deleted)
        except Exception:
            log.exception("Could not delete old Log entries")

    # -- one Check --

    async def _check(self, feed_id: int) -> None:
        # Loaded here, not handed in: the Feed may have been edited, paused or deleted
        # since it was listed as due.
        feed = self._db.get_feed(feed_id)
        if feed is None or feed.paused is not None:
            return
        server = self._db.get_server(feed.server_id)
        if server is None or server.removed_at is not None:
            return
        if feed.id in self._stuck:
            await self._failed(feed, "An earlier Check of this Feed never finished.")
            return

        # Boundary: the next Check is booked before this one does anything, as if it were
        # going to fail. Should this Check take the whole process down with it, the Feed is
        # then not due the moment the bot is back, and the other Feeds get their turn
        # first. It also proves the database can be written before anything is posted.
        booked = self._after(self._clock.now(), _backoff(feed.interval_s, feed.fail_count + 1))
        if not self._save(feed.id, next_check_at=booked):
            return

        loop = asyncio.get_running_loop()
        started = loop.time()
        work = loop.create_task(self._work(feed, started + self._check_timeout_s))
        work.add_done_callback(_retrieve)
        try:
            in_time = await self._bounded(work, self._check_timeout_s)
        except asyncio.CancelledError:
            # A shutdown is not a failure: the Feed gets its place in the queue back.
            with contextlib.suppress(Exception):
                self._save(feed.id, next_check_at=feed.next_check_at)
            raise

        if not in_time:
            if not work.done():
                self._leave_behind(feed.id, work)
            limit = f"{self._check_timeout_s:g}"
            await self._failed(feed, f"The Check took longer than {limit} seconds.")
        elif work.cancelled():
            await self._failed(feed, "The Check was interrupted.")
        elif (error := work.exception()) is None:
            outcome = work.result()
            await self._settle(feed, outcome)
            self._log_check(feed, outcome, round((loop.time() - started) * 1000))
        elif isinstance(error, _CheckFailed) and error.slow_down:
            self._slowed(feed, error.wait_s)
        elif isinstance(error, _CheckFailed):
            await self._failed(feed, str(error), wait_s=error.wait_s)
        else:
            name = type(error).__name__
            await self._failed(feed, f"Unexpected error ({name}); see the bot's log.", error)

    def _log_check(self, feed: Feed, outcome: _Outcome | None, took_ms: int) -> None:
        """One line for a Check that ran to its end: INFO if it posted Items, else DEBUG.
        If it left the Feed paused, the reason is in the line; the Journal's Log entry
        has its own WARNING.

        A Check that failed has its own WARNING line, so it has none of these.
        """
        if outcome is None:  # the Feed was deleted, paused or edited meanwhile
            log.debug("check feed=%s outcome=dropped took_ms=%d", feed.id, took_ms)
            return
        line = "check feed=%s name=%s server=%s posted=%d skipped=%d took_ms=%d%s"
        extra = ""
        if outcome.not_modified:
            extra = " not_modified=yes"
        elif outcome.pause is not None:
            extra = f" paused={outcome.pause.value}"
        level = logging.INFO if outcome.posted else logging.DEBUG
        log.log(
            level,
            line,
            feed.id,
            quote(outcome.feed.name),
            feed.server_id,
            outcome.posted,
            outcome.skipped,
            took_ms,
            extra,
        )

    async def _work(self, feed: Feed, deadline: float) -> _Outcome | None:
        """The part of a Check that can hang: fetch, parse and post.

        `deadline` is when the Check's time is up, on the event loop's clock. Returns
        what to store, or None if the Feed was deleted, paused or pointed at
        another address meanwhile. Raises if the Check failed.
        """
        try:
            result = await self._fetcher.fetch(
                feed.url, etag=feed.etag, last_modified=feed.last_modified
            )
        except FetchError as exc:
            raise _CheckFailed(
                str(exc) or "The Feed could not be fetched.",
                wait_s=_asked_wait(exc),
                slow_down=getattr(exc, "slow_down", False) is True,
            ) from exc
        if result.not_modified:
            return _Outcome(
                feed,
                result.etag or feed.etag,
                result.last_modified or feed.last_modified,
                not_modified=True,
            )
        parsed = await self._parse_in_thread(feed.id, result.body, result.url or feed.url)

        # The fetch may have taken a while. Post with the Feed as it is now, if it still
        # wants these Items at all.
        current = self._db.get_feed(feed.id)
        if current is None or current.paused is not None or current.url != feed.url:
            return None
        tally = _Tally()
        queue = self._plan(current, parsed.items, tally)
        pause, unfinished = await self._post(current, queue, deadline, tally)
        return _Outcome(
            current,
            # An Item left to be tried again is only seen again if the source is read in
            # full, and with these stored the source would answer "not modified".
            None if unfinished else result.etag,
            None if unfinished else result.last_modified,
            parsed.title,
            parsed.link,
            pause,
            posting_again=tally.delivered > 0 and tally.given_up == 0,
            posted=tally.delivered,
            skipped=tally.given_up + tally.beyond,
        )

    async def _parse_in_thread(self, feed_id: int, body: bytes, url: str) -> ParsedFeed:
        job = asyncio.ensure_future(asyncio.to_thread(self._parse, body, url))
        job.add_done_callback(_retrieve)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            # Boundary: a thread cannot be stopped, only abandoned. It is remembered, so
            # that the Feed's later Checks fail at once instead of each starting one more
            # thread that never comes back, until none are left for anybody.
            if not job.done():
                self._leave_behind(feed_id, job)
            raise
        except Exception as exc:
            raise _CheckFailed(str(exc) or "The Feed could not be read.") from exc

    def _plan(self, feed: Feed, listed: Iterable[Item], tally: _Tally) -> list[Item]:
        """Record the listed Items that will not be posted and return those that will.

        Oldest first.
        """
        now = self._clock.now()
        items: dict[str, Item] = {}
        for item in listed:
            items.setdefault(item.key, item)  # a key listed twice is one Item

        if not self._db.has_seen_items(feed.id):
            # The first Check: what the source lists now is where the Feed starts, not news.
            # In one write with the marker, so that it cannot be half done.
            start = [(key, ItemStatus.SEEN) for key in (STARTED_KEY, *items)]
            self._db.record_seen(feed.id, start, now)
            return []

        states = self._db.seen_states(feed.id, [STARTED_KEY, *items])
        if STARTED_KEY not in states:
            self._db.record_seen(feed.id, [(STARTED_KEY, ItemStatus.SEEN)], now)
        # Pruning only ever follows a full listing that has just been touched. After a run
        # of "not modified" answers the stored times are old, and pruning by them would
        # forget Items the source still lists, which would then be posted again.
        self._db.touch_seen(feed.id, list(states), now)
        self._db.prune_seen(feed.id, now - SEEN_KEEP_S)

        # Boundary: an Item still marked as being sent had its delivery cut off, by a crash
        # or by the Check's time limit, before the bot heard whether Discord took it.
        # Sending it again could post it twice, so it is skipped instead (docs/adr/0003).
        for key, item in items.items():
            if key in states and states[key][0] is ItemStatus.SENDING:
                self._give_up(feed, item, "its delivery was cut off", tally)

        new = [item for key, item in items.items() if key not in states]
        retries = [
            item
            for key, item in items.items()
            if key in states and states[key][0] is ItemStatus.PENDING
        ]
        if not new and not retries:
            return []

        filters = self._db.list_filters(feed.id) if new else []
        wanted: list[Item] = []
        settled: list[tuple[str, ItemStatus]] = []  # recorded without being posted
        for item in new:
            if self._wanted(feed, item, filters):
                wanted.append(item)
            else:
                settled.append((item.key, ItemStatus.SEEN))

        # Catch-up limits the new Items. Retries come on top: there are never many, since
        # each is given up after a few Checks.
        places = _places(list(items.values()))
        wanted.sort(key=lambda item: places[item.key])  # newest first
        beyond = wanted[CATCH_UP_LIMIT:]
        settled += [(item.key, ItemStatus.SKIPPED) for item in beyond]
        if settled:
            self._db.record_seen(feed.id, settled, now)
        if beyond:
            tally.beyond += len(beyond)
            log.info(
                "item.skipped feed=%s name=%s count=%d reason=%s",
                feed.id,
                quote(feed.name),
                len(beyond),
                quote("beyond Catch-up"),
            )

        queue = wanted[:CATCH_UP_LIMIT] + retries
        queue.sort(key=lambda item: places[item.key], reverse=True)  # oldest first
        return queue

    def _wanted(self, feed: Feed, item: Item, filters: Sequence[Filter]) -> bool:
        try:
            return passes(item, filters)
        except Exception:
            # Boundary: Filters that cannot be applied to one Item let it through. The
            # other choices are losing the Item unseen or failing the whole Check over it.
            log.warning("Feed %s: could not apply the Filters to an Item", feed.id, exc_info=True)
            return True

    async def _post(
        self, feed: Feed, queue: Sequence[Item], deadline: float, tally: _Tally
    ) -> tuple[PauseReason | None, bool]:
        """Post the Items in order, for as long as the Check has time for one more.

        Returns why the Feed has to pause, if it has to, and whether any Item is left to
        be tried again on the next Check.
        """
        loop = asyncio.get_running_loop()
        unfinished = False
        for started, item in enumerate(queue):
            if deadline - loop.time() < self._item_allowance_s:
                # Boundary: a backlog that does not fit into one Check is not a failed
                # Check. The Items not started are left untouched, neither recorded nor
                # counted, so the next Check finds them new, and it comes at the usual
                # time. Starting them anyway would get one cut off by the time limit.
                log.info(
                    "check.out_of_time feed=%s name=%s left=%d",
                    feed.id,
                    quote(feed.name),
                    len(queue) - started,
                )
                return None, True
            try:
                result = await self._post_item(feed, item, tally)
            except Exception:
                # Boundary: the guard around one Item. Whatever went wrong with it, in the
                # renderer, the deliverer or the database, the next Item still gets its
                # turn. Nothing is recorded here. An Item that could not be recorded at
                # all was never sent, and the next Check finds it new. One whose outcome
                # could not be recorded is still marked as being sent, and the next Check
                # skips it rather than risk posting it twice.
                log.warning(
                    "item.failed feed=%s name=%s item=%s detail=%s",
                    feed.id,
                    quote(feed.name),
                    quote(item.key[:80]),
                    quote("could not be handled; going on with the next"),
                    exc_info=True,
                )
                unfinished = True
                continue
            if isinstance(result, PauseReason):
                return result, True
            unfinished = unfinished or result is ItemStatus.PENDING
        return None, unfinished

    async def _post_item(self, feed: Feed, item: Item, tally: _Tally) -> ItemStatus | PauseReason:
        """Post one Item and record how it went, as soon as that is known."""
        # Boundary: the attempt is recorded and counted before anything is sent. An Item
        # that cannot be recorded is therefore never posted, so a database that cannot be
        # written cannot turn into the same Item posted on every Check. It is marked as
        # being sent last, so that mark is only ever left by an attempt that was started.
        self._db.record_seen(feed.id, [(item.key, ItemStatus.PENDING)], self._clock.now())
        attempts = self._db.bump_attempts(feed.id, item.key)
        self._db.record_seen(feed.id, [(item.key, ItemStatus.SENDING)], self._clock.now())

        try:
            outcome = await self._attempt(feed, item)
        except asyncio.CancelledError:
            # Cut off by the Check's time limit or a shutdown, with nothing to say whether
            # Discord took the message. Possibly never is chosen over possibly twice
            # (docs/adr/0003): the Item is skipped. Should that not get recorded, the Item
            # is still marked as being sent, and the next Check skips it.
            with contextlib.suppress(Exception):
                self._give_up(feed, item, "its delivery was cut off", tally)
            raise
        if outcome is DeliveryOutcome.DELIVERED:
            self._db.record_seen(feed.id, [(item.key, ItemStatus.DELIVERED)], self._clock.now())
            tally.delivered += 1
            return ItemStatus.DELIVERED
        if outcome is DeliveryOutcome.REJECTED:
            return self._give_up(
                feed, item, "it could not be made into a message Discord takes", tally
            )
        pauses = outcome in (DeliveryOutcome.LOST_CHANNEL, DeliveryOutcome.NEEDS_TAG)
        if not pauses and attempts >= MAX_DELIVERY_ATTEMPTS:
            return self._give_up(feed, item, f"its delivery failed {attempts} times", tally)
        # Discord answered and did not take the message: it is safe to send again.
        self._db.record_seen(feed.id, [(item.key, ItemStatus.PENDING)], self._clock.now())
        if outcome is DeliveryOutcome.LOST_CHANNEL:
            return PauseReason.LOST_CHANNEL
        if outcome is DeliveryOutcome.NEEDS_TAG:
            return PauseReason.NEEDS_TAG
        return ItemStatus.PENDING

    async def _attempt(self, feed: Feed, item: Item) -> DeliveryOutcome:
        """Render and deliver one Item, falling back to the default rendering.

        REJECTED means nothing more can be done for the Item.
        """
        refused = False
        message = self._rendered(self._render, feed, item)
        if message is not None:
            outcome = await self._deliver(feed, message)
            if outcome is not DeliveryOutcome.REJECTED:
                return outcome
            refused = True

        # The Feed's own Template did not work for this Item, here or at Discord.
        message = self._rendered(self._render_default, feed, item)
        if message is None:
            return DeliveryOutcome.REJECTED
        outcome = await self._deliver(feed, message)
        if refused and outcome is DeliveryOutcome.RETRY:
            return DeliveryOutcome.REJECTED  # the default rendering gets one try, not three
        return outcome

    def _rendered(self, render: RenderFn, feed: Feed, item: Item) -> OutgoingMessage | None:
        try:
            return render(feed, item)
        except Exception:
            log.debug(
                "item.render_failed feed=%s item=%s", feed.id, quote(item.key[:80]), exc_info=True
            )
            return None

    async def _deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        started = time.monotonic()
        try:
            outcome = await self._deliverer.deliver(feed, message)
        except Exception:
            # The Deliverer reports failures as outcomes, so this is a fault in it. For the
            # Item it is one more failed attempt.
            log.warning(
                "deliver.failed feed=%s name=%s outcome=raised",
                feed.id,
                quote(feed.name),
                exc_info=True,
            )
            return DeliveryOutcome.RETRY
        if not isinstance(outcome, DeliveryOutcome):
            outcome = DeliveryOutcome.RETRY
        took_ms = round((time.monotonic() - started) * 1000)
        if outcome is DeliveryOutcome.DELIVERED:
            log.debug(
                "deliver feed=%s name=%s channel=%s outcome=delivered took_ms=%d",
                feed.id,
                quote(feed.name),
                feed.channel_id,
                took_ms,
            )
        else:
            log.warning(
                "deliver.failed feed=%s name=%s channel=%s outcome=%s took_ms=%d",
                feed.id,
                quote(feed.name),
                feed.channel_id,
                outcome.value,
                took_ms,
            )
        return outcome

    def _give_up(self, feed: Feed, item: Item, why: str, tally: _Tally) -> ItemStatus:
        now = self._clock.now()
        self._db.record_seen(feed.id, [(item.key, ItemStatus.SKIPPED)], now)
        tally.given_up += 1
        log.warning(
            "item.skipped feed=%s name=%s item=%s reason=%s",
            feed.id,
            quote(feed.name),
            quote(item.key[:80]),
            quote(why),
        )
        # Counted at once, not when the Check is settled: a Check that is cut off or fails
        # after this still shows on the Feed's status that an Item was lost.
        self._db.count_skipped(feed.id, now)
        return ItemStatus.SKIPPED

    # -- after the Check --

    async def _settle(self, feed: Feed, outcome: _Outcome | None) -> None:
        if outcome is None:
            # As if this Check had not been started.
            self._save(feed.id, next_check_at=feed.next_check_at)
        elif outcome.pause is not None:
            await self._paused(outcome.feed, outcome.pause, feed.next_check_at)
        else:
            await self._succeeded(outcome)

    async def _succeeded(self, outcome: _Outcome) -> None:
        feed = outcome.feed
        now = self._clock.now()
        changes: dict[str, Any] = {
            "etag": outcome.etag,
            "last_modified": outcome.last_modified,
            "last_success_at": now,
            "last_checked_at": now,
            "rate_limited_since": None,
            "next_check_at": self._after(now, feed.interval_s),
            "fail_count": 0,
            "failing_since": None,
            "last_error": "",
            "warned": False,
        }
        if outcome.title is not None and outcome.title != feed.source_title:
            changes["source_title"] = outcome.title
        if outcome.link is not None and outcome.link != feed.source_link:
            changes["source_link"] = outcome.link
        if outcome.posting_again:
            changes["skipped_count"] = 0
            changes["skipped_since"] = None
        if not self._save(feed.id, **changes):
            return
        if feed.warned:
            await self._report(feed, LogKind.FEED_WORKING_AGAIN)

    def _slowed(self, feed: Feed, wait_s: int) -> None:
        """Book the next Check after the source asked for fewer requests. Not a failure.

        The source answered and nothing is broken, so the Feed becomes a Rate-limited feed
        and its failure count stays as it is. The next Check comes when the source said, or
        after the Feed's interval if it did not say.
        """
        delay = max(wait_s, SLOW_DOWN_MIN_S) if wait_s > 0 else feed.interval_s
        now = self._clock.now()
        since = now if feed.rate_limited_since is None else feed.rate_limited_since
        if self._save(
            feed.id,
            next_check_at=self._after(now, delay),
            last_checked_at=now,
            rate_limited_since=since,
        ):
            # A Feed becoming Rate-limited is news; one that stays so is checked often.
            log.log(
                logging.DEBUG if feed.rate_limited_since is not None else logging.WARNING,
                "check.rate_limited feed=%s name=%s server=%s next_check_in_s=%d",
                feed.id,
                quote(feed.name),
                feed.server_id,
                delay,
            )

    async def _failed(
        self, feed: Feed, error: str, cause: BaseException | None = None, *, wait_s: int = 0
    ) -> None:
        """Count a failed Check and book the next one. Never touches Seen items.

        `wait_s` is how long the source asked to be left alone: the next Check is not
        booked sooner than that, however short the backoff.
        """
        error = _short(error)
        now = self._clock.now()
        count = feed.fail_count + 1
        since = now if feed.failing_since is None else feed.failing_since
        warn = not feed.warned and now - since >= WARN_AFTER_S
        log.warning(
            "check.failed feed=%s name=%s server=%s failures=%d error=%s",
            feed.id,
            quote(feed.name),
            feed.server_id,
            count,
            quote(error),
            exc_info=cause,
        )
        changes: dict[str, Any] = {
            "fail_count": count,
            "failing_since": since,
            "last_error": error,
            "last_checked_at": now,
            "rate_limited_since": None,
            "next_check_at": self._after(now, max(_backoff(feed.interval_s, count), wait_s)),
        }
        if warn:
            # Stored before the report is sent: a warning that is lost is better than one
            # that is repeated on every Check from now on.
            changes["warned"] = True
        if self._save(feed.id, **changes) and warn:
            await self._report(
                feed,
                LogKind.FEED_BROKEN,
                "It has been failing for over a day. It is still being checked. "
                f"Last error: {error}",
            )

    async def _paused(self, feed: Feed, reason: PauseReason, due: int) -> None:
        # The Feed keeps the time it was due, so it is checked as soon as it is resumed.
        # Nothing else is stored: with the old validators the source is read in full
        # again, and the Items that were not delivered are still there to post.
        changes = {"last_checked_at": self._clock.now(), "rate_limited_since": None}
        if not self._save(feed.id, paused=reason, next_check_at=due, **changes):
            return
        # No line here: _report saves the Log entry, and the Journal writes its line
        # (feed.auto_pause, with the reason) to the container log.
        if reason is PauseReason.NEEDS_TAG:
            cause = "That forum wants a tag on every Forum post and the Feed has none"
            fix = "Give the Feed a tag, then resume it."
        else:
            cause = "The bot can no longer post in that channel"
            fix = "Check the channel and the bot's permissions there, then resume the Feed."
        await self._report(feed, LogKind.FEED_AUTO_PAUSED, f"{cause}. {fix}")

    async def _report(self, feed: Feed, kind: LogKind, detail: str = "") -> None:
        """Save the Log entry of something the bot did or found itself, and report it in
        the Server's Logs channel.

        Boundary: a Log entry that cannot be saved, or a notifier that raises or hangs,
        costs the report and nothing else. Reports go out after the Check's state is
        stored, and the journal gives each a time limit of its own.
        """
        actor = Actor.bot()
        try:
            entry = self._journal.record_feed(actor, kind, feed, detail=detail)
        except Exception:
            log.warning(
                "report.failed feed=%s name=%s kind=%s detail=%s",
                feed.id,
                quote(feed.name),
                kind.value,
                quote("could not save the Log entry of a report"),
                exc_info=True,
            )
            return
        await self._journal.announce(entry, actor)

    # -- plumbing --

    async def _bounded(self, task: asyncio.Future[Any], limit: float) -> bool:
        """Wait for `task` for at most `limit` seconds, then cancel it.

        Returns whether it finished in time.

        Boundary: the wait is what is bounded, not the task. asyncio.timeout() waits for
        the task to accept its cancellation, so a port that swallowed it, or hung in its
        own cleanup, would hold its Check, the tick behind it and with that every Feed,
        for ever. A task that will not stop gets `grace_s` and is then left behind.
        """
        try:
            done, _ = await asyncio.wait({task}, timeout=limit)
            if done:
                return True
            task.cancel()
            await asyncio.wait({task}, timeout=self._grace_s)
            return False
        except asyncio.CancelledError:
            # Being stopped ourselves: the task goes too, with a moment for its cleanup.
            task.cancel()
            await asyncio.wait({task}, timeout=self._grace_s)
            raise

    def _leave_behind(self, feed_id: int, future: asyncio.Future[Any]) -> None:
        """Remember work that could not be stopped, until it ends by itself.

        While it runs, the Feed's Checks fail without starting anything: the Feed is never
        worked on twice at once, and it turns up as a Broken feed instead of going quiet.
        """

        def ended(done: asyncio.Future[Any]) -> None:
            if self._stuck.get(feed_id) is done:
                del self._stuck[feed_id]

        log.warning("check.stuck feed=%s detail=%s", feed_id, quote("left running"))
        self._stuck[feed_id] = future
        future.add_done_callback(ended)

    def _save(self, feed_id: int, **changes: Any) -> bool:
        """Store changes to a Feed. False if the Feed has been deleted."""
        try:
            self._db.update_feed(feed_id, **changes)
        except FeedNotFound:
            return False
        return True

    def _after(self, now: int, delay: int) -> int:
        """`delay` seconds from now, and up to a tenth more so that Feeds drift apart."""
        try:
            extra = int(delay * JITTER * min(max(float(self._rand()), 0.0), 1.0))
        except Exception:  # a random source that raises, or hands back something like NaN
            extra = 0
        return now + delay + extra
