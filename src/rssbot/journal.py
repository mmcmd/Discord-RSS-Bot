"""The record of what members and the bot did: saving Log entries and reporting them.

Everything that changes a Feed, a Grant or the Logs channel goes through one Journal.
`record` saves a Log entry and writes one line to the container log; `announce` reports
entries in the Server's Logs channel. Saving never depends on the Logs channel: a Server
without one has its entries saved all the same, and nothing is posted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlsplit

from .db import Database
from .models import Actor, Change, Feed, LogEntry, LogKind
from .ports import Clock, Notifier

log = logging.getLogger("rssbot.journal")

ANNOUNCE_TIMEOUT_S = 10.0

# The bot's own problem reports stand out in the container log; everything else is INFO.
_WARNING_KINDS = frozenset({LogKind.FEED_AUTO_PAUSED, LogKind.FEED_BROKEN})


def _level(entry: LogEntry) -> int:
    return logging.WARNING if entry.kind in _WARNING_KINDS else logging.INFO


def quote(value: str) -> str:
    """A value for the container log, in quotes, that cannot end its line or its quotes.

    Whatever a member or a feed put in a name, the line stays one line and no key=value
    pair can be added to it.
    """
    out = []
    for char in value:
        if char in '"\\':
            out.append("\\" + char)
        elif char.isprintable():
            out.append(char)
        else:  # newlines, other control characters and Unicode line separators
            code = ord(char)
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
    return '"' + "".join(out) + '"'


def _site_only(value: str) -> str:
    """A web address cut down to its site, e.g. `https://example.com/…`; anything else as it is."""
    if not value.lower().startswith(("http://", "https://")):
        return value
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        host = None
    return f"{parts.scheme}://{host}/…" if host else "(an address)"


def log_line(entry: LogEntry) -> str:
    """The container log line of a Log entry.

    For example: `feed.pause server=123 feed=45 name="BBC News" channel=678 by="alex"
    by_id=987`. The kind comes first, then key=value pairs in a fixed order; a pair with
    nothing to say is left out. The bot as actor is `by=bot`. Of a changed address only the
    site is written: an address can carry a private key in its path or query.
    """
    parts = [entry.kind.value, f"server={entry.server_id}"]
    if entry.feed_id is not None:
        parts.append(f"feed={entry.feed_id}")
    if entry.feed_name:
        parts.append(f"name={quote(entry.feed_name)}")
    if entry.channel_id is not None:
        parts.append(f"channel={entry.channel_id}")
    if entry.actor_id is None:
        parts.append("by=bot")
    else:
        parts.append(f"by={quote(entry.actor_name)}")
        parts.append(f"by_id={entry.actor_id}")
    if entry.changes:
        changes = "; ".join(
            f"{c.label}: {_site_only(c.before)} -> {_site_only(c.after)}" for c in entry.changes
        )
        parts.append(f"changes={quote(changes)}")
    if entry.detail:
        parts.append(f"detail={quote(entry.detail)}")
    return " ".join(parts)


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Look at a finished future's exception, so asyncio does not report it as lost."""
    if not future.cancelled():
        future.exception()


