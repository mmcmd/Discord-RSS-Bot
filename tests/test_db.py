from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from rssbot import db as db_module
from rssbot.db import Database, FeedNotFound
from rssbot.models import (
    DEFAULT_FORUM_TITLE_TEMPLATE,
    DEFAULT_INTERVAL_S,
    DEFAULT_TEXT_TEMPLATE,
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

SERVER = 111
CHANNEL = 222


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


def make_feed(db: Database, **overrides: object) -> Feed:
    args: dict[str, object] = {
        "server_id": SERVER,
        "channel_id": CHANNEL,
        "channel_kind": ChannelKind.MESSAGES,
        "name": "News",
        "url": "https://example.com/rss",
        "now": 1000,
    }
    args.update(overrides)
    return db.create_feed(**args)  # type: ignore[arg-type]


FULL_EMBED = EmbedSpec(
    title="{{title}}",
    description="{{description}}",
    url="{{link}}",
    image="{{image}}",
    footer="{{feed_title}}",
    colour=0xFF8800,
    fields=(FieldSpec("Author", "{{author}}", inline=True), FieldSpec("Tags", "{{categories}}")),
)

# A value for every Feed field that update_feed may set, each different from the default.
ALL_CHANGES: dict[str, object] = {
    "channel_id": 999,
    "channel_kind": ChannelKind.FORUM,
    "name": "Renamed ✨",
    "url": "https://example.org/atom.xml",
    "interval_s": 3600,
    "source_title": "Example Site",
    "source_link": "https://example.org",
    "text_template": "{{title}}\n{{link}}",
    "embed": FULL_EMBED,
    "buttons": (ButtonSpec("Read", "{{link}}"), ButtonSpec("Site", "{{feed_link}}")),
    "mention_role_ids": (5, 2**63 - 1),
    "post_as": PostAs.CUSTOM,
    "custom_name": "Herald",
    "custom_avatar": "https://example.org/a.png",
    "site_name": "Example",
    "site_icon": "https://example.org/favicon.ico",
    "site_checked_at": 1234,
    "forum_title_template": "{{title}}",
    "forum_tag_ids": (7, 8, 9),
    "forum_cover": True,
    "paused": PauseReason.NEEDS_TAG,
    "etag": 'W/"abc"',
    "last_modified": "Wed, 07 Oct 2026 10:00:00 GMT",
    "next_check_at": 5000,
    "fail_count": 4,
    "failing_since": 4000,
    "warned": True,
    "last_error": "The site answered 500.",
    "last_success_at": 3000,
    "skipped_count": 3,
    "skipped_since": 4500,
    "last_checked_at": 4600,
    "rate_limited_since": 4550,
}


# -- opening and migrations --


def test_new_database_is_at_latest_version(db: Database) -> None:
    assert db.schema_version == len(db_module.MIGRATIONS)


def test_reopening_a_file_keeps_data_and_changes_nothing(tmp_path) -> None:
    path = tmp_path / "data" / "bot.db"  # the parent directory is created
    first = Database(path)
    feed = make_feed(first, embed=FULL_EMBED)
    first.add_filter(feed.id, FilterList.BLOCK, FilterField.TITLE, "ad")
    first.record_seen(feed.id, [("a", ItemStatus.DELIVERED)], now=1000)
    first.set_grant(SERVER, 5, TargetKind.ROLE, Level.MANAGER)
    first.set_webhook(CHANNEL, 77, "tok")
    first.close()

    second = Database(str(path))
    assert second.schema_version == len(db_module.MIGRATIONS)
    assert second.get_feed(feed.id) == feed
    assert [f.word for f in second.list_filters(feed.id)] == ["ad"]
    assert second.seen_states(feed.id, ["a"]) == {"a": (ItemStatus.DELIVERED, 0)}
    assert second.list_grants(SERVER) == [Grant(SERVER, 5, TargetKind.ROLE, Level.MANAGER)]
    assert second.get_webhook(CHANNEL) == (77, "tok")
    second.close()


def test_file_database_uses_wal_and_foreign_keys(tmp_path) -> None:
    database = Database(tmp_path / "bot.db")
    conn = database._conn
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    database.close()


def test_later_migrations_apply_on_top_of_existing_data(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bot.db"
    first = Database(path)
    feed = make_feed(first)
    first.close()

    extra = "CREATE TABLE extra (n INTEGER); INSERT INTO extra VALUES (1);"
    monkeypatch.setattr(db_module, "MIGRATIONS", (*db_module.MIGRATIONS, extra))
    second = Database(path)
    assert second.schema_version == len(db_module.MIGRATIONS)
    assert second.get_feed(feed.id) == feed
    second.close()

    # Up to date now: the new script must not run a second time.
    third = Database(path)
    assert third._conn.execute("SELECT COUNT(*) FROM extra").fetchone()[0] == 1
    third.close()


def test_failed_migration_rolls_back_whole_script(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bot.db"
    Database(path).close()
    base = len(db_module.MIGRATIONS)

    broken = "CREATE TABLE half (n INTEGER); CREATE TABLE servers (oops INTEGER);"
    monkeypatch.setattr(db_module, "MIGRATIONS", (*db_module.MIGRATIONS, broken))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)

    monkeypatch.undo()
    database = Database(path)
    assert database.schema_version == base
    tables = {r[0] for r in database._conn.execute("SELECT name FROM sqlite_master")}
    assert "half" not in tables
    database.close()


def test_database_from_a_newer_bot_is_refused(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bot.db"
    Database(path).close()
    monkeypatch.setattr(db_module, "MIGRATIONS", ())
    with pytest.raises(RuntimeError, match="newer version"):
        Database(path)


# -- Servers --


def test_get_server_unknown(db: Database) -> None:
    assert db.get_server(SERVER) is None


def test_ensure_server_creates_once(db: Database) -> None:
    assert db.ensure_server(SERVER) == Server(SERVER, None, None)
    db.set_logs_channel(SERVER, 42)
    assert db.ensure_server(SERVER) == Server(SERVER, 42, None)
    assert db.get_server(SERVER) == Server(SERVER, 42, None)


def test_set_and_clear_logs_channel(db: Database) -> None:
    db.set_logs_channel(SERVER, 42)  # creates the Server too
    assert db.get_server(SERVER) == Server(SERVER, 42, None)
    db.set_logs_channel(SERVER, None)
    assert db.get_server(SERVER) == Server(SERVER, None, None)


def test_mark_removed_and_returned(db: Database) -> None:
    db.set_logs_channel(SERVER, 42)
    db.mark_server_removed(SERVER, 500)
    assert db.get_server(SERVER) == Server(SERVER, 42, 500)
    db.mark_server_returned(SERVER)
    assert db.get_server(SERVER) == Server(SERVER, 42, None)


def test_mark_removed_ignores_unknown_and_returned_creates(db: Database) -> None:
    db.mark_server_removed(SERVER, 500)
    assert db.get_server(SERVER) is None
    db.mark_server_returned(SERVER)
    assert db.get_server(SERVER) == Server(SERVER, None, None)


def test_present_server_ids_leaves_out_removed_servers(db: Database) -> None:
    assert db.present_server_ids() == []
    db.ensure_server(3)
    db.ensure_server(1)
    db.ensure_server(2)
    db.mark_server_removed(2, 500)
    assert db.present_server_ids() == [1, 3]
    db.mark_server_returned(2)
    assert db.present_server_ids() == [1, 2, 3]


def test_count_skipped_counts_up_and_keeps_the_first_time(db: Database) -> None:
    feed = make_feed(db)
    assert (feed.skipped_count, feed.skipped_since) == (0, None)
    db.count_skipped(feed.id, now=50)
    db.count_skipped(feed.id, now=90)
    stored = db.get_feed(feed.id)
    assert stored is not None
    assert (stored.skipped_count, stored.skipped_since) == (2, 50)
    db.count_skipped(feed.id + 1, now=90)  # no such Feed: nothing to do


def test_a_version_1_database_gains_the_skipped_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.db"
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:1])
        old = Database(path)
        old._conn.execute("INSERT INTO servers (server_id) VALUES (1)")
        columns = {name: 0 for name in db_module._FEED_FIELDS if name != "id"}
        del columns["skipped_count"], columns["skipped_since"]
        del columns["last_checked_at"], columns["rate_limited_since"]
        columns["last_success_at"] = 700
        columns.update(channel_kind="messages", post_as="bot", name="Old", url="https://e.com/f")
        columns.update(server_id=1, buttons="[]", mention_role_ids="[]", forum_tag_ids="[]")
        columns.update(embed=None, paused=None)
        marks = ", ".join("?" for _ in columns)
        old._conn.execute(
            f"INSERT INTO feeds ({', '.join(columns)}) VALUES ({marks})", tuple(columns.values())
        )
        old.close()
    database = Database(path)
    try:
        assert database.schema_version == len(db_module.MIGRATIONS)
        (feed,) = database.list_feeds(1)
        assert (feed.name, feed.skipped_count, feed.skipped_since) == ("Old", 0, None)
        # A Feed from before the column starts from the last time it worked.
        assert (feed.last_checked_at, feed.rate_limited_since) == (700, None)
    finally:
        database.close()


def test_purge_removed_servers_cascades(db: Database) -> None:
    old = make_feed(db, server_id=1, channel_id=10)
    edge = make_feed(db, server_id=2, channel_id=20)
    live = make_feed(db, server_id=3, channel_id=30)
    for feed in (old, edge, live):
        db.set_grant(feed.server_id, 5, TargetKind.ROLE, Level.ADMIN)
        db.add_filter(feed.id, FilterList.BLOCK, FilterField.ANY, "x")
        db.record_seen(feed.id, [("k", ItemStatus.SEEN)], now=1)
        db.set_webhook(feed.channel_id, 1, "t")
    db.mark_server_removed(1, 99)
    db.mark_server_removed(2, 100)

    assert db.purge_removed_servers(before=100) == [1]

    assert db.get_server(1) is None
    assert db.list_grants(1) == []
    assert db.get_feed(old.id) is None
    assert db.count_feeds(1) == 0
    assert db.list_filters(old.id) == []
    assert not db.has_seen_items(old.id)
    assert db.get_webhook(10) is None
    for feed in (edge, live):
        assert db.get_server(feed.server_id) is not None
        assert len(db.list_grants(feed.server_id)) == 1
        assert db.get_feed(feed.id) == feed
        assert len(db.list_filters(feed.id)) == 1
        assert db.has_seen_items(feed.id)
        assert db.get_webhook(feed.channel_id) == (1, "t")

    assert db.purge_removed_servers(before=100) == []
    assert db.purge_removed_servers(before=101) == [2]


# -- Grants --


def test_grants_set_replace_list_remove(db: Database) -> None:
    assert db.list_grants(SERVER) == []
    granted = db.set_grant(SERVER, 20, TargetKind.ROLE, Level.MANAGER)  # creates the Server too
    assert granted == Grant(SERVER, 20, TargetKind.ROLE, Level.MANAGER)
    db.set_grant(SERVER, 10, TargetKind.MEMBER, Level.ADMIN)
    db.set_grant(999, 20, TargetKind.ROLE, Level.ADMIN)

    db.set_grant(SERVER, 20, TargetKind.ROLE, Level.ADMIN)  # replaces the level
    assert db.list_grants(SERVER) == [
        Grant(SERVER, 10, TargetKind.MEMBER, Level.ADMIN),
        Grant(SERVER, 20, TargetKind.ROLE, Level.ADMIN),
    ]

    assert db.remove_grant(SERVER, 20) is True
    assert db.remove_grant(SERVER, 20) is False
    assert db.list_grants(SERVER) == [Grant(SERVER, 10, TargetKind.MEMBER, Level.ADMIN)]
    assert db.list_grants(999) == [Grant(999, 20, TargetKind.ROLE, Level.ADMIN)]


# -- Feeds --


def test_create_feed_defaults(db: Database) -> None:
    feed = make_feed(db)
    assert feed == Feed(
        id=feed.id,
        server_id=SERVER,
        channel_id=CHANNEL,
        channel_kind=ChannelKind.MESSAGES,
        name="News",
        url="https://example.com/rss",
        interval_s=DEFAULT_INTERVAL_S,
        source_title="",
        source_link="",
        text_template=DEFAULT_TEXT_TEMPLATE,
        embed=None,
        buttons=(),
        mention_role_ids=(),
        post_as=PostAs.BOT,
        custom_name="",
        custom_avatar="",
        site_name="",
        site_icon="",
        site_checked_at=None,
        forum_title_template=DEFAULT_FORUM_TITLE_TEMPLATE,
        forum_tag_ids=(),
        forum_cover=True,
        paused=None,
        etag=None,
        last_modified=None,
        next_check_at=1000,
        fail_count=0,
        failing_since=None,
        warned=False,
        last_error="",
        last_success_at=None,
        skipped_count=0,
        skipped_since=None,
        last_checked_at=None,
        rate_limited_since=None,
        created_at=1000,
    )
    assert db.get_feed(feed.id) == feed
    assert db.get_server(SERVER) == Server(SERVER, None, None)  # created along the way


def test_create_feed_with_every_settable_field(db: Database) -> None:
    feed = make_feed(
        db,
        channel_kind=ChannelKind.FORUM,
        next_check_at=1500,
        interval_s=900,
        source_title="Site",
        source_link="https://example.com",
        text_template="{{title}}",
        embed=FULL_EMBED,
        buttons=[ButtonSpec("Read", "{{link}}")],  # any iterable is accepted
        mention_role_ids=[1, 2],
        post_as=PostAs.SITE,
        custom_name="N",
        custom_avatar="https://example.com/a.png",
        forum_title_template="{{author}}",
        forum_tag_ids=[3],
        forum_cover=True,
    )
    assert feed.channel_kind is ChannelKind.FORUM
    assert feed.next_check_at == 1500
    assert feed.created_at == 1000
    assert feed.interval_s == 900
    assert (feed.source_title, feed.source_link) == ("Site", "https://example.com")
    assert feed.text_template == "{{title}}"
    assert feed.embed == FULL_EMBED
    assert feed.buttons == (ButtonSpec("Read", "{{link}}"),)
    assert feed.mention_role_ids == (1, 2)
    assert feed.post_as is PostAs.SITE
    assert (feed.custom_name, feed.custom_avatar) == ("N", "https://example.com/a.png")
    assert feed.forum_title_template == "{{author}}"
    assert feed.forum_tag_ids == (3,)
    assert feed.forum_cover is True
    assert db.get_feed(feed.id) == feed


def test_get_feed_unknown(db: Database) -> None:
    assert db.get_feed(12345) is None


def test_update_feed_round_trips_every_field(db: Database) -> None:
    settable = {f.name for f in dataclasses.fields(Feed)} - {"id", "server_id", "created_at"}
    assert set(ALL_CHANGES) == settable  # a new Feed field must be added to this test

    feed = make_feed(db)
    updated = db.update_feed(feed.id, **ALL_CHANGES)
    assert updated == dataclasses.replace(feed, **ALL_CHANGES)
    assert db.get_feed(feed.id) == updated
    # Exact types, not just equal values.
    assert updated.forum_cover is True and updated.warned is True
    assert isinstance(updated.buttons, tuple) and isinstance(updated.embed.fields, tuple)
    assert updated.paused is PauseReason.NEEDS_TAG

    # And back to the empty values.
    cleared: dict[str, object] = {
        "embed": None,
        "buttons": (),
        "mention_role_ids": (),
        "forum_tag_ids": (),
        "forum_cover": False,
        "warned": False,
        "paused": None,
        "etag": None,
        "last_modified": None,
        "site_checked_at": None,
        "failing_since": None,
        "last_success_at": None,
    }
    again = db.update_feed(feed.id, **cleared)
    assert again == dataclasses.replace(updated, **cleared)
    assert db.get_feed(feed.id) == again
    assert again.forum_cover is False and again.warned is False


def test_embed_round_trips_empty_and_without_colour(db: Database) -> None:
    feed = make_feed(db, embed=EmbedSpec())
    assert feed.embed == EmbedSpec()
    assert feed.embed.colour is None and feed.embed.fields == ()
    zero = db.update_feed(feed.id, embed=EmbedSpec(title="t", colour=0))
    assert zero.embed == EmbedSpec(title="t", colour=0)
    assert zero.embed.colour == 0 and zero.embed.colour is not None


def test_update_feed_changes_only_named_fields(db: Database) -> None:
    feed = make_feed(db)
    other = make_feed(db, name="Other")
    updated = db.update_feed(feed.id, name="New", fail_count=2)
    assert updated == dataclasses.replace(feed, name="New", fail_count=2)
    assert db.get_feed(other.id) == other


def test_update_feed_without_changes_returns_feed(db: Database) -> None:
    feed = make_feed(db)
    assert db.update_feed(feed.id) == feed


@pytest.mark.parametrize("name", ["id", "server_id", "created_at", "nope", "name = 'x' --"])
def test_update_feed_rejects_fields(db: Database, name: str) -> None:
    feed = make_feed(db)
    with pytest.raises(ValueError, match="Cannot set"):
        db.update_feed(feed.id, **{name: 1})
    assert db.get_feed(feed.id) == feed


def test_update_feed_rejects_bad_enum_value(db: Database) -> None:
    feed = make_feed(db)
    with pytest.raises(ValueError):
        db.update_feed(feed.id, post_as="nobody")


def test_update_feed_unknown_feed(db: Database) -> None:
    with pytest.raises(FeedNotFound):
        db.update_feed(12345, name="x")
    with pytest.raises(KeyError):
        db.update_feed(12345)


def test_list_feeds_orders_by_name_ignoring_case(db: Database) -> None:
    for name in ("banana", "Apple", "cherry", "apple"):
        make_feed(db, name=name)
    make_feed(db, server_id=999, name="aaa")
    assert [f.name for f in db.list_feeds(SERVER)] == ["Apple", "apple", "banana", "cherry"]
    assert db.list_feeds(12345) == []


def test_list_channel_feeds_and_count(db: Database) -> None:
    a = make_feed(db, name="b", channel_id=1)
    b = make_feed(db, name="A", channel_id=1)
    c = make_feed(db, name="c", channel_id=2)
    make_feed(db, server_id=999, channel_id=3)
    assert db.list_channel_feeds(1) == [b, a]
    assert db.list_channel_feeds(2) == [c]
    assert db.list_channel_feeds(12345) == []
    assert db.count_feeds(SERVER) == 3
    assert db.count_feeds(999) == 1
    assert db.count_feeds(12345) == 0


def test_delete_feed_cascades_to_filters_and_seen_items(db: Database) -> None:
    feed = make_feed(db)
    keep = make_feed(db, name="Keep")
    for f in (feed, keep):
        db.add_filter(f.id, FilterList.MUST_HAVE, FilterField.ANY, "w")
        db.record_seen(f.id, [("k", ItemStatus.DELIVERED)], now=1)

    assert db.delete_feed(feed.id) is True
    assert db.delete_feed(feed.id) is False
    assert db.get_feed(feed.id) is None
    assert db.list_filters(feed.id) == []
    assert not db.has_seen_items(feed.id)
    assert db._conn.execute("SELECT COUNT(*) FROM filters").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0] == 1
    assert db.get_feed(keep.id) == keep
    assert len(db.list_filters(keep.id)) == 1
    assert db.has_seen_items(keep.id)


