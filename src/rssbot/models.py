"""Shared data types. Every module codes against these, so change them with care.

Vocabulary follows CONTEXT.md. Times are Unix seconds. Discord IDs are ints.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

MIN_INTERVAL_S = 5 * 60
MAX_INTERVAL_S = 24 * 60 * 60
DEFAULT_INTERVAL_S = 10 * 60

CATCH_UP_LIMIT = 10
MAX_DELIVERY_ATTEMPTS = 3

DEFAULT_TEXT_TEMPLATE = "📰 | **{{title}}**\n{{link}}"
DEFAULT_FORUM_TITLE_TEMPLATE = "{{title||feed_title}}"

MAX_BUTTONS = 5
MAX_EMBED_FIELDS = 25
MAX_FORUM_TAGS = 5
MAX_COVER_IMAGE_BYTES = 8 * 1024 * 1024


class Level(enum.StrEnum):
    ADMIN = "admin"
    MANAGER = "manager"


class TargetKind(enum.StrEnum):
    ROLE = "role"
    MEMBER = "member"


class ChannelKind(enum.StrEnum):
    MESSAGES = "messages"  # text channel, announcement channel or thread
    FORUM = "forum"


class PostAs(enum.StrEnum):
    BOT = "bot"
    SITE = "site"
    CUSTOM = "custom"


class PauseReason(enum.StrEnum):
    MANUAL = "manual"
    LOST_CHANNEL = "lost_channel"
    NEEDS_TAG = "needs_tag"  # the forum requires a tag and the Feed has none


class FilterList(enum.StrEnum):
    MUST_HAVE = "must_have"
    BLOCK = "block"


class FilterField(enum.StrEnum):
    ANY = "any"  # title and description
    TITLE = "title"
    DESCRIPTION = "description"
    CATEGORY = "category"
    AUTHOR = "author"


class ItemStatus(enum.StrEnum):
    SEEN = "seen"  # recorded without posting: present when the Feed was added, or filtered out
    DELIVERED = "delivered"
    SENDING = "sending"  # being delivered; only an attempt that was cut off leaves this
    PENDING = "pending"  # delivery failed for now; retried on the next Check
    # Given up on: outside Catch-up, delivery kept failing, or its delivery was cut off.
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Item:
    key: str  # stable identity within its Feed
    title: str  # plain text
    link: str
    summary: str  # the short text, already in Discord formatting
    content: str  # the full text, already in Discord formatting
    author: str
    published: int | None
    categories: tuple[str, ...]
    image: str  # URL, or ""

    @property
    def description(self) -> str:
        return self.summary or self.content


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    title: str
    link: str  # the site's home page, or ""
    image: str  # the feed's own image, icon or logo URL, or ""
    items: tuple[Item, ...]  # in the order the source lists them


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True, slots=True)
class EmbedSpec:
    """An Embed. On a Feed its strings are templates; in an OutgoingMessage they are final text."""

    title: str = ""
    description: str = ""
    url: str = ""
    image: str = ""
    footer: str = ""
    colour: int | None = None
    fields: tuple[FieldSpec, ...] = ()
    timestamp: bool = True  # show the Item's date after the footer, in each viewer's own time


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    """A Button. On a Feed its strings are templates; in an OutgoingMessage they are final text."""

    label: str
    url: str


@dataclass(frozen=True, slots=True)
class Feed:
    id: int
    server_id: int
    channel_id: int
    channel_kind: ChannelKind
    name: str
    url: str
    interval_s: int

    # What the source says about itself: the feed_title and feed_link Placeholders.
    source_title: str
    source_link: str

    # Template
    text_template: str
    embed: EmbedSpec | None
    buttons: tuple[ButtonSpec, ...]
    mention_role_ids: tuple[int, ...]

    # Post as
    post_as: PostAs
    custom_name: str
    custom_avatar: str
    site_name: str  # discovered for PostAs.SITE; "" until discovered
    site_icon: str
    site_checked_at: int | None

    # Forum channels only
    forum_title_template: str
    forum_tag_ids: tuple[int, ...]
    forum_cover: bool

    # State
    paused: PauseReason | None
    etag: str | None
    last_modified: str | None
    next_check_at: int
    fail_count: int  # consecutive failed Checks
    failing_since: int | None
    warned: bool  # the Broken feed warning has been sent
    last_error: str
    last_success_at: int | None
    skipped_count: int  # Items whose delivery was given up on since the Feed last posted one
    skipped_since: int | None
    last_checked_at: int | None  # the last Check that reached a verdict, or fetch on add or edit
    rate_limited_since: int | None  # set while the source's latest answer is "slow down" (429)
    created_at: int


@dataclass(frozen=True, slots=True)
class Filter:
    id: int
    feed_id: int
    list: FilterList
    field: FilterField
    word: str


@dataclass(frozen=True, slots=True)
class Grant:
    server_id: int
    target_id: int
    target_kind: TargetKind
    level: Level


@dataclass(frozen=True, slots=True)
class Server:
    server_id: int
    logs_channel_id: int | None
    removed_at: int | None  # when the bot was removed; data is deleted 30 days later


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """One Item, fully rendered and cut to Discord's limits, ready to send."""

    content: str
    embed: EmbedSpec | None = None
    buttons: tuple[ButtonSpec, ...] = ()
    mention_role_ids: tuple[int, ...] = ()  # the only roles this message may ping
    username: str | None = None  # Post as; None posts as the bot
    avatar_url: str | None = None
    thread_title: str | None = None  # set when the Feed is bound to a forum channel
    tag_ids: tuple[int, ...] = ()
    cover_image_url: str | None = None  # to download and attach to a Forum post
    published: int | None = None  # the Item's date, for the Embed's timestamp