class Journal:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        notifier: Notifier | None = None,
        *,
        announce_timeout_s: float = ANNOUNCE_TIMEOUT_S,
    ) -> None:
        self._db = db
        self._clock = clock
        self._notifier = notifier
        self._announce_timeout_s = announce_timeout_s
        self._announcing: set[asyncio.Task[None]] = set()

    # -- saving --

    def record(
        self,
        server_id: int,
        actor: Actor,
        kind: LogKind,
        *,
        feed_id: int | None = None,
        feed_name: str = "",
        channel_id: int | None = None,
        feed_url: str = "",
        changes: Iterable[Change] = (),
        detail: str = "",
    ) -> LogEntry:
        """Save a Log entry, dated now, and write its line to the container log
        (WARNING for the bot's own problem reports, INFO for the rest).

        For an entry about a Feed, record_feed takes the Feed instead of its parts. This
        posts nothing: pass the entry to announce or announce_soon. Raises sqlite3.Error
        if the entry cannot be saved.
        """
        entry = self._db.add_log_entry(
            server_id=server_id,
            at=self._clock.now(),
            actor_id=actor.id,
            actor_name=actor.name,
            kind=kind,
            feed_id=feed_id,
            feed_name=feed_name,
            channel_id=channel_id,
            feed_url=feed_url,
            changes=changes,
            detail=detail,
        )
        log.log(_level(entry), "%s", log_line(entry))
        return entry

    def record_feed(
        self,
        actor: Actor,
        kind: LogKind,
        feed: Feed,
        *,
        changes: Iterable[Change] = (),
        detail: str = "",
    ) -> LogEntry:
        """Save a Log entry about a Feed, with the Feed's name, channel and address as
        they are in `feed`. For a removal or a rename, pass the Feed as it should be shown."""
        return self.record(
            feed.server_id,
            actor,
            kind,
            feed_id=feed.id,
            feed_name=feed.name,
            channel_id=feed.channel_id,
            feed_url=feed.url,
            changes=changes,
            detail=detail,
        )

    def record_many(
        self, actor: Actor, kind: LogKind, feeds: Iterable[Feed], *, detail: str = ""
    ) -> list[LogEntry]:
        """Save one Log entry per Feed, all or none, e.g. for the Feeds of an OPML import.

        Each gets its own line in the container log. Passed together to announce they are
        one report.
        """
        now = self._clock.now()
        entries = self._db.add_log_entries(
            LogEntry(
                id=0,
                server_id=feed.server_id,
                at=now,
                actor_id=actor.id,
                actor_name=actor.name,
                kind=kind,
                feed_id=feed.id,
                feed_name=feed.name,
                channel_id=feed.channel_id,
                feed_url=feed.url,
                detail=detail,
            )
            for feed in feeds
        )
        for entry in entries:
            log.log(_level(entry), "%s", log_line(entry))
        return entries

    # -- reporting --

    async def announce(self, entries: LogEntry | Sequence[LogEntry], actor: Actor) -> None:
        """Report Log entries in their Server's Logs channel, if their kind is one that is.

        Entries passed together are one report ("Sam imported 50 Feeds"). Never raises,
        and waits `announce_timeout_s` at most: a Discord call that fails or hangs costs
        the report, with a warning in the container log, and nothing else.
        """
        for server_id, group in self._to_announce(entries).items():
            await self._announce(server_id, group, actor)

    def announce_soon(self, entries: LogEntry | Sequence[LogEntry], actor: Actor) -> None:
        """Start announce in the background and return at once, for a command handler
        that must not keep its reply waiting. Needs a running event loop; see drain."""
        if not self._to_announce(entries):
            return
        task = asyncio.get_running_loop().create_task(
            self.announce(entries, actor), name="journal-announce"
        )
        # The loop only holds a weak reference: without this one the task could vanish.
        self._announcing.add(task)
        task.add_done_callback(self._announcing.discard)

    async def drain(self) -> None:
        """Wait until every report started with announce_soon is out or given up on."""
        while self._announcing:
            await asyncio.gather(*self._announcing, return_exceptions=True)

    def _to_announce(self, entries: LogEntry | Sequence[LogEntry]) -> dict[int, list[LogEntry]]:
        """The entries that are reported in a Logs channel, by Server."""
        groups: dict[int, list[LogEntry]] = {}
        if self._notifier is None:
            return groups
        for entry in [entries] if isinstance(entries, LogEntry) else entries:
            if entry.kind.announced:
                groups.setdefault(entry.server_id, []).append(entry)
        return groups

    async def _announce(self, server_id: int, entries: list[LogEntry], actor: Actor) -> None:
        # The wait is what is bounded, not the task: a notifier that swallows its
        # cancellation is left behind rather than waited for.
        assert self._notifier is not None
        try:
            task = asyncio.ensure_future(self._notifier.announce(server_id, entries, actor))
        except Exception:
            log.warning(
                "Could not report to the Logs channel of Server %s", server_id, exc_info=True
            )
            return
        task.add_done_callback(_retrieve)
        try:
            done, _ = await asyncio.wait({task}, timeout=self._announce_timeout_s)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            log.warning(
                "A report to the Logs channel of Server %s took too long and was dropped",
                server_id,
            )
        elif not task.cancelled() and task.exception() is not None:
            log.warning(
                "Could not report to the Logs channel of Server %s",
                server_id,
                exc_info=task.exception(),
            )