def test_due_feeds(db: Database) -> None:
    late = make_feed(db, name="late", next_check_at=900)
    later = make_feed(db, name="later", next_check_at=1000)
    earliest = make_feed(db, name="earliest", next_check_at=100)
    make_feed(db, name="not yet", next_check_at=1001)
    paused = make_feed(db, name="paused", next_check_at=100)
    db.update_feed(paused.id, paused=PauseReason.MANUAL)
    make_feed(db, name="gone", server_id=999, next_check_at=100)
    db.mark_server_removed(999, 500)

    assert db.due_feeds(1000) == [earliest, late, later]
    assert db.due_feeds(99) == []

    db.mark_server_returned(999)
    resumed = db.update_feed(paused.id, paused=None)
    due = db.due_feeds(1000)
    assert [f.name for f in due] == ["earliest", "paused", "gone", "late", "later"]
    assert resumed in due


# -- Filters --


def test_filters_add_list_remove(db: Database) -> None:
    feed = make_feed(db)
    other = make_feed(db, name="Other")
    first = db.add_filter(feed.id, FilterList.MUST_HAVE, FilterField.ANY, "python")
    second = db.add_filter(feed.id, FilterList.BLOCK, FilterField.AUTHOR, "Spam Bot")
    third = db.add_filter(other.id, FilterList.BLOCK, FilterField.CATEGORY, "ads")
    assert first == Filter(first.id, feed.id, FilterList.MUST_HAVE, FilterField.ANY, "python")
    assert second == Filter(second.id, feed.id, FilterList.BLOCK, FilterField.AUTHOR, "Spam Bot")
    assert len({first.id, second.id, third.id}) == 3

    assert db.list_filters(feed.id) == [first, second]
    assert db.list_filters(other.id) == [third]

    assert db.remove_filter(first.id) is True
    assert db.remove_filter(first.id) is False
    assert db.list_filters(feed.id) == [second]


