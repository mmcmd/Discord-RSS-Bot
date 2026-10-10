"""SQLite storage for Servers, Grants, Feeds, Filters, Seen items, webhooks and Log entries.

Methods are synchronous and are called straight from the event loop: every query is
tiny and local. Times are Unix seconds and are always passed in, never read here.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    DEFAULT_FORUM_TITLE_TEMPLATE,
    DEFAULT_INTERVAL_S,
    DEFAULT_TEXT_TEMPLATE,
    PAUSE_KINDS,
    ButtonSpec,
    Change,
    ChannelKind,
    EmbedSpec,
    Feed,
    FeedAttribution,
    FieldSpec,
    Filter,
    FilterField,
    FilterList,
    Grant,
    ItemStatus,
    Level,
    LogEntry,
    LogKind,
    PauseReason,
    PostAs,
    Server,
    TargetKind,
)

# Applied in order from PRAGMA user_version upward. Never edit a released script; add one.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE servers (
        server_id       INTEGER PRIMARY KEY,
        logs_channel_id INTEGER,
        removed_at      INTEGER
    ) STRICT;

    CREATE TABLE grants (
        server_id   INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
        target_id   INTEGER NOT NULL,
        target_kind TEXT NOT NULL,
        level       TEXT NOT NULL,
        PRIMARY KEY (server_id, target_id)
    ) STRICT, WITHOUT ROWID;

    CREATE TABLE feeds (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id            INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
        channel_id           INTEGER NOT NULL,
        channel_kind         TEXT NOT NULL,
        name                 TEXT NOT NULL,
        url                  TEXT NOT NULL,
        interval_s           INTEGER NOT NULL,
        source_title         TEXT NOT NULL,
        source_link          TEXT NOT NULL,
        text_template        TEXT NOT NULL,
        embed                TEXT,
        buttons              TEXT NOT NULL,
        mention_role_ids     TEXT NOT NULL,
        post_as              TEXT NOT NULL,
        custom_name          TEXT NOT NULL,
        custom_avatar        TEXT NOT NULL,
        site_name            TEXT NOT NULL,
        site_icon            TEXT NOT NULL,
        site_checked_at      INTEGER,
        forum_title_template TEXT NOT NULL,
        forum_tag_ids        TEXT NOT NULL,
        forum_cover          INTEGER NOT NULL,
        paused               TEXT,
        etag                 TEXT,
        last_modified        TEXT,
        next_check_at        INTEGER NOT NULL,
        fail_count           INTEGER NOT NULL,
        failing_since        INTEGER,
        warned               INTEGER NOT NULL,
        last_error           TEXT NOT NULL,
        last_success_at      INTEGER,
        created_at           INTEGER NOT NULL
    ) STRICT;
    CREATE INDEX feeds_by_server ON feeds(server_id);
    CREATE INDEX feeds_by_channel ON feeds(channel_id);
    CREATE INDEX feeds_by_next_check ON feeds(next_check_at);

    CREATE TABLE filters (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
        list    TEXT NOT NULL,
        field   TEXT NOT NULL,
        word    TEXT NOT NULL
    ) STRICT;
    CREATE INDEX filters_by_feed ON filters(feed_id);

    CREATE TABLE seen_items (
        feed_id        INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
        key            TEXT NOT NULL,
        status         TEXT NOT NULL,
        attempts       INTEGER NOT NULL DEFAULT 0,
        first_seen_at  INTEGER NOT NULL,
        last_listed_at INTEGER NOT NULL,
        PRIMARY KEY (feed_id, key)
    ) STRICT, WITHOUT ROWID;

    CREATE TABLE webhooks (
        channel_id INTEGER PRIMARY KEY,
        webhook_id INTEGER NOT NULL,
        token      TEXT NOT NULL
    ) STRICT;
    """,
    """
    ALTER TABLE feeds ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE feeds ADD COLUMN skipped_since INTEGER;
    """,
    """
    ALTER TABLE feeds ADD COLUMN last_checked_at INTEGER;
    ALTER TABLE feeds ADD COLUMN rate_limited_since INTEGER;
    UPDATE feeds SET last_checked_at = last_success_at;
    """,
    # feed_id is deliberately not a foreign key: a Log entry outlives its Feed.
    """
    CREATE TABLE log_entries (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id  INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
        at         INTEGER NOT NULL,
        actor_id   INTEGER,
        actor_name TEXT NOT NULL,
        kind       TEXT NOT NULL,
        feed_id    INTEGER,
        feed_name  TEXT NOT NULL,
        channel_id INTEGER,
        feed_url   TEXT NOT NULL,
        changes    TEXT NOT NULL,
        detail     TEXT NOT NULL
    ) STRICT;
    CREATE INDEX log_entries_by_server ON log_entries(server_id, id);
    CREATE INDEX log_entries_by_feed ON log_entries(feed_id);
    """,
)

