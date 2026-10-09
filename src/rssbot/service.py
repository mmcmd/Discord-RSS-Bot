"""The application logic behind the slash commands: everything that is not Discord UI.

Every operation that takes a feed id also takes the server id, and refuses a Feed of
another Server as if it did not exist. Every expected failure is a ServiceError whose
message can be shown to the member as it is.

Methods that change something or fetch are `async`; plain lookups are not.
Positions (of Fields and Buttons) count from 1, as a member would.

Every method that changes something for a member takes that member as `actor`, and saves
a Log entry once the change is made. A change that changes nothing saves none.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import random
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from . import template as tpl
from .db import Database, FeedNotFound
from .filters import normalise_word
from .identity import SiteIdentity, discover
from .journal import Journal
from .models import (
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
    Filter,
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
from .opml import OpmlEntry, OpmlError, build_opml, parse_opml
from .parse import ParseError, parse_feed
from .ports import Clock, Fetcher, FetchError, FetchResult, ParseFn, RenderFn
from .render import (
    MAX_BUTTON_LABEL,
    MAX_BUTTON_URL,
    MAX_COLOUR,
    MAX_CONTENT,
    MAX_EMBED_DESCRIPTION,
    MAX_EMBED_FOOTER,
    MAX_EMBED_TITLE,
    MAX_EMBED_URL,
    MAX_FIELD_NAME,
    MAX_FIELD_VALUE,
    MAX_USERNAME,
    render_item,
)
from .scheduler import STARTED_KEY

log = logging.getLogger(__name__)

MAX_NAME_CHARS = 100
MAX_URL_CHARS = 2000
MAX_FORUM_TITLE_TEMPLATE_CHARS = 200
MAX_MENTION_ROLES = 10
MAX_FILTERS = 100
MAX_FILTER_WORD_CHARS = 100
MAX_IMPORT_FEEDS = 100
IMPORT_CONCURRENCY = 5
SITE_REFRESH_S = 7 * 24 * 60 * 60
MAX_PLACEHOLDER_VALUE_CHARS = 200

NO_SUCH_FEED = "That Feed no longer exists."

_PAUSE_WORDS = {
    PauseReason.MANUAL: "by a member",
    PauseReason.LOST_CHANNEL: "the bot can no longer post in its channel",
    PauseReason.NEEDS_TAG: "the forum requires a tag and the Feed has none",
}

_LIST_WORDS = {FilterList.MUST_HAVE: "must-have", FilterList.BLOCK: "block"}

POST_AS_WORDS = {
    PostAs.BOT: "The bot",
    PostAs.SITE: "The site's name and icon",
    PostAs.CUSTOM: "A custom name and picture",
}


MAX_FEEDS_PER_SERVER = 1000  # a safety limit against runaway imports, not a product limit


class ServiceError(Exception):
    """An expected failure. The message is one plain sentence to show to the Discord member."""


class DuplicateFeed(ServiceError):
    """The channel already has a Feed for that address."""


@dataclass(frozen=True, slots=True)
class RemovedFeed:
    feed: Feed  # as it was
    channel_id: int
    webhook_in_use: bool  # another Feed in that channel still posts through the webhook


@dataclass(frozen=True, slots=True)
class ImportFailure:
    title: str
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class OpmlImport:
    added: tuple[str, ...]  # the new Feeds' names, in the file's order
    skipped: int  # the channel already had a Feed for these
    failed: tuple[ImportFailure, ...]
    left_out: int  # new addresses beyond MAX_IMPORT_FEEDS, not tried


class FeedStatus(enum.StrEnum):
    PAUSED = "paused"
    RATE_LIMITED = "rate limited"
    FAILING = "failing"
    WORKING = "working"


def status_of(feed: Feed) -> FeedStatus:
    """The Feed's status. When several apply, the first of the four above wins."""
    if feed.paused is not None:
        return FeedStatus.PAUSED
    if feed.rate_limited_since is not None:
        return FeedStatus.RATE_LIMITED  # the source's latest answer, whatever failed before
    if feed.fail_count > 0:
        return FeedStatus.FAILING
    return FeedStatus.WORKING


def pause_cause(reason: PauseReason) -> str:
    """Why a Feed is paused, in words that follow "Paused: "."""
    return _PAUSE_WORDS.get(reason, reason.value)


def status_line(feed: Feed) -> str:
    """One line: paused (and why), rate limited, failing (and why), or working."""
    status = status_of(feed)
    if feed.paused is not None:
        return f"Paused: {pause_cause(feed.paused)}"
    if status is FeedStatus.RATE_LIMITED:
        return "Rate limited"
    if status is FeedStatus.FAILING:
        return f"Failing: {feed.last_error.strip() or 'unknown error'}"
    if feed.skipped_count > 0:
        # The Checks themselves succeed, so this is not Failing.
        since = "" if feed.skipped_since is None else f" since <t:{feed.skipped_since}:R>"
        return f"Working, {skipped_words(feed.skipped_count)}{since}"
    return "Working"


def skipped_words(count: int) -> str:
    return "1 Item could not be posted" if count == 1 else f"{count} Items could not be posted"