@pytest.mark.parametrize("field", list(FilterField))
@pytest.mark.parametrize("list_", list(FilterList))
def test_filter_enums_round_trip(db: Database, list_: FilterList, field: FilterField) -> None:
    feed = make_feed(db)
    added = db.add_filter(feed.id, list_, field, "w")
    assert db.list_filters(feed.id) == [added]
    assert added.list is list_ and added.field is field


def test_add_filter_needs_a_feed(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.add_filter(12345, FilterList.BLOCK, FilterField.ANY, "w")


# -- Seen items --


def test_record_and_look_up_seen_items(db: Database) -> None:
    feed = make_feed(db)
    other = make_feed(db, name="Other")
    assert not db.has_seen_items(feed.id)
    assert db.seen_states(feed.id, ["a"]) == {}
    assert db.seen_states(feed.id, []) == {}

    db.record_seen(feed.id, [], now=10)
    assert not db.has_seen_items(feed.id)

    db.record_seen(
        feed.id,
        [
            ("a", ItemStatus.SEEN),
            ("b", ItemStatus.DELIVERED),
            ("c", ItemStatus.PENDING),
            ("d", ItemStatus.SKIPPED),
        ],
        now=10,
    )
    assert db.has_seen_items(feed.id)
    assert not db.has_seen_items(other.id)  # keyed per Feed
    assert db.seen_states(other.id, ["a"]) == {}
    assert db.seen_states(feed.id, ["a", "b", "c", "d", "missing", "a"]) == {
        "a": (ItemStatus.SEEN, 0),
        "b": (ItemStatus.DELIVERED, 0),
        "c": (ItemStatus.PENDING, 0),
        "d": (ItemStatus.SKIPPED, 0),
    }
    assert db.seen_states(feed.id, iter(["b"])) == {"b": (ItemStatus.DELIVERED, 0)}


def test_record_seen_updates_status_and_keeps_attempts_and_first_seen(db: Database) -> None:
    feed = make_feed(db)
    db.record_seen(feed.id, [("a", ItemStatus.PENDING)], now=10)
    assert db.bump_attempts(feed.id, "a") == 1
    db.record_seen(feed.id, [("a", ItemStatus.DELIVERED), ("b", ItemStatus.SEEN)], now=20)
    assert db.seen_states(feed.id, ["a", "b"]) == {
        "a": (ItemStatus.DELIVERED, 1),
        "b": (ItemStatus.SEEN, 0),
    }
    rows = db._conn.execute(
        "SELECT key, first_seen_at, last_listed_at FROM seen_items ORDER BY key"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("a", 10, 20), ("b", 20, 20)]


def test_bump_attempts(db: Database) -> None:
    feed = make_feed(db)
    db.record_seen(feed.id, [("a", ItemStatus.PENDING), ("b", ItemStatus.PENDING)], now=10)
    assert db.bump_attempts(feed.id, "a") == 1
    assert db.bump_attempts(feed.id, "a") == 2
    assert db.bump_attempts(feed.id, "b") == 1
    assert db.seen_states(feed.id, ["a", "b"]) == {
        "a": (ItemStatus.PENDING, 2),
        "b": (ItemStatus.PENDING, 1),
    }
    with pytest.raises(KeyError):
        db.bump_attempts(feed.id, "missing")


def test_touch_and_prune_seen_items(db: Database) -> None:
    feed = make_feed(db)
    other = make_feed(db, name="Other")
    db.record_seen(feed.id, [(k, ItemStatus.DELIVERED) for k in "abc"], now=100)
    db.record_seen(other.id, [("a", ItemStatus.DELIVERED)], now=100)

    db.touch_seen(feed.id, ["a", "b", "missing"], now=200)
    db.touch_seen(feed.id, [], now=999)
    assert db.seen_states(feed.id, ["missing"]) == {}  # touching never records

    assert db.prune_seen(feed.id, before=100) == 0  # strictly before
    assert db.prune_seen(feed.id, before=200) == 1
    assert set(db.seen_states(feed.id, "abc")) == {"a", "b"}
    assert db.has_seen_items(other.id)  # another Feed's Seen items are not touched

    assert db.prune_seen(feed.id, before=201) == 2
    assert not db.has_seen_items(feed.id)
    assert db.prune_seen(feed.id, before=10_000) == 0


def test_seen_items_cope_with_many_keys(db: Database) -> None:
    feed = make_feed(db)
    count = db_module._CHUNK * 2 + 300
    keys = [f"https://example.com/item/{n}" for n in range(count)]
    db.record_seen(feed.id, [(k, ItemStatus.SEEN) for k in keys], now=1)

    states = db.seen_states(feed.id, [*keys, "missing"])
    assert set(states) == set(keys)
    assert set(states.values()) == {(ItemStatus.SEEN, 0)}

    db.touch_seen(feed.id, keys[:-1], now=50)
    assert db.prune_seen(feed.id, before=50) == 1
    assert len(db.seen_states(feed.id, keys)) == count - 1


def test_record_seen_is_all_or_nothing(db: Database) -> None:
    feed = make_feed(db)
    with pytest.raises(ValueError):
        db.record_seen(feed.id, [("a", ItemStatus.SEEN), ("b", "nonsense")], now=1)  # type: ignore[list-item]
    with pytest.raises(sqlite3.IntegrityError):
        db.record_seen(12345, [("a", ItemStatus.SEEN)], now=1)  # no such Feed
    assert not db.has_seen_items(feed.id)
    db.record_seen(feed.id, [("a", ItemStatus.SEEN)], now=1)  # the connection is still usable
    assert db.has_seen_items(feed.id)


# -- Webhooks --


def test_webhooks_get_set_delete(db: Database) -> None:
    assert db.get_webhook(CHANNEL) is None
    db.set_webhook(CHANNEL, 2**63 - 1, "secret")
    db.set_webhook(999, 2, "other")
    assert db.get_webhook(CHANNEL) == (2**63 - 1, "secret")

    db.set_webhook(CHANNEL, 3, "replaced")  # one per channel
    assert db.get_webhook(CHANNEL) == (3, "replaced")

    assert db.delete_webhook(CHANNEL) is True
    assert db.delete_webhook(CHANNEL) is False
    assert db.get_webhook(CHANNEL) is None
    assert db.get_webhook(999) == (2, "other")


# -- Log entries --


def log(db: Database, kind: LogKind = LogKind.FEED_EDITED, **overrides: object) -> LogEntry:
    args: dict[str, object] = {
        "server_id": SERVER,
        "at": 1000,
        "actor_id": 7,
        "actor_name": "alex",
        "kind": kind,
    }
    args.update(overrides)
    return db.add_log_entry(**args)  # type: ignore[arg-type]


def log_for(db: Database, feed: Feed, kind: LogKind, **overrides: object) -> LogEntry:
    return log(db, kind, server_id=feed.server_id, feed_id=feed.id, **overrides)


def test_add_log_entry_round_trips_every_field(db: Database) -> None:
    changes = (Change("Check interval", "10 minutes", "30 minutes"), Change("Name", "a", "b ✨"))
    entry = db.add_log_entry(
        server_id=SERVER,
        at=1234,
        actor_id=2**63 - 1,
        actor_name="alex",
        kind=LogKind.FEED_EDITED,
        feed_id=45,  # no such Feed: an entry does not need one
        feed_name="BBC News",
        channel_id=678,
        feed_url="https://example.com/rss",
        changes=iter(changes),
        detail="removed Button 2",
    )
    assert entry == LogEntry(
        id=entry.id,
        server_id=SERVER,
        at=1234,
        actor_id=2**63 - 1,
        actor_name="alex",
        kind=LogKind.FEED_EDITED,
        feed_id=45,
        feed_name="BBC News",
        channel_id=678,
        feed_url="https://example.com/rss",
        changes=changes,
        detail="removed Button 2",
    )
    assert db.list_log_entries(SERVER, limit=10) == [entry]
    assert isinstance(entry.changes, tuple) and entry.kind is LogKind.FEED_EDITED


def test_add_log_entry_defaults_and_the_bot_as_actor(db: Database) -> None:
    entry = db.add_log_entry(
        server_id=SERVER, at=5, actor_id=None, actor_name="", kind=LogKind.LOGS_CHANNEL_CHANGED
    )
    assert (entry.feed_id, entry.feed_name) == (None, "")
    assert (entry.channel_id, entry.feed_url) == (None, "")
    assert (entry.changes, entry.detail, entry.actor_id) == ((), "", None)
    assert db.list_log_entries(SERVER, limit=10) == [entry]


def test_add_log_entry_creates_the_server_and_keeps_its_settings(db: Database) -> None:
    assert db.get_server(SERVER) is None
    log(db)
    assert db.get_server(SERVER) == Server(SERVER, None, None)
    db.set_logs_channel(SERVER, 42)
    log(db)
    assert db.get_server(SERVER) == Server(SERVER, 42, None)


@pytest.mark.parametrize("kind", list(LogKind))
def test_every_log_kind_round_trips(db: Database, kind: LogKind) -> None:
    entry = log(db, kind)
    assert db.list_log_entries(SERVER, limit=1)[0].kind is kind is entry.kind


def test_add_log_entry_rejects_an_unknown_kind(db: Database) -> None:
    with pytest.raises(ValueError):
        log(db, "feed.exploded")  # type: ignore[arg-type]
    assert db.count_log_entries(SERVER) == 0


def test_add_log_entries_saves_many_in_order_and_ignores_given_ids(db: Database) -> None:
    drafts = [
        LogEntry(99, server, 10 + n, 7, "sam", LogKind.FEED_ADDED, feed_id=n, feed_name=f"F{n}")
        for n, server in enumerate([SERVER, SERVER, 999])
    ]
    saved = db.add_log_entries(iter(drafts))
    assert [e.feed_name for e in saved] == ["F0", "F1", "F2"]
    assert saved[0].id < saved[1].id < saved[2].id
    assert saved == [dataclasses.replace(d, id=s.id) for d, s in zip(drafts, saved, strict=True)]
    assert db.list_log_entries(SERVER, limit=10) == [saved[1], saved[0]]
    assert db.list_log_entries(999, limit=10) == [saved[2]]  # its Server was created too
    assert db.add_log_entries([]) == []


def test_add_log_entries_is_all_or_nothing(db: Database) -> None:
    good = LogEntry(0, SERVER, 1, 7, "sam", LogKind.FEED_ADDED)
    bad = dataclasses.replace(good, actor_name=None)  # type: ignore[arg-type]
    with pytest.raises(sqlite3.IntegrityError):
        db.add_log_entries([good, bad])
    assert db.count_log_entries(SERVER) == 0
    assert db.get_server(SERVER) is None
    assert db.add_log_entries([good])[0].id > 0  # the connection is still usable


def test_log_entries_outlive_their_feed_and_ids_are_not_reused(db: Database) -> None:
    feed = make_feed(db)
    added = log_for(db, feed, LogKind.FEED_ADDED, feed_name=feed.name)
    db.delete_feed(feed.id)
    removed = log_for(db, feed, LogKind.FEED_REMOVED, feed_name=feed.name)
    assert db.list_log_entries(SERVER, feed_id=feed.id, limit=10) == [removed, added]
    assert make_feed(db).id != feed.id  # so a new Feed never inherits these entries

    db.prune_log_entries(before=10_000)
    assert log(db).id > removed.id  # nor is an entry's id


def test_list_log_entries_newest_first_with_limit_and_offset(db: Database) -> None:
    # Saved out of time order: the order is the order of saving.
    entries = [log(db, at=at) for at in (50, 10, 30, 30, 20)]
    newest_first = entries[::-1]
    assert db.list_log_entries(SERVER, limit=10) == newest_first
    assert db.list_log_entries(SERVER, limit=2) == newest_first[:2]
    assert db.list_log_entries(SERVER, limit=2, offset=2) == newest_first[2:4]
    assert db.list_log_entries(SERVER, limit=2, offset=4) == newest_first[4:]
    assert db.list_log_entries(SERVER, limit=2, offset=5) == []
    assert db.list_log_entries(SERVER, limit=0) == []
    assert db.list_log_entries(12345, limit=10) == []
    assert db.count_log_entries(SERVER) == 5
    assert db.count_log_entries(12345) == 0


def test_list_and_count_log_entries_filters(db: Database) -> None:
    a = log(db, LogKind.FEED_ADDED, feed_id=1, actor_id=7)
    b = log(db, LogKind.FEED_PAUSED, feed_id=1, actor_id=8)
    c = log(db, LogKind.FEED_AUTO_PAUSED, feed_id=2, actor_id=None, actor_name="")
    d = log(db, LogKind.GRANT_GIVEN, actor_id=7)
    e = log(db, LogKind.FEED_BROKEN, feed_id=1, actor_id=None, actor_name="")
    log(db, LogKind.FEED_ADDED, server_id=999, feed_id=1, actor_id=7)  # another Server

    def both(**filters: object) -> list[LogEntry]:
        found = db.list_log_entries(SERVER, limit=100, **filters)  # type: ignore[arg-type]
        assert db.count_log_entries(SERVER, **filters) == len(found)  # type: ignore[arg-type]
        return found

    assert both() == [e, d, c, b, a]
    assert both(feed_id=1) == [e, b, a]
    assert both(actor_id=7) == [d, a]
    assert both(by_bot=True) == [e, c]
    assert both(by_bot=True, feed_id=1) == [e]
    assert both(kinds=[LogKind.FEED_PAUSED, LogKind.FEED_AUTO_PAUSED]) == [c, b]
    assert both(kinds={LogKind.GRANT_GIVEN}) == [d]
    assert both(kinds=[]) == []  # none of no kinds
    assert both(feed_id=1, actor_id=8, kinds=[LogKind.FEED_PAUSED]) == [b]
    assert both(feed_id=3) == [] and both(actor_id=12345) == []

    with pytest.raises(ValueError, match="not both"):
        db.list_log_entries(SERVER, actor_id=7, by_bot=True, limit=1)
    with pytest.raises(ValueError, match="not both"):
        db.count_log_entries(SERVER, actor_id=7, by_bot=True)


def test_purge_removed_servers_takes_their_log_entries(db: Database) -> None:
    gone = make_feed(db, server_id=1, channel_id=10)
    kept = make_feed(db, server_id=2, channel_id=20)
    log_for(db, gone, LogKind.FEED_ADDED)
    log(db, LogKind.GRANT_GIVEN, server_id=1)
    stays = log_for(db, kept, LogKind.FEED_ADDED)
    db.mark_server_removed(1, 99)

    assert db.purge_removed_servers(before=100) == [1]

    assert db.count_log_entries(1) == 0
    assert db._conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0] == 1
    assert db.list_log_entries(2, limit=10) == [stays]