# Keys per statement; well under SQLite's bound-variable limit (999 on old builds).
_CHUNK = 500


class FeedNotFound(KeyError):
    """No Feed has this id."""


def _encode_embed(embed: EmbedSpec | None) -> str | None:
    return None if embed is None else json.dumps(dataclasses.asdict(embed))


def _decode_embed(text: str | None) -> EmbedSpec | None:
    if text is None:
        return None
    data = json.loads(text)
    data["fields"] = tuple(FieldSpec(**f) for f in data["fields"])
    return EmbedSpec(**data)


def _encode_buttons(buttons: Iterable[ButtonSpec]) -> str:
    return json.dumps([dataclasses.asdict(b) for b in buttons])


def _decode_buttons(text: str) -> tuple[ButtonSpec, ...]:
    return tuple(ButtonSpec(**b) for b in json.loads(text))


def _encode_ids(ids: Iterable[int]) -> str:
    return json.dumps([int(i) for i in ids])


def _decode_ids(text: str) -> tuple[int, ...]:
    return tuple(json.loads(text))


def _encode_pause(reason: PauseReason | None) -> str | None:
    return None if reason is None else PauseReason(reason).value


def _decode_pause(text: str | None) -> PauseReason | None:
    return None if text is None else PauseReason(text)


def _encode_bool(value: bool) -> int:
    return int(bool(value))


# Feed fields that are not stored as they are: name -> (encode, decode).
_FEED_CODECS: dict[str, tuple[Callable[[Any], Any], Callable[[Any], Any]]] = {
    "channel_kind": (lambda v: ChannelKind(v).value, ChannelKind),
    "embed": (_encode_embed, _decode_embed),
    "buttons": (_encode_buttons, _decode_buttons),
    "mention_role_ids": (_encode_ids, _decode_ids),
    "post_as": (lambda v: PostAs(v).value, PostAs),
    "forum_tag_ids": (_encode_ids, _decode_ids),
    "forum_cover": (_encode_bool, bool),
    "paused": (_encode_pause, _decode_pause),
    "warned": (_encode_bool, bool),
}

_FEED_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(Feed))
_FEED_FIXED = frozenset({"id", "server_id", "created_at"})


def _encode_feed_value(name: str, value: Any) -> Any:
    codec = _FEED_CODECS.get(name)
    return codec[0](value) if codec else value


def _feed(row: sqlite3.Row) -> Feed:
    values = {}
    for name in _FEED_FIELDS:
        codec = _FEED_CODECS.get(name)
        values[name] = codec[1](row[name]) if codec else row[name]
    return Feed(**values)


def _filter(row: sqlite3.Row) -> Filter:
    return Filter(
        id=row["id"],
        feed_id=row["feed_id"],
        list=FilterList(row["list"]),
        field=FilterField(row["field"]),
        word=row["word"],
    )


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(
        server_id=row["server_id"],
        target_id=row["target_id"],
        target_kind=TargetKind(row["target_kind"]),
        level=Level(row["level"]),
    )


def _server(row: sqlite3.Row) -> Server:
    return Server(
        server_id=row["server_id"],
        logs_channel_id=row["logs_channel_id"],
        removed_at=row["removed_at"],
    )