@dataclass(frozen=True, slots=True)
class Actor:
    """Who did something: a member, or the bot itself."""

    id: int | None  # None means the bot itself
    name: str  # the display name at the time
    avatar_url: str = ""

    @classmethod
    def bot(cls) -> Actor:
        """The bot itself. Its name and picture are filled in where they are shown."""
        return cls(id=None, name="")

    @property
    def is_bot(self) -> bool:
        return self.id is None


class LogKind(enum.StrEnum):
    """What a Log entry records. The values are stored and written to the container log."""

    FEED_ADDED = "feed.add"  # also each Feed of an OPML import
    FEED_REMOVED = "feed.remove"
    FEED_PAUSED = "feed.pause"  # by a member
    FEED_RESUMED = "feed.resume"
    FEED_EDITED = "feed.edit"
    TEMPLATE_CHANGED = "template.change"
    FILTER_CHANGED = "filter.change"
    POST_AS_CHANGED = "post_as.change"
    MENTIONS_CHANGED = "mentions.change"
    FORUM_TAGS_CHANGED = "forum_tags.change"
    GRANT_GIVEN = "grant.give"
    GRANT_TAKEN = "grant.take"
    LOGS_CHANNEL_CHANGED = "logs_channel.change"
    # The bot's own reports.
    FEED_AUTO_PAUSED = "feed.auto_pause"
    FEED_BROKEN = "feed.broken"
    FEED_WORKING_AGAIN = "feed.working"

    @property
    def announced(self) -> bool:
        """Whether Log entries of this kind are also posted in the Server's Logs channel."""
        return self not in _SAVED_ONLY


# The small changes: saved, never posted in the Logs channel.
_SAVED_ONLY = frozenset(
    {
        LogKind.TEMPLATE_CHANGED,
        LogKind.FILTER_CHANGED,
        LogKind.POST_AS_CHANGED,
        LogKind.MENTIONS_CHANGED,
        LogKind.FORUM_TAGS_CHANGED,
    }
)

# The kinds that say who paused a Paused feed.
PAUSE_KINDS = frozenset({LogKind.FEED_PAUSED, LogKind.FEED_AUTO_PAUSED})


@dataclass(frozen=True, slots=True)
class Change:
    """One value before and after, as shown: "Check interval", "10 minutes", "30 minutes"."""

    label: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class LogEntry:
    """A Log entry. It keeps the Feed's name, channel and address as they were at the time,
    and outlives the Feed."""

    id: int
    server_id: int
    at: int
    actor_id: int | None  # None means the bot itself
    actor_name: str  # "" for the bot
    kind: LogKind
    feed_id: int | None = None
    feed_name: str = ""  # as it was then; "" when no Feed
    channel_id: int | None = None
    feed_url: str = ""  # "" when none
    changes: tuple[Change, ...] = ()
    detail: str = ""  # one line: what was removed, a pause cause, the Grant's target; or ""


@dataclass(frozen=True, slots=True)
class FeedAttribution:
    """The Log entries that say who added a Feed and, while it is paused, who paused it."""

    added: LogEntry | None = None
    paused: LogEntry | None = None  # the latest pause, by a member or the bot; None unless paused