def test_feed_attributions(db: Database) -> None:
    plain = make_feed(db, name="plain")  # added before Log entries existed
    running = make_feed(db, name="running")
    paused = make_feed(db, name="paused")
    by_bot = make_feed(db, name="by bot")
    resumed = make_feed(db, name="resumed")
    removed = make_feed(db, name="removed")
    elsewhere = make_feed(db, server_id=999)

    added = {f.id: log_for(db, f, LogKind.FEED_ADDED) for f in (running, paused, by_bot, resumed)}
    log_for(db, removed, LogKind.FEED_ADDED)
    other = log_for(db, elsewhere, LogKind.FEED_ADDED)
    # An entry of another Server that happens to name this Feed id is not this Feed's.
    log(db, LogKind.FEED_ADDED, server_id=999, feed_id=plain.id)

    log_for(db, paused, LogKind.FEED_PAUSED, actor_id=1)
    log_for(db, paused, LogKind.FEED_RESUMED)
    latest = log_for(db, paused, LogKind.FEED_PAUSED, actor_id=2)
    log_for(db, paused, LogKind.FEED_EDITED)
    db.update_feed(paused.id, paused=PauseReason.MANUAL)

    log_for(db, by_bot, LogKind.FEED_PAUSED, actor_id=1)
    auto = log_for(db, by_bot, LogKind.FEED_AUTO_PAUSED, actor_id=None, actor_name="")
    db.update_feed(by_bot.id, paused=PauseReason.LOST_CHANNEL)

    log_for(db, resumed, LogKind.FEED_PAUSED)  # not paused now: says nothing any more
    log_for(db, resumed, LogKind.FEED_RESUMED)
    db.delete_feed(removed.id)

    assert db.feed_attributions(SERVER) == {
        plain.id: FeedAttribution(None, None),
        running.id: FeedAttribution(added[running.id], None),
        paused.id: FeedAttribution(added[paused.id], latest),
        by_bot.id: FeedAttribution(added[by_bot.id], auto),
        resumed.id: FeedAttribution(added[resumed.id], None),
    }
    assert db.feed_attributions(999) == {elsewhere.id: FeedAttribution(other, None)}
    assert db.feed_attributions(12345) == {}

    assert db.feed_attribution(paused.id) == FeedAttribution(added[paused.id], latest)
    assert db.feed_attribution(plain.id) == FeedAttribution()
    assert db.feed_attribution(removed.id) == FeedAttribution()
    assert db.feed_attribution(12345) == FeedAttribution()