def _encode_changes(changes: Iterable[Change]) -> str:
    return json.dumps([[c.label, c.before, c.after] for c in changes], ensure_ascii=False)


def _log_entry(row: sqlite3.Row) -> LogEntry:
    return LogEntry(
        id=row["id"],
        server_id=row["server_id"],
        at=row["at"],
        actor_id=row["actor_id"],
        actor_name=row["actor_name"],
        kind=LogKind(row["kind"]),
        feed_id=row["feed_id"],
        feed_name=row["feed_name"],
        channel_id=row["channel_id"],
        feed_url=row["feed_url"],
        changes=tuple(Change(*c) for c in json.loads(row["changes"])),
        detail=row["detail"],
    )


_PAUSE_KIND_VALUES: tuple[str, ...] = tuple(sorted(kind.value for kind in PAUSE_KINDS))

# The Log entries that still say something about a Feed as it is now: who added a Feed
# that exists, and the latest pause of a Feed that is paused. feed_attributions reads
# them and prune_log_entries keeps them, so the two must agree: both use these.
_ADDED_ENTRIES = (
    "SELECT e.id FROM log_entries e JOIN feeds f ON f.id = e.feed_id AND f.server_id = e.server_id"
    " WHERE e.kind = ? AND {where}"
)
_PAUSE_ENTRIES = (
    "SELECT MAX(e.id) FROM log_entries e"
    " JOIN feeds f ON f.id = e.feed_id AND f.server_id = e.server_id"
    f" WHERE f.paused IS NOT NULL AND e.kind IN ({','.join('?' * len(_PAUSE_KIND_VALUES))})"
    " AND {where} GROUP BY e.feed_id"
)


def _chunks(keys: Iterable[str]) -> Iterator[list[str]]:
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), _CHUNK):
        yield unique[start : start + _CHUNK]


def _marks(count: int) -> str:
    return ",".join("?" * count)


def _log_filter(
    server_id: int,
    feed_id: int | None,
    actor_id: int | None,
    by_bot: bool,
    kinds: Iterable[LogKind] | None,
) -> tuple[str, tuple[Any, ...]]:
    """The WHERE clause and its values for list_log_entries and count_log_entries."""
    if by_bot and actor_id is not None:
        raise ValueError("Give actor_id or by_bot, not both")
    clauses: list[str] = ["server_id = ?"]
    params: list[Any] = [server_id]
    if feed_id is not None:
        clauses.append("feed_id = ?")
        params.append(feed_id)
    if by_bot:
        clauses.append("actor_id IS NULL")
    elif actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    if kinds is not None:
        values = [LogKind(kind).value for kind in kinds]
        clauses.append(f"kind IN ({_marks(len(values))})")
        params.extend(values)
    return " AND ".join(clauses), tuple(params)