def duration_words(seconds: int) -> str:
    """ "10 minutes", "1 hour", "6 hours": how a Check interval is put into words."""
    for size, unit in ((3600, "hour"), (60, "minute")):
        if seconds >= size and seconds % size == 0:
            return _count(seconds // size, unit)
    return _count(seconds, "second")


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


class FeedService:
    def __init__(
        self,
        db: Database,
        fetcher: Fetcher,
        clock: Clock,
        journal: Journal,
        parse: ParseFn = parse_feed,  # synchronous and possibly slow: run in a thread
        render: RenderFn = render_item,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._db = db
        self._fetcher = fetcher
        self._clock = clock
        self._journal = journal
        self._parse = parse
        self._render = render
        self._rand = rand

    def now(self) -> int:
        return self._clock.now()

    # -- lookups --

    def get_feed(self, server_id: int, feed_id: int) -> Feed:
        """The Feed, which must belong to the Server. Every operation goes through this."""
        feed = self._db.get_feed(feed_id)
        if feed is None or feed.server_id != server_id:
            raise ServiceError(NO_SUCH_FEED)
        return feed

    def list_feeds(self, server_id: int) -> list[Feed]:
        return self._db.list_feeds(server_id)

    def list_channel_feeds(self, server_id: int, channel_id: int) -> list[Feed]:
        feeds = self._db.list_channel_feeds(channel_id)
        return [feed for feed in feeds if feed.server_id == server_id]

    def search_feeds(self, server_id: int, text: str, limit: int = 25) -> list[Feed]:
        """The Server's Feeds whose name or address contains the text, for autocomplete."""
        wanted = text.strip().casefold()
        feeds = self._db.list_feeds(server_id)
        if wanted:
            feeds = [f for f in feeds if wanted in f.name.casefold() or wanted in f.url.casefold()]
        return feeds[:limit]

    def channel_uses_webhook(self, channel_id: int) -> bool:
        """Whether any Feed in the channel posts through a webhook (Post as is not Bot)."""
        return any(f.post_as is not PostAs.BOT for f in self._db.list_channel_feeds(channel_id))

    status_line = staticmethod(status_line)

    # -- add, edit, remove --

    async def add_feed(
        self,
        server_id: int,
        channel_id: int,
        channel_kind: ChannelKind,
        url: str,
        *,
        actor: Actor,
        name: str | None = None,
        interval_s: int | None = None,
        post_as: PostAs = PostAs.BOT,
        custom_name: str = "",  # for PostAs.CUSTOM only
        custom_avatar: str = "",
    ) -> tuple[Feed, int]:
        """Add a Feed. Returns it and how many Items its source lists now (none is posted)."""
        feed, listed = await self._add(
            server_id,
            channel_id,
            channel_kind,
            url,
            name=name,
            interval_s=interval_s,
            post_as=post_as,
            custom_name=custom_name,
            custom_avatar=custom_avatar,
        )
        self._log(actor, LogKind.FEED_ADDED, feed)
        return feed, listed

    async def _add(
        self,
        server_id: int,
        channel_id: int,
        channel_kind: ChannelKind,
        url: str,
        *,
        name: str | None = None,
        interval_s: int | None = None,
        post_as: PostAs = PostAs.BOT,
        custom_name: str = "",
        custom_avatar: str = "",
    ) -> tuple[Feed, int]:
        """add_feed without its Log entry: an OPML import saves those of its Feeds together."""
        url = _clean_url(url)
        interval = DEFAULT_INTERVAL_S if interval_s is None else _check_interval(interval_s)
        post_as = PostAs(post_as)
        if post_as is PostAs.CUSTOM:
            custom_name, custom_avatar = _check_custom(custom_name, custom_avatar)
        else:
            custom_name = custom_avatar = ""
        self._refuse_duplicate(channel_id, url)
        if self._db.count_feeds(server_id) >= MAX_FEEDS_PER_SERVER:
            raise ServiceError(
                f"This Server already has {MAX_FEEDS_PER_SERVER} Feeds, the most allowed."
            )

        parsed, result = await self._read(url)
        identity = None
        if post_as is PostAs.SITE:
            identity = await discover(parsed, url, self._fetcher)

        # Nothing is awaited from here on, so the Feed and its Seen items appear together.
        self._refuse_duplicate(channel_id, url)
        now = self._clock.now()
        feed = self._db.create_feed(
            server_id=server_id,
            channel_id=channel_id,
            channel_kind=ChannelKind(channel_kind),
            name=_name(name, parsed, url),
            url=url,
            now=now,
            next_check_at=self._first_check(now, interval),
            interval_s=interval,
            source_title=parsed.title,
            source_link=parsed.link,
            post_as=post_as,
            custom_name=custom_name,
            custom_avatar=custom_avatar,
        )
        try:
            changes: dict[str, Any] = {
                "etag": result.etag,
                "last_modified": result.last_modified,
                "last_success_at": now,
                "last_checked_at": now,
            }
            if identity is not None:
                changes.update(_identity_changes(identity, now))
            feed = self._db.update_feed(feed.id, **changes)
            self._baseline(feed.id, parsed, now)
        except Exception:
            self._db.delete_feed(feed.id)  # a Feed without its starting point would post it all
            raise
        return feed, len(parsed.items)

    async def edit_feed(
        self,
        server_id: int,
        feed_id: int,
        *,
        actor: Actor,
        name: str | None = None,
        url: str | None = None,
        channel_id: int | None = None,
        channel_kind: ChannelKind | None = None,  # required with channel_id
        interval_s: int | None = None,
    ) -> tuple[Feed, int | None]:
        """Change any of the settings; None leaves one as it is.

        Returns the Feed and the id of its old channel if the channel changed.
        """
        if channel_id is not None and channel_kind is None:
            raise ValueError("channel_kind is required with channel_id")
        new_name = None if name is None else _clean_name(name)
        new_interval = None if interval_s is None else _check_interval(interval_s)
        new_url = None if url is None else _clean_url(url)

        feed = self.get_feed(server_id, feed_id)
        if new_url == feed.url:
            new_url = None
        target_channel = feed.channel_id if channel_id is None else channel_id
        if new_url is not None or target_channel != feed.channel_id:
            self._refuse_duplicate(target_channel, new_url or feed.url, but=feed.id)

        parsed: ParsedFeed | None = None
        result: FetchResult | None = None
        identity: SiteIdentity | None = None
        if new_url is not None:
            parsed, result = await self._read(new_url)
            if feed.post_as is PostAs.SITE:
                identity = await discover(parsed, new_url, self._fetcher)
            feed = self.get_feed(server_id, feed_id)  # it may have changed meanwhile
            self._refuse_duplicate(target_channel, new_url, but=feed.id)

        now = self._clock.now()
        interval = feed.interval_s if new_interval is None else new_interval
        changes: dict[str, Any] = {}
        if new_name is not None:
            changes["name"] = new_name
        if new_interval is not None and new_interval != feed.interval_s:
            changes["interval_s"] = new_interval
            changes["next_check_at"] = min(feed.next_check_at, now + new_interval)
        if parsed is not None and result is not None:
            changes.update(
                url=new_url,
                etag=result.etag,
                last_modified=result.last_modified,
                source_title=parsed.title,
                source_link=parsed.link,
                next_check_at=now + interval,
                fail_count=0,
                failing_since=None,
                warned=False,
                last_error="",
                last_success_at=now,
                last_checked_at=now,
                rate_limited_since=None,
                skipped_count=0,
                skipped_since=None,
            )
            if identity is not None:
                changes.update(_identity_changes(identity, now))
        moved = target_channel != feed.channel_id
        if moved:
            changes.update(channel_id=target_channel, forum_tag_ids=())
            if feed.paused in (PauseReason.LOST_CHANNEL, PauseReason.NEEDS_TAG):
                changes["paused"] = None
                changes.setdefault("next_check_at", now)
        if channel_kind is not None and ChannelKind(channel_kind) != feed.channel_kind:
            changes["channel_kind"] = ChannelKind(channel_kind)

        updated = self._update(feed.id, **changes)
        if parsed is not None:
            self._baseline(feed.id, parsed, now)
        edits = _edits(feed, updated)
        if edits:
            # Moved out of the channel the bot had paused it over, it is checked again.
            resumed = feed.paused is not None and updated.paused is None
            self._log(actor, LogKind.FEED_EDITED, updated, changes=edits, resumed=resumed)
        return updated, feed.channel_id if moved else None

    async def remove_feed(self, server_id: int, feed_id: int, *, actor: Actor) -> RemovedFeed:
        feed = self.get_feed(server_id, feed_id)
        self._db.delete_feed(feed.id)
        self._log(actor, LogKind.FEED_REMOVED, feed)
        return RemovedFeed(feed, feed.channel_id, self.channel_uses_webhook(feed.channel_id))

    async def pause_feed(self, server_id: int, feed_id: int, *, actor: Actor) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        if feed.paused is PauseReason.MANUAL:
            return feed
        updated = self._update(feed.id, paused=PauseReason.MANUAL)
        self._log(actor, LogKind.FEED_PAUSED, updated)
        return updated

    async def resume_feed(self, server_id: int, feed_id: int, *, actor: Actor) -> Feed:
        """Clear any pause reason and make the Feed due now.

        A Rate-limited feed keeps the time it is booked for: its source asked for the wait.
        """
        feed = self.get_feed(server_id, feed_id)
        now = self._clock.now()
        due = max(feed.next_check_at, now) if feed.rate_limited_since is not None else now
        updated = self._update(feed.id, paused=None, next_check_at=due)
        if feed.paused is not None:  # whoever paused it, a member or the bot
            self._log(actor, LogKind.FEED_RESUMED, updated)
        return updated

    def make_due(self, server_id: int) -> tuple[int, int]:
        """Make the Server's Feeds due now, except Paused feeds and Rate-limited feeds.

        Returns how many were made due and how many Rate-limited feeds were left to wait.
        """
        now = self._clock.now()
        feeds = [feed for feed in self._db.list_feeds(server_id) if feed.paused is None]
        waiting = [feed for feed in feeds if feed.rate_limited_since is not None]
        due = [feed for feed in feeds if feed.rate_limited_since is None]
        for feed in due:
            with contextlib.suppress(FeedNotFound):  # removed meanwhile
                self._db.update_feed(feed.id, next_check_at=min(feed.next_check_at, now))
        return len(due), len(waiting)

    # -- preview --

    async def preview(self, server_id: int, feed_id: int) -> tuple[OutgoingMessage, Item]:
        """The source's newest Item as the Feed would post it now. Changes nothing."""
        feed = self.get_feed(server_id, feed_id)
        item = await self._newest(feed)
        try:
            return self._render(feed, item), item
        except Exception as exc:
            log.warning("Feed %s: the preview could not be rendered", feed.id, exc_info=True)
            raise ServiceError("The newest Item could not be made into a message.") from exc

    async def placeholder_values(self, server_id: int, feed_id: int) -> list[tuple[str, str]]:
        """Every Placeholder with its value for the newest Item, cut short.

        The values are empty if the source cannot be read or lists nothing.
        """
        feed = self.get_feed(server_id, feed_id)
        try:
            values = tpl.values_for(feed, await self._newest(feed))
        except ServiceError:
            values = {}
        return [(name, _cut(values.get(name, ""))) for name in tpl.PLACEHOLDERS]

    # -- Template --

    async def set_text(self, server_id: int, feed_id: int, text: str, *, actor: Actor) -> Feed:
        """Set the message text. Empty means none: the Feed then posts only its Embed."""
        feed = self.get_feed(server_id, feed_id)
        text = _template(text.strip() and text, "The message text", MAX_CONTENT)
        return self._change_template(actor, feed, "changed the message text", text_template=text)

    async def set_embed(
        self,
        server_id: int,
        feed_id: int,
        *,
        actor: Actor,
        title: str | None = None,
        description: str | None = None,
        url: str | None = None,
        image: str | None = None,
        footer: str | None = None,
        colour: int | str | None = None,
        timestamp: bool | None = None,
    ) -> Feed:
        """Set parts of the Embed, creating it if needed.

        None leaves a part as it is and "" clears it. The colour is an int or a hex string
        such as "#ff8800".
        """
        feed = self.get_feed(server_id, feed_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = _template(title, "The Embed title", MAX_EMBED_TITLE)
        if description is not None:
            changes["description"] = _template(
                description, "The Embed description", MAX_EMBED_DESCRIPTION
            )
        if url is not None:
            changes["url"] = _url_template(url, "The Embed link", MAX_EMBED_URL)
        if image is not None:
            changes["image"] = _url_template(image, "The Embed image", MAX_EMBED_URL)
        if footer is not None:
            changes["footer"] = _template(footer, "The Embed footer", MAX_EMBED_FOOTER)
        if colour is not None:
            changes["colour"] = _colour(colour)
        if timestamp is not None:
            changes["timestamp"] = timestamp
        what = "added the Embed" if feed.embed is None else "changed the Embed"
        embed = replace(feed.embed or EmbedSpec(), **changes)
        return self._change_template(actor, feed, what, embed=embed)

    async def remove_embed(self, server_id: int, feed_id: int, *, actor: Actor) -> Feed:
        """Remove the Embed with its Fields."""
        feed = self.get_feed(server_id, feed_id)
        return self._change_template(actor, feed, "removed the Embed", embed=None)

    async def add_field(
        self,
        server_id: int,
        feed_id: int,
        name: str,
        value: str,
        inline: bool = False,
        *,
        actor: Actor,
    ) -> Feed:
        """Add a Field to the Embed, creating an empty Embed if the Feed has none."""
        feed = self.get_feed(server_id, feed_id)
        embed = feed.embed or EmbedSpec()
        if len(embed.fields) >= MAX_EMBED_FIELDS:
            raise ServiceError(f"An Embed can have at most {MAX_EMBED_FIELDS} Fields.")
        field = FieldSpec(
            name=_template(name, "The Field name", MAX_FIELD_NAME, required=True),
            value=_template(value, "The Field value", MAX_FIELD_VALUE, required=True),
            inline=bool(inline),
        )
        fields = (*embed.fields, field)
        return self._change_template(
            actor, feed, f"added Field {len(fields)}", embed=replace(embed, fields=fields)
        )

    async def remove_field(
        self, server_id: int, feed_id: int, position: int, *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        fields = () if feed.embed is None else feed.embed.fields
        if feed.embed is None or not 1 <= position <= len(fields):
            raise ServiceError(f"There is no Field number {position}.")
        kept = fields[: position - 1] + fields[position:]
        return self._change_template(
            actor, feed, f"removed Field {position}", embed=replace(feed.embed, fields=kept)
        )

    async def add_button(
        self, server_id: int, feed_id: int, label: str, url: str, *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        if len(feed.buttons) >= MAX_BUTTONS:
            raise ServiceError(f"A Feed can have at most {MAX_BUTTONS} Buttons.")
        button = ButtonSpec(
            label=_template(label, "The Button label", MAX_BUTTON_LABEL, required=True),
            url=_url_template(url, "The Button link", MAX_BUTTON_URL, required=True),
        )
        buttons = (*feed.buttons, button)
        return self._change_template(actor, feed, f"added Button {len(buttons)}", buttons=buttons)

    async def remove_button(
        self, server_id: int, feed_id: int, position: int, *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        if not 1 <= position <= len(feed.buttons):
            raise ServiceError(f"There is no Button number {position}.")
        kept = feed.buttons[: position - 1] + feed.buttons[position:]
        return self._change_template(actor, feed, f"removed Button {position}", buttons=kept)

    async def reset_template(self, server_id: int, feed_id: int, *, actor: Actor) -> Feed:
        """Back to the default text, no Embed and no Buttons."""
        feed = self.get_feed(server_id, feed_id)
        return self._change_template(
            actor,
            feed,
            "reset the Template",
            text_template=DEFAULT_TEXT_TEMPLATE,
            embed=None,
            buttons=(),
        )

    async def set_forum_title(
        self, server_id: int, feed_id: int, text: str, *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        text = _template(
            text.strip(), "The Forum post title", MAX_FORUM_TITLE_TEMPLATE_CHARS, required=True
        )
        return self._change_template(
            actor, feed, "changed the Forum post title", forum_title_template=text
        )

    async def set_mentions(
        self, server_id: int, feed_id: int, role_ids: Iterable[int], *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        # The role whose id is the Server's own is @everyone.
        ids = tuple(dict.fromkeys(int(r) for r in role_ids if int(r) != server_id))
        if len(ids) > MAX_MENTION_ROLES:
            raise ServiceError(f"A Feed can mention at most {MAX_MENTION_ROLES} roles.")
        updated = self._update(feed.id, mention_role_ids=ids)
        if ids != feed.mention_role_ids:
            detail = f"now mentions {_count(len(ids), 'role') if ids else 'no roles'}"
            self._log(actor, LogKind.MENTIONS_CHANGED, updated, detail=detail)
        return updated

    # -- Post as --

    async def set_post_as(
        self,
        server_id: int,
        feed_id: int,
        post_as: PostAs,
        *,
        actor: Actor,
        custom_name: str = "",  # for PostAs.CUSTOM only
        custom_avatar: str = "",
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        post_as = PostAs(post_as)
        if post_as is PostAs.BOT:
            updated = self._update(feed.id, post_as=PostAs.BOT)
        elif post_as is PostAs.CUSTOM:
            custom_name, custom_avatar = _check_custom(custom_name, custom_avatar)
            updated = self._update(
                feed.id, post_as=PostAs.CUSTOM, custom_name=custom_name, custom_avatar=custom_avatar
            )
        else:
            identity = await self._discover(feed)
            changes = _identity_changes(identity, self._clock.now())
            updated = self._update(feed.id, post_as=PostAs.SITE, **changes)
        before, after = _post_as_shown(feed), _post_as_shown(updated)
        if before != after:
            change = Change("Post as", before, after)
            self._log(actor, LogKind.POST_AS_CHANGED, updated, changes=[change])
        elif _picture(feed) != _picture(updated):
            self._log(actor, LogKind.POST_AS_CHANGED, updated, detail="changed the picture")
        return updated

    async def refresh_site_identity(self, server_id: int, feed_id: int) -> bool:
        """Look the site's name and picture up again if that was last done over 7 days ago.

        For the background loop. Returns whether it looked. Does nothing for a Feed whose
        Post as is not Site. No member asked for this, so it saves no Log entry.
        """
        feed = self.get_feed(server_id, feed_id)
        now = self._clock.now()
        if feed.post_as is not PostAs.SITE:
            return False
        if feed.site_checked_at is not None and now - feed.site_checked_at < SITE_REFRESH_S:
            return False
        identity = await self._discover(feed)
        current = self._db.get_feed(feed.id)
        if current is None or current.post_as is not PostAs.SITE or current.url != feed.url:
            return False
        # A lookup that found less than last time does not take away what is there.
        kept = SiteIdentity(identity.name or current.site_name, identity.icon or current.site_icon)
        self._update(feed.id, **_identity_changes(kept, self._clock.now()))
        return True

    # -- forum options --

    async def set_forum_tags(
        self, server_id: int, feed_id: int, tag_ids: Iterable[int], *, actor: Actor
    ) -> Feed:
        """Set the tags put on the Feed's Forum posts. Resumes a Feed paused for want of one."""
        feed = self.get_feed(server_id, feed_id)
        ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        if len(ids) > MAX_FORUM_TAGS:
            raise ServiceError(f"A Forum post can have at most {MAX_FORUM_TAGS} tags.")
        changes: dict[str, Any] = {"forum_tag_ids": ids}
        if ids and feed.paused is PauseReason.NEEDS_TAG:
            changes.update(paused=None, next_check_at=self._clock.now())
        updated = self._update(feed.id, **changes)
        resumed = "paused" in changes
        if ids != feed.forum_tag_ids:
            tags = _count(len(ids), "tag") if ids else "no tags"
            detail = f"now puts {tags} on its Forum posts"
            self._log(actor, LogKind.FORUM_TAGS_CHANGED, updated, detail=detail, resumed=resumed)
        elif resumed:
            self._log(actor, LogKind.FEED_RESUMED, updated)
        return updated

    async def set_forum_cover(
        self, server_id: int, feed_id: int, on: bool, *, actor: Actor
    ) -> Feed:
        feed = self.get_feed(server_id, feed_id)
        what = "turned the Cover image on" if on else "turned the Cover image off"
        return self._change_template(actor, feed, what, forum_cover=bool(on))

    # -- Filters --

    def list_filters(self, server_id: int, feed_id: int) -> list[Filter]:
        return self._db.list_filters(self.get_feed(server_id, feed_id).id)

    async def add_filters(
        self,
        server_id: int,
        feed_id: int,
        list_: FilterList,
        field: FilterField,
        words: Iterable[str],
        *,
        actor: Actor,
    ) -> list[Filter]:
        """Add words to one list. Returns the Filters added: blanks and repeats are left out.

        Adds nothing if any word is too long, is already in the other list for the same
        field, or the Feed would have too many Filters.
        """
        feed = self.get_feed(server_id, feed_id)
        list_, field = FilterList(list_), FilterField(field)
        existing = self._db.list_filters(feed.id)
        taken = {f.word.casefold() for f in existing if f.list == list_ and f.field == field}
        # In both lists a word would block every Item it lets through.
        other = {f.word.casefold(): f for f in existing if f.list != list_ and f.field == field}
        fresh: list[str] = []
        for raw in words:
            word = normalise_word(raw)
            if not word or word.casefold() in taken:
                continue
            clash = other.get(word.casefold())
            if clash is not None:
                raise ServiceError(
                    f"“{clash.word}” is already a {_LIST_WORDS[clash.list]} word "
                    "for this Feed. Remove it there first."
                )
            if len(word) > MAX_FILTER_WORD_CHARS:
                raise ServiceError(
                    f"A Filter word can be at most {MAX_FILTER_WORD_CHARS} characters long."
                )
            taken.add(word.casefold())
            fresh.append(word)
        if len(existing) + len(fresh) > MAX_FILTERS:
            raise ServiceError(f"A Feed can have at most {MAX_FILTERS} Filters.")
        added = [self._db.add_filter(feed.id, list_, field, word) for word in fresh]
        if added:
            self._log(actor, LogKind.FILTER_CHANGED, feed, detail=_filters_detail("added", added))
        return added

    async def remove_filter(
        self, server_id: int, feed_id: int, filter_id: int, *, actor: Actor
    ) -> Filter:
        """Remove one of the Feed's own Filters and return it."""
        feed = self.get_feed(server_id, feed_id)
        for flt in self._db.list_filters(feed.id):
            if flt.id == filter_id:
                self._db.remove_filter(flt.id)
                detail = _filters_detail("removed", [flt])
                self._log(actor, LogKind.FILTER_CHANGED, feed, detail=detail)
                return flt
        raise ServiceError("That Filter no longer exists.")

    # -- OPML --

    async def import_opml(
        self,
        server_id: int,
        channel_id: int,
        channel_kind: ChannelKind,
        data: bytes,
        *,
        actor: Actor,
    ) -> OpmlImport:
        """Add a Feed in the channel for every address in the file that it does not have yet.

        Each Feed added gets its own Log entry; together they are one report.
        """
        try:
            entries = parse_opml(data)
        except OpmlError as exc:
            raise ServiceError(str(exc)) from exc

        present = {feed.url for feed in self._db.list_channel_feeds(channel_id)}
        fresh = [entry for entry in entries if entry.url not in present]
        todo = fresh[:MAX_IMPORT_FEEDS]
        slots = asyncio.Semaphore(IMPORT_CONCURRENCY)

        async def add(entry: OpmlEntry) -> Feed | ImportFailure | None:
            """The new Feed, why it failed, or None if the channel has it after all."""
            name = None if entry.title == entry.url else entry.title
            async with slots:
                try:
                    feed, _ = await self._add(
                        server_id, channel_id, channel_kind, entry.url, name=name
                    )
                except DuplicateFeed:
                    return None
                except ServiceError as exc:
                    return ImportFailure(entry.title, entry.url, str(exc))
                except Exception:
                    # One address that breaks something never stops the others.
                    site = _host(entry.url) or "that address"
                    log.exception("OPML import: could not add %s", site)
                    reason = "Something went wrong. It has been logged."
                    return ImportFailure(entry.title, entry.url, reason)
            return feed

        results = await asyncio.gather(*(add(entry) for entry in todo))
        added = [r for r in results if isinstance(r, Feed)]
        if added:
            self._logged(actor, lambda: self._journal.record_many(actor, LogKind.FEED_ADDED, added))
        return OpmlImport(
            added=tuple(feed.name for feed in added),
            skipped=len(entries) - len(fresh) + sum(r is None for r in results),
            failed=tuple(r for r in results if isinstance(r, ImportFailure)),
            left_out=len(fresh) - len(todo),
        )

    def export_opml(self, server_id: int, title: str = "Discord RSS Bot feeds") -> bytes:
        """All the Server's Feeds as an OPML file."""
        feeds = self._db.list_feeds(server_id)
        if not feeds:
            raise ServiceError("This Server has no Feeds yet. Add one with `/feed add`.")
        return build_opml([OpmlEntry(feed.name, feed.url) for feed in feeds], title)

    # -- plumbing --

    def _change_template(self, actor: Actor, feed: Feed, what: str, **changes: Any) -> Feed:
        """Store a change to the Template. `what` names the part, never its text."""
        updated = self._update(feed.id, **changes)
        if any(getattr(feed, name) != value for name, value in changes.items()):
            self._log(actor, LogKind.TEMPLATE_CHANGED, updated, detail=what)
        return updated

    def _log(
        self,
        actor: Actor,
        kind: LogKind,
        feed: Feed,
        *,
        changes: Iterable[Change] = (),
        detail: str = "",
        resumed: bool = False,
    ) -> None:
        """Save the Log entry of a change that has been made and start its report.

        `resumed` adds the entry of a Paused feed that the change set going again.
        """

        def save() -> list[LogEntry]:
            record = self._journal.record_feed
            entries = [record(actor, kind, feed, changes=changes, detail=detail)]
            if resumed:
                entries.append(record(actor, LogKind.FEED_RESUMED, feed))
            return entries

        self._logged(actor, save)

    def _logged(self, actor: Actor, save: Callable[[], Sequence[LogEntry]]) -> None:
        """Save Log entries and start their report in the Logs channel.

        The change they record has already been made: if they cannot be saved, that is
        logged and the member is not shown an error for something that was done.
        """
        try:
            entries = save()
        except sqlite3.Error:
            log.exception("A change was made, but its Log entry could not be saved")
            return
        self._journal.announce_soon(entries, actor)

    def _update(self, feed_id: int, **changes: Any) -> Feed:
        try:
            return self._db.update_feed(feed_id, **changes)
        except FeedNotFound:
            raise ServiceError(NO_SUCH_FEED) from None

    def _first_check(self, now: int, interval: int) -> int:
        """Between half an interval and a whole one from now.

        Feeds added together, as by an import, would otherwise all be due in the same
        minute for good: the Scheduler's jitter is too small to pull them apart.
        """
        try:
            sooner = int(interval / 2 * min(max(float(self._rand()), 0.0), 1.0))
        except Exception:  # a random source that raises, or hands back something like NaN
            sooner = 0
        return now + interval - sooner

    def _refuse_duplicate(self, channel_id: int, url: str, but: int | None = None) -> None:
        for feed in self._db.list_channel_feeds(channel_id):
            if feed.url == url and feed.id != but:
                raise DuplicateFeed("That channel already has a Feed for that address.")

    def _baseline(self, feed_id: int, parsed: ParsedFeed, now: int) -> None:
        """Record everything the source lists as Seen, so that none of it is posted.

        With the Scheduler's marker, in one write: this stands in for the Feed's first Check.
        """
        keys = dict.fromkeys((STARTED_KEY, *(item.key for item in parsed.items)))
        self._db.record_seen(feed_id, [(key, ItemStatus.SEEN) for key in keys], now)

    async def _read(self, url: str) -> tuple[ParsedFeed, FetchResult]:
        """Fetch and parse a source in full."""
        try:
            result = await self._fetcher.fetch(url)
        except FetchError as exc:
            # The host only: an address can carry a private key in its path or query.
            log.info("Could not fetch from %s: %s", _host(url) or "that address", exc)
            raise ServiceError(str(exc) or "The Feed could not be fetched.") from exc
        if result.not_modified:
            raise ServiceError("The Feed could not be fetched.")
        try:
            parsed = await asyncio.to_thread(self._parse, result.body, result.url or url)
        except ParseError as exc:
            log.info("No feed from %s: %s", _host(url) or "that address", exc)
            raise ServiceError(str(exc) or "That address did not return a feed.") from exc
        except Exception as exc:
            log.warning("Could not parse %s", _host(url) or "that address", exc_info=True)
            raise ServiceError("That address did not return a feed that can be read.") from exc
        return parsed, result

    async def _newest(self, feed: Feed) -> Item:
        parsed, _ = await self._read(feed.url)
        if not parsed.items:
            raise ServiceError("The Feed does not list any Items right now.")
        dated = [item for item in parsed.items if item.published is not None]
        if not dated:
            return parsed.items[0]
        return max(dated, key=lambda item: item.published or 0)  # the first of equals

    async def _discover(self, feed: Feed) -> SiteIdentity:
        """The site's identity. A source that cannot be read is stood in for by what is stored."""
        try:
            parsed, _ = await self._read(feed.url)
        except ServiceError:
            parsed = ParsedFeed(title=feed.source_title, link=feed.source_link, image="", items=())
        return await discover(parsed, feed.url, self._fetcher)


def _edits(before: Feed, after: Feed) -> list[Change]:
    """What an edit changed, as shown: nothing for a setting that was saved as it was."""
    shown = [
        ("Name", before.name, after.name),
        ("Channel", f"<#{before.channel_id}>", f"<#{after.channel_id}>"),
        ("Address", before.url, after.url),
        ("Check interval", duration_words(before.interval_s), duration_words(after.interval_s)),
    ]
    return [Change(label, old, new) for label, old, new in shown if old != new]


def _post_as_shown(feed: Feed) -> str:
    """Post as in words, with the name posted under when it is the site's or a custom one."""
    words = POST_AS_WORDS[feed.post_as]
    name = {PostAs.SITE: feed.site_name, PostAs.CUSTOM: feed.custom_name}.get(feed.post_as, "")
    return f"{words} ({name})" if name else words


def _picture(feed: Feed) -> str:
    return {PostAs.SITE: feed.site_icon, PostAs.CUSTOM: feed.custom_avatar}.get(feed.post_as, "")


def _filters_detail(did: str, filters: Sequence[Filter]) -> str:
    """ "added block Filter "sponsored"", "removed must-have Filter "linux" (title only)"."""
    first = filters[0]
    words = ", ".join(f'"{flt.word}"' for flt in filters)
    noun = "Filter" if len(filters) == 1 else "Filters"
    where = "" if first.field is FilterField.ANY else f" ({first.field.value} only)"
    return f"{did} {_LIST_WORDS[first.list]} {noun} {words}{where}"


def _identity_changes(identity: SiteIdentity, now: int) -> dict[str, Any]:
    return {"site_name": identity.name, "site_icon": identity.icon, "site_checked_at": now}


def _clean_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ServiceError("The Feed address cannot be empty.")
    if "://" not in url:
        url = "https://" + url
    if len(url) > MAX_URL_CHARS:
        raise ServiceError(f"The Feed address can be at most {MAX_URL_CHARS} characters long.")
    if not _is_web_url(url):
        raise ServiceError("The Feed address must be a web address starting with http or https.")
    return url


def _is_web_url(url: str) -> bool:
    if any(c.isspace() or not c.isprintable() for c in url):
        return False
    try:
        parts = urlsplit(url)
        return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _name(name: str | None, parsed: ParsedFeed, url: str) -> str:
    for candidate in (name or "", parsed.title, _host(url), url):
        cleaned = " ".join(candidate.split())[:MAX_NAME_CHARS].rstrip()
        if cleaned:
            return cleaned
    return "Feed"


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())[:MAX_NAME_CHARS].rstrip()
    if not cleaned:
        raise ServiceError("The Feed name cannot be empty.")
    return cleaned


def _check_interval(interval_s: int) -> int:
    if isinstance(interval_s, bool) or not isinstance(interval_s, int):
        raise ServiceError("The Check interval must be a whole number.")
    if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
        raise ServiceError(
            f"The Check interval must be between {MIN_INTERVAL_S // 60} minutes "
            f"and {MAX_INTERVAL_S // 3600} hours."
        )
    return interval_s


def _check_custom(name: str, avatar: str) -> tuple[str, str]:
    name = " ".join(name.split())
    avatar = avatar.strip()
    if not name:
        raise ServiceError("A custom name is required to post under a custom name.")
    if len(name) > MAX_USERNAME:
        raise ServiceError(f"The custom name can be at most {MAX_USERNAME} characters long.")
    if avatar and (len(avatar) > MAX_EMBED_URL or not _is_web_url(avatar)):
        raise ServiceError("The custom picture must be a web address starting with http or https.")
    return name, avatar


def _template(text: str, what: str, limit: int, *, required: bool = False) -> str:
    """A Template string that can be stored, or ServiceError saying why not."""
    if required and not text.strip():
        raise ServiceError(f"{what} cannot be empty.")
    if len(text) > limit:
        raise ServiceError(f"{what} can be at most {limit} characters long.")
    try:
        tpl.validate(text)
    except tpl.TemplateError as exc:
        raise ServiceError(str(exc)) from exc
    return text


def _url_template(text: str, what: str, limit: int, *, required: bool = False) -> str:
    text = _template(text.strip(), what, limit, required=required)
    if text and not text.lower().startswith(("http://", "https://", "{{")):
        raise ServiceError(f"{what} must start with http://, https:// or a Placeholder.")
    return text


def _colour(colour: int | str) -> int | None:
    """An int, a hex string such as "#ff8800", or "" for no colour."""
    if isinstance(colour, str):
        text = colour.strip().removeprefix("#")
        if not text:
            return None
        if len(text) != 6 or not all(c in "0123456789abcdefABCDEF" for c in text):
            raise ServiceError("The colour must be a hex code such as #ff8800.")
        return int(text, 16)
    if isinstance(colour, bool) or not isinstance(colour, int) or not 0 <= colour <= MAX_COLOUR:
        raise ServiceError("The colour must be a hex code such as #ff8800.")
    return colour


def _cut(value: str) -> str:
    if len(value) <= MAX_PLACEHOLDER_VALUE_CHARS:
        return value
    return value[: MAX_PLACEHOLDER_VALUE_CHARS - 1].rstrip() + tpl.ELLIPSIS