def test_a_paused_feed_without_a_pause_entry_has_no_pause_attribution(db: Database) -> None:
    feed = make_feed(db)
    db.update_feed(feed.id, paused=PauseReason.MANUAL)
    assert db.feed_attributions(SERVER) == {feed.id: FeedAttribution()}


def test_feed_attributions_takes_two_statements_however_many_feeds(db: Database) -> None:
    feeds = [make_feed(db, name=f"F{n}") for n in range(300)]
    for feed in feeds:
        log_for(db, feed, LogKind.FEED_ADDED)
        log_for(db, feed, LogKind.FEED_PAUSED)
        db.update_feed(feed.id, paused=PauseReason.MANUAL)
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    found = db.feed_attributions(SERVER)
    db._conn.set_trace_callback(None)
    assert len(statements) <= 3  # the Feed ids, the "added" entries, the pauses
    assert len(found) == 300
    assert all(a.added is not None and a.paused is not None for a in found.values())


def test_prune_log_entries_deletes_strictly_older_entries(db: Database) -> None:
    old = log(db, at=99)
    edge = log(db, at=100)
    new = log(db, at=101)
    assert db.prune_log_entries(before=100) == 1
    assert db.list_log_entries(SERVER, limit=10) == [new, edge]
    assert old not in db.list_log_entries(SERVER, limit=10)
    assert db.prune_log_entries(before=100) == 0
    assert db.prune_log_entries(before=102) == 2