class Database:
    def __init__(self, path: Path | str) -> None:
        memory = str(path) == ":memory:"
        if not memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            # The database holds webhook tokens: only the bot's own user may read it. SQLite
            # gives its -wal and -shm files the same mode as this one.
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(path, 0o600)
        # isolation_level=None: statements commit on their own; _transaction() groups them.
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if not memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # -- plumbing --

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> None:
        version = self.schema_version
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"The database is at schema version {version}, but this bot only knows "
                f"up to {len(MIGRATIONS)}. It was written by a newer version of the bot."
            )
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                # executescript commits any open transaction first, so the script carries its own.
                self._conn.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\n;PRAGMA user_version = {number:d};\nCOMMIT;"
                )
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        try:
            self._conn.execute("COMMIT")
        except BaseException:
            # A COMMIT that fails can leave the transaction open, and with it the write lock.
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        # Always drain the cursor: a half-read RETURNING statement would keep its write open.
        return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _changed(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return self._conn.execute(sql, params).rowcount

    def _insert_server(self, server_id: int) -> None:
        self._conn.execute("INSERT OR IGNORE INTO servers (server_id) VALUES (?)", (server_id,))

    # -- Servers --

    def get_server(self, server_id: int) -> Server | None:
        row = self._one("SELECT * FROM servers WHERE server_id = ?", (server_id,))
        return _server(row) if row else None

    def ensure_server(self, server_id: int) -> Server:
        """Return the Server, creating it if this is the first time it is seen."""
        self._insert_server(server_id)
        row = self._one("SELECT * FROM servers WHERE server_id = ?", (server_id,))
        assert row is not None
        return _server(row)

    def set_logs_channel(self, server_id: int, channel_id: int | None) -> None:
        """Set the Server's Logs channel, or clear it with None."""
        self._conn.execute(
            "INSERT INTO servers (server_id, logs_channel_id) VALUES (?, ?) "
            "ON CONFLICT (server_id) DO UPDATE SET logs_channel_id = excluded.logs_channel_id",
            (server_id, channel_id),
        )

    def mark_server_removed(self, server_id: int, at: int) -> None:
        """Record that the bot was removed from the Server. Unknown Servers are left alone."""
        self._conn.execute("UPDATE servers SET removed_at = ? WHERE server_id = ?", (at, server_id))

    def mark_server_returned(self, server_id: int) -> None:
        """Record that the bot is in the Server (again), creating it if needed."""
        self._conn.execute(
            "INSERT INTO servers (server_id) VALUES (?) "
            "ON CONFLICT (server_id) DO UPDATE SET removed_at = NULL",
            (server_id,),
        )

    def present_server_ids(self) -> list[int]:
        """The ids of the Servers that are not recorded as removed."""
        rows = self._all(
            "SELECT server_id FROM servers WHERE removed_at IS NULL ORDER BY server_id"
        )
        return [row["server_id"] for row in rows]

    def purge_removed_servers(self, before: int) -> list[int]:
        """Delete Servers removed before `before`, with everything they own. Returns their ids."""
        with self._transaction():
            # Webhooks hang off channels, not Servers, so the cascade cannot reach them. Those of
            # channels with no Feed name no Server: the periodic cleanup of unused webhooks
            # retries them, and a Server the bot has left answers 403 or 404.
            self._conn.execute(
                "DELETE FROM webhooks WHERE channel_id IN ("
                " SELECT f.channel_id FROM feeds f JOIN servers s ON s.server_id = f.server_id"
                " WHERE s.removed_at < ?)",
                (before,),
            )
            rows = self._all(
                "DELETE FROM servers WHERE removed_at < ? RETURNING server_id", (before,)
            )
        return sorted(row["server_id"] for row in rows)

    # -- Grants --

    def set_grant(
        self, server_id: int, target_id: int, target_kind: TargetKind, level: Level
    ) -> Grant:
        """Give a role or member a level in a Server, replacing any Grant it already has."""
        grant = Grant(server_id, target_id, TargetKind(target_kind), Level(level))
        with self._transaction():
            self._insert_server(server_id)
            self._conn.execute(
                "INSERT INTO grants (server_id, target_id, target_kind, level) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (server_id, target_id) DO UPDATE SET "
                "target_kind = excluded.target_kind, level = excluded.level",
                (server_id, target_id, grant.target_kind.value, grant.level.value),
            )
        return grant

    def remove_grant(self, server_id: int, target_id: int) -> bool:
        """Returns whether there was a Grant to remove."""
        return bool(
            self._changed(
                "DELETE FROM grants WHERE server_id = ? AND target_id = ?", (server_id, target_id)
            )
        )

    def list_grants(self, server_id: int) -> list[Grant]:
        rows = self._all(
            "SELECT * FROM grants WHERE server_id = ? ORDER BY target_kind, target_id",
            (server_id,),
        )
        return [_grant(row) for row in rows]

    # -- Feeds --

    def create_feed(
        self,
        *,
        server_id: int,
        channel_id: int,
        channel_kind: ChannelKind,
        name: str,
        url: str,
        now: int,
        next_check_at: int | None = None,  # defaults to now: checked on the next pass
        interval_s: int = DEFAULT_INTERVAL_S,
        source_title: str = "",
        source_link: str = "",
        text_template: str = DEFAULT_TEXT_TEMPLATE,
        embed: EmbedSpec | None = None,
        buttons: Iterable[ButtonSpec] = (),
        mention_role_ids: Iterable[int] = (),
        post_as: PostAs = PostAs.BOT,
        custom_name: str = "",
        custom_avatar: str = "",
        forum_title_template: str = DEFAULT_FORUM_TITLE_TEMPLATE,
        forum_tag_ids: Iterable[int] = (),
        forum_cover: bool = True,
    ) -> Feed:
        """Add a Feed, creating its Server if needed. Other fields start empty; see update_feed."""
        values: dict[str, Any] = {
            "server_id": server_id,
            "channel_id": channel_id,
            "channel_kind": channel_kind,
            "name": name,
            "url": url,
            "interval_s": interval_s,
            "source_title": source_title,
            "source_link": source_link,
            "text_template": text_template,
            "embed": embed,
            "buttons": buttons,
            "mention_role_ids": mention_role_ids,
            "post_as": post_as,
            "custom_name": custom_name,
            "custom_avatar": custom_avatar,
            "site_name": "",
            "site_icon": "",
            "site_checked_at": None,
            "forum_title_template": forum_title_template,
            "forum_tag_ids": forum_tag_ids,
            "forum_cover": forum_cover,
            "paused": None,
            "etag": None,
            "last_modified": None,
            "next_check_at": now if next_check_at is None else next_check_at,
            "fail_count": 0,
            "failing_since": None,
            "warned": False,
            "last_error": "",
            "last_success_at": None,
            "skipped_count": 0,
            "skipped_since": None,
            "last_checked_at": None,
            "rate_limited_since": None,
            "created_at": now,
        }
        params = tuple(_encode_feed_value(name, value) for name, value in values.items())
        with self._transaction():
            self._insert_server(server_id)
            rows = self._all(
                f"INSERT INTO feeds ({', '.join(values)}) VALUES ({_marks(len(values))}) "
                "RETURNING *",
                params,
            )
        return _feed(rows[0])

    def get_feed(self, feed_id: int) -> Feed | None:
        row = self._one("SELECT * FROM feeds WHERE id = ?", (feed_id,))
        return _feed(row) if row else None

    def list_feeds(self, server_id: int) -> list[Feed]:
        """A Server's Feeds, ordered by name without regard to case."""
        rows = self._all(
            "SELECT * FROM feeds WHERE server_id = ? ORDER BY name COLLATE NOCASE, id",
            (server_id,),
        )
        return [_feed(row) for row in rows]

    def list_channel_feeds(self, channel_id: int) -> list[Feed]:
        rows = self._all(
            "SELECT * FROM feeds WHERE channel_id = ? ORDER BY name COLLATE NOCASE, id",
            (channel_id,),
        )
        return [_feed(row) for row in rows]

    def count_feeds(self, server_id: int) -> int:
        row = self._one("SELECT COUNT(*) FROM feeds WHERE server_id = ?", (server_id,))
        assert row is not None
        return row[0]

    def delete_feed(self, feed_id: int) -> bool:
        """Delete a Feed with its Filters and Seen items. Returns whether it existed."""
        return bool(self._changed("DELETE FROM feeds WHERE id = ?", (feed_id,)))

    def due_feeds(self, now: int) -> list[Feed]:
        """Feeds to Check now, most overdue first. Leaves out Paused feeds and removed Servers."""
        rows = self._all(
            "SELECT f.* FROM feeds f JOIN servers s ON s.server_id = f.server_id "
            "WHERE f.paused IS NULL AND s.removed_at IS NULL AND f.next_check_at <= ? "
            "ORDER BY f.next_check_at, f.id",
            (now,),
        )
        return [_feed(row) for row in rows]

    def update_feed(self, feed_id: int, **changes: Any) -> Feed:
        """Change any Feed fields except id, server_id and created_at; returns the new Feed.

        Raises ValueError for a field that cannot be set and FeedNotFound (a KeyError)
        if there is no such Feed.
        """
        bad = sorted(n for n in changes if n not in _FEED_FIELDS or n in _FEED_FIXED)
        if bad:
            raise ValueError(f"Cannot set Feed field(s): {', '.join(bad)}")
        if not changes:
            feed = self.get_feed(feed_id)
            if feed is None:
                raise FeedNotFound(feed_id)
            return feed
        # Column names are checked against the Feed's own fields above.
        assignments = ", ".join(f"{name} = ?" for name in changes)
        params = tuple(_encode_feed_value(name, value) for name, value in changes.items())
        row = self._one(
            f"UPDATE feeds SET {assignments} WHERE id = ? RETURNING *", (*params, feed_id)
        )
        if row is None:
            raise FeedNotFound(feed_id)
        return _feed(row)

    # -- Filters --

    def add_filter(self, feed_id: int, list_: FilterList, field: FilterField, word: str) -> Filter:
        row = self._one(
            "INSERT INTO filters (feed_id, list, field, word) VALUES (?, ?, ?, ?) RETURNING *",
            (feed_id, FilterList(list_).value, FilterField(field).value, word),
        )
        assert row is not None
        return _filter(row)

    def remove_filter(self, filter_id: int) -> bool:
        """Returns whether there was a Filter to remove."""
        return bool(self._changed("DELETE FROM filters WHERE id = ?", (filter_id,)))

    def list_filters(self, feed_id: int) -> list[Filter]:
        rows = self._all("SELECT * FROM filters WHERE feed_id = ? ORDER BY id", (feed_id,))
        return [_filter(row) for row in rows]

    # -- Seen items --

    def seen_states(self, feed_id: int, keys: Iterable[str]) -> dict[str, tuple[ItemStatus, int]]:
        """The (status, attempts) of each given Item key the Feed has recorded."""
        states: dict[str, tuple[ItemStatus, int]] = {}
        for chunk in _chunks(keys):
            rows = self._all(
                "SELECT key, status, attempts FROM seen_items "
                f"WHERE feed_id = ? AND key IN ({_marks(len(chunk))})",
                (feed_id, *chunk),
            )
            for row in rows:
                states[row["key"]] = (ItemStatus(row["status"]), row["attempts"])
        return states

    def record_seen(self, feed_id: int, items: Iterable[tuple[str, ItemStatus]], now: int) -> None:
        """Record Items as Seen items, or change the status of ones already recorded.

        Also counts as the source listing them at `now`. Attempt counts are kept.
        """
        params = [(feed_id, key, ItemStatus(status).value, now, now) for key, status in items]
        if not params:
            return
        with self._transaction():
            self._conn.executemany(
                "INSERT INTO seen_items (feed_id, key, status, first_seen_at, last_listed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (feed_id, key) DO UPDATE SET "
                "status = excluded.status, last_listed_at = excluded.last_listed_at",
                params,
            )

    def bump_attempts(self, feed_id: int, key: str) -> int:
        """Count one more delivery attempt for a Seen item and return the new count.

        Raises KeyError if the Item has not been recorded.
        """
        row = self._one(
            "UPDATE seen_items SET attempts = attempts + 1 WHERE feed_id = ? AND key = ? "
            "RETURNING attempts",
            (feed_id, key),
        )
        if row is None:
            raise KeyError((feed_id, key))
        return row["attempts"]

    def count_skipped(self, feed_id: int, now: int) -> None:
        """Count one more Item whose delivery the Feed gave up on. A deleted Feed is left alone."""
        self._conn.execute(
            "UPDATE feeds SET skipped_count = skipped_count + 1, "
            "skipped_since = COALESCE(skipped_since, ?) WHERE id = ?",
            (now, feed_id),
        )

    def touch_seen(self, feed_id: int, keys: Iterable[str], now: int) -> None:
        """Note that the source still lists these Items, so prune_seen keeps them."""
        chunks = list(_chunks(keys))
        if not chunks:
            return
        with self._transaction():
            for chunk in chunks:
                self._conn.execute(
                    "UPDATE seen_items SET last_listed_at = ? "
                    f"WHERE feed_id = ? AND key IN ({_marks(len(chunk))})",
                    (now, feed_id, *chunk),
                )

    def prune_seen(self, feed_id: int, before: int) -> int:
        """Forget Seen items the source last listed before `before`. Returns how many."""
        return self._changed(
            "DELETE FROM seen_items WHERE feed_id = ? AND last_listed_at < ?", (feed_id, before)
        )

    def has_seen_items(self, feed_id: int) -> bool:
        row = self._one("SELECT 1 FROM seen_items WHERE feed_id = ? LIMIT 1", (feed_id,))
        return row is not None

    # -- Webhooks (one per channel) --

    def get_webhook(self, channel_id: int) -> tuple[int, str] | None:
        """The channel's (webhook_id, token), if one is stored."""
        row = self._one(
            "SELECT webhook_id, token FROM webhooks WHERE channel_id = ?", (channel_id,)
        )
        return (row["webhook_id"], row["token"]) if row else None

    def set_webhook(self, channel_id: int, webhook_id: int, token: str) -> None:
        self._conn.execute(
            "INSERT INTO webhooks (channel_id, webhook_id, token) VALUES (?, ?, ?) "
            "ON CONFLICT (channel_id) DO UPDATE SET "
            "webhook_id = excluded.webhook_id, token = excluded.token",
            (channel_id, webhook_id, token),
        )

    def channels_with_unused_webhook(self) -> list[int]:
        """The channels that have a stored webhook and no Feed."""
        rows = self._all(
            "SELECT channel_id FROM webhooks "
            "WHERE channel_id NOT IN (SELECT channel_id FROM feeds) ORDER BY channel_id"
        )
        return [row["channel_id"] for row in rows]

    def delete_webhook(self, channel_id: int, webhook_id: int | None = None) -> bool:
        """Returns whether there was a webhook to delete.

        With `webhook_id`, only that webhook is deleted: the channel may meanwhile have been
        given a new one, which must stay.
        """
        if webhook_id is None:
            return bool(self._changed("DELETE FROM webhooks WHERE channel_id = ?", (channel_id,)))
        return bool(
            self._changed(
                "DELETE FROM webhooks WHERE channel_id = ? AND webhook_id = ?",
                (channel_id, webhook_id),
            )
        )

    # -- Log entries --

    def add_log_entry(
        self,
        *,
        server_id: int,
        at: int,
        actor_id: int | None,
        actor_name: str,
        kind: LogKind,
        feed_id: int | None = None,
        feed_name: str = "",
        channel_id: int | None = None,
        feed_url: str = "",
        changes: Iterable[Change] = (),
        detail: str = "",
    ) -> LogEntry:
        """Save one Log entry, creating its Server if needed. `actor_id` None is the bot."""
        draft = LogEntry(
            id=0,
            server_id=server_id,
            at=at,
            actor_id=actor_id,
            actor_name=actor_name,
            kind=LogKind(kind),
            feed_id=feed_id,
            feed_name=feed_name,
            channel_id=channel_id,
            feed_url=feed_url,
            changes=tuple(changes),
            detail=detail,
        )
        return self.add_log_entries([draft])[0]

    def add_log_entries(self, entries: Iterable[LogEntry]) -> list[LogEntry]:
        """Save many Log entries in one transaction: all of them or none.

        The `id` of a given entry is ignored; the saved entries are returned, in the same
        order, with the ids they were given. Their Servers are created if needed.
        """
        drafts = list(entries)
        saved: list[LogEntry] = []
        if not drafts:
            return saved
        with self._transaction():
            for server_id in {draft.server_id for draft in drafts}:
                self._insert_server(server_id)
            for draft in drafts:
                row = self._one(
                    "INSERT INTO log_entries (server_id, at, actor_id, actor_name, kind, feed_id,"
                    " feed_name, channel_id, feed_url, changes, detail)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                    (
                        draft.server_id,
                        draft.at,
                        draft.actor_id,
                        draft.actor_name,
                        LogKind(draft.kind).value,
                        draft.feed_id,
                        draft.feed_name,
                        draft.channel_id,
                        draft.feed_url,
                        _encode_changes(draft.changes),
                        draft.detail,
                    ),
                )
                assert row is not None
                saved.append(dataclasses.replace(draft, id=row["id"], kind=LogKind(draft.kind)))
        return saved

    def list_log_entries(
        self,
        server_id: int,
        *,
        feed_id: int | None = None,
        actor_id: int | None = None,
        by_bot: bool = False,
        kinds: Iterable[LogKind] | None = None,
        limit: int,
        offset: int = 0,
    ) -> list[LogEntry]:
        """A Server's Log entries, newest first, `limit` of them after skipping `offset`.

        Each filter that is given narrows the list: one Feed (also a removed one), one
        member as `actor_id`, the bot itself with `by_bot=True`, and any of `kinds`.
        Raises ValueError if both `actor_id` and `by_bot` are given.
        """
        where, params = _log_filter(server_id, feed_id, actor_id, by_bot, kinds)
        rows = self._all(
            f"SELECT * FROM log_entries WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, max(limit, 0), max(offset, 0)),
        )
        return [_log_entry(row) for row in rows]

    def count_log_entries(
        self,
        server_id: int,
        *,
        feed_id: int | None = None,
        actor_id: int | None = None,
        by_bot: bool = False,
        kinds: Iterable[LogKind] | None = None,
    ) -> int:
        """How many Log entries list_log_entries has for the same filters."""
        where, params = _log_filter(server_id, feed_id, actor_id, by_bot, kinds)
        row = self._one(f"SELECT COUNT(*) FROM log_entries WHERE {where}", params)
        assert row is not None
        return row[0]

    def feed_attributions(self, server_id: int) -> dict[int, FeedAttribution]:
        """Who added and who paused each Feed of a Server, by Feed id.

        Every Feed the Server has is in the result. `added` is None for a Feed whose
        "added" Log entry does not exist (any more); `paused` is the latest pause, by a
        member or the bot, and only for a Feed that is paused now.
        """
        rows = self._all("SELECT id FROM feeds WHERE server_id = ?", (server_id,))
        found = self._attributions("f.server_id = ?", server_id)
        return {row["id"]: found.get(row["id"], FeedAttribution()) for row in rows}

    def feed_attribution(self, feed_id: int) -> FeedAttribution:
        """Who added and who paused one Feed; see feed_attributions. Empty for no such Feed."""
        return self._attributions("f.id = ?", feed_id).get(feed_id, FeedAttribution())

    def _attributions(self, where: str, param: int) -> dict[int, FeedAttribution]:
        # Two statements whatever the number of Feeds.
        added: dict[int, LogEntry] = {}
        for row in self._all(
            f"SELECT * FROM log_entries WHERE id IN ({_ADDED_ENTRIES.format(where=where)})"
            " ORDER BY id DESC",
            (LogKind.FEED_ADDED.value, param),
        ):
            added[row["feed_id"]] = _log_entry(row)  # the earliest wins, should there be two
        paused: dict[int, LogEntry] = {}
        for row in self._all(
            f"SELECT * FROM log_entries WHERE id IN ({_PAUSE_ENTRIES.format(where=where)})",
            (*_PAUSE_KIND_VALUES, param),
        ):
            paused[row["feed_id"]] = _log_entry(row)
        return {
            feed_id: FeedAttribution(added.get(feed_id), paused.get(feed_id))
            for feed_id in added.keys() | paused.keys()
        }

    def prune_log_entries(self, before: int) -> int:
        """Delete the Log entries from before `before`. Returns how many.

        Two are kept however old, because they still describe a Feed as it is: the "added"
        entry of a Feed that exists, and the latest pause of a Feed that is paused. Once the
        Feed is removed or resumed they are ordinary entries and go with the rest.
        """
        return self._changed(
            "DELETE FROM log_entries WHERE at < ?"
            f" AND id NOT IN ({_ADDED_ENTRIES.format(where='1')})"
            f" AND id NOT IN ({_PAUSE_ENTRIES.format(where='1')})",
            (before, LogKind.FEED_ADDED.value, *_PAUSE_KIND_VALUES),
        )