def test_prune_keeps_the_added_entry_while_the_feed_exists(db: Database) -> None:
    feed = make_feed(db)
    added = log_for(db, feed, LogKind.FEED_ADDED, at=1)
    log_for(db, feed, LogKind.FEED_EDITED, at=1)
    log(db, LogKind.FEED_ADDED, at=1, feed_id=12345)  # of a Feed that is gone
    log(db, LogKind.FEED_ADDED, at=1, server_id=999, feed_id=feed.id)  # not this Feed's
    log(db, LogKind.GRANT_GIVEN, at=1)  # about no Feed at all

    assert db.prune_log_entries(before=1000) == 4
    assert db.list_log_entries(SERVER, limit=10) == [added]
    assert db.feed_attribution(feed.id) == FeedAttribution(added, None)

    db.delete_feed(feed.id)  # now an ordinary entry
    assert db.prune_log_entries(before=1000) == 1
    assert db.count_log_entries(SERVER) == 0


@pytest.mark.parametrize("kind", [LogKind.FEED_PAUSED, LogKind.FEED_AUTO_PAUSED])
def test_prune_keeps_the_latest_pause_while_the_feed_is_paused(db: Database, kind: LogKind) -> None:
    feed = make_feed(db)
    other_kind = LogKind.FEED_AUTO_PAUSED if kind is LogKind.FEED_PAUSED else LogKind.FEED_PAUSED
    log_for(db, feed, other_kind, at=1)  # an earlier pause
    log_for(db, feed, LogKind.FEED_RESUMED, at=2)
    latest = log_for(db, feed, kind, at=3)
    db.update_feed(feed.id, paused=PauseReason.MANUAL)

    assert db.prune_log_entries(before=1000) == 2
    assert db.list_log_entries(SERVER, limit=10) == [latest]
    assert db.feed_attribution(feed.id) == FeedAttribution(None, latest)

    db.update_feed(feed.id, paused=None)  # resumed: now an ordinary entry
    assert db.prune_log_entries(before=1000) == 1
    assert db.count_log_entries(SERVER) == 0


def test_prune_drops_a_pause_entry_once_the_paused_feed_is_removed(db: Database) -> None:
    feed = make_feed(db)
    log_for(db, feed, LogKind.FEED_ADDED, at=1)
    log_for(db, feed, LogKind.FEED_PAUSED, at=2)
    db.update_feed(feed.id, paused=PauseReason.MANUAL)
    assert db.prune_log_entries(before=1000) == 0
    db.delete_feed(feed.id)
    assert db.prune_log_entries(before=1000) == 2


def test_prune_log_entries_spans_servers_and_spares_new_entries(db: Database) -> None:
    feed = make_feed(db)
    log(db, at=1)
    log(db, at=1, server_id=999)
    recent = log_for(db, feed, LogKind.FEED_EDITED, at=5000)
    assert db.prune_log_entries(before=1000) == 2
    assert db.list_log_entries(SERVER, limit=10) == [recent]
    assert db.count_log_entries(999) == 0


def test_a_version_3_database_gains_log_entries(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "old.db"
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:3])
        old = Database(path)
        feed = make_feed(old)
        old.close()
    database = Database(path)
    try:
        assert database.schema_version == len(db_module.MIGRATIONS)
        assert database.get_feed(feed.id) == feed
        entry = log_for(database, feed, LogKind.FEED_ADDED)
        assert database.feed_attributions(SERVER) == {feed.id: FeedAttribution(entry, None)}
        fks = database._conn.execute("PRAGMA foreign_key_list(log_entries)").fetchall()
        assert [(fk["table"], fk["from"], fk["on_delete"]) for fk in fks] == [
            ("servers", "server_id", "CASCADE")
        ]
    finally:
        database.close()
