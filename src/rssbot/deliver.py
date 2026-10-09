"""Posting rendered messages to Discord, and notes and Log entries to a Server's Logs channel.

`DiscordDeliverer.deliver` never raises: every failure is turned into a DeliveryOutcome by
`classify_error`, so one bad Item or one broken channel cannot hold up anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord

from . import logembed
from .db import Database
from .journal import quote
from .logsetup import site
from .models import Actor, ChannelKind, EmbedSpec, Feed, LogEntry, OutgoingMessage
from .ports import DeliveryOutcome, Fetcher, ImageData, Notifier

log = logging.getLogger(__name__)

WEBHOOK_NAME = "RSS feeds"  # Discord refuses names containing "discord" or "clyde"
DEFAULT_THREAD_TITLE = "New item"
MAX_THREAD_TITLE = 100
MAX_USERNAME = 80
MAX_NOTE = 2000

# Discord's JSON error codes. discord.py does not name them; these come from Discord's API docs.
UNKNOWN_CHANNEL = 10003
UNKNOWN_GUILD = 10004
UNKNOWN_WEBHOOK = 10015
MAX_WEBHOOKS_REACHED = 30007
TAG_REQUIRED = 40067
MISSING_ACCESS = 50001
MISSING_PERMISSIONS = 50013
INVALID_WEBHOOK_TOKEN = 50027
THREAD_ARCHIVED = 50083
THREAD_LOCKED = 160005

_LOST_CODES = frozenset(
    {
        UNKNOWN_CHANNEL,
        UNKNOWN_GUILD,
        MISSING_ACCESS,
        MISSING_PERMISSIONS,
        THREAD_ARCHIVED,
        THREAD_LOCKED,
    }
)
_WEBHOOK_GONE_CODES = frozenset({UNKNOWN_WEBHOOK, INVALID_WEBHOOK_TOKEN})

_FORBIDDEN_NAME = re.compile(r"discord|clyde", re.IGNORECASE)


class Step(enum.Enum):
    """What was being attempted when an error happened."""

    CHANNEL_FETCH = "channel_fetch"
    SEND = "send"  # a message sent as the bot
    THREAD_CREATE = "thread_create"  # a Forum post created as the bot
    WEBHOOK_SEND = "webhook_send"
    WEBHOOK_CREATE = "webhook_create"


_CONTENT_STEPS = frozenset({Step.SEND, Step.THREAD_CREATE, Step.WEBHOOK_SEND})


def classify_error(exc: BaseException, step: Step) -> DeliveryOutcome:
    """Map an error raised at `step` to an outcome. When in doubt, RETRY."""
    if not isinstance(exc, discord.HTTPException):
        # fetch_channel raises InvalidData for a channel of a type that cannot hold messages.
        if isinstance(exc, discord.InvalidData) and step is Step.CHANNEL_FETCH:
            return DeliveryOutcome.LOST_CHANNEL
        return DeliveryOutcome.RETRY  # rate-limit exhaustion, network, timeout, anything else

    status, code = exc.status, exc.code
    if code == TAG_REQUIRED:
        return DeliveryOutcome.NEEDS_TAG
    if status >= 500 or status in (401, 408, 429):
        return DeliveryOutcome.RETRY
    if code in _WEBHOOK_GONE_CODES:
        return DeliveryOutcome.RETRY  # still gone after it was recreated once
    if step is Step.WEBHOOK_CREATE and code != MISSING_ACCESS and status != 404:
        return DeliveryOutcome.RETRY  # says nothing about the channel or the message
    if code in _LOST_CODES:
        return DeliveryOutcome.LOST_CHANNEL
    if status in (403, 404):
        # A webhook has no permissions of its own, so this is not about the channel.
        return DeliveryOutcome.RETRY if step is Step.WEBHOOK_SEND else DeliveryOutcome.LOST_CHANNEL
    if status in (400, 413) and step in _CONTENT_STEPS:
        return DeliveryOutcome.REJECTED
    return DeliveryOutcome.RETRY


def webhook_is_gone(exc: BaseException) -> bool:
    """Whether a webhook send failed because the webhook was deleted or its token is invalid."""
    if not isinstance(exc, discord.HTTPException):
        return False
    if exc.code in _WEBHOOK_GONE_CODES or exc.status == 401:
        return True
    # A 404 that is not about the target thread can only be about the webhook itself.
    return exc.status == 404 and exc.code != UNKNOWN_CHANNEL


def webhook_unavailable(exc: BaseException) -> bool:
    """Whether creating a webhook failed in a way that posting as the bot works around."""
    if not isinstance(exc, discord.HTTPException):
        return False
    if exc.status == 403:
        return exc.code != MISSING_ACCESS
    return exc.status == 400  # includes the channel's webhook limit


def build_allowed_mentions(role_ids: tuple[int, ...]) -> discord.AllowedMentions:
    """Permit pinging exactly these roles. Every field is explicit, so the client's
    own defaults are never merged in."""
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role_id) for role_id in role_ids] if role_ids else False,
        replied_user=False,
    )


def build_embed(
    spec: EmbedSpec | None, *, fallback_image: str = "", published: int | None = None
) -> discord.Embed | None:
    """The Embed, or None if there is nothing to show in it.

    `published` is shown after the footer, converted by Discord to each viewer's own time.
    """
    if spec is None:
        return None
    embed = discord.Embed(
        title=spec.title or None,
        description=spec.description or None,
        url=spec.url or None,
        colour=spec.colour,
    )
    image = spec.image or fallback_image
    if image:
        embed.set_image(url=image)
    if spec.footer:
        embed.set_footer(text=spec.footer)
    if spec.timestamp and published is not None:
        # An impossible date is left out, as {{date}} is.
        with contextlib.suppress(OverflowError, OSError, ValueError):
            embed.timestamp = datetime.fromtimestamp(published, UTC)
    for field in spec.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    if not (embed.title or embed.description or image or spec.footer or spec.fields):
        return None
    return embed


def build_view(message: OutgoingMessage) -> discord.ui.View | None:
    """Link buttons only: such a view is never registered with the client and, with no
    timeout, starts no task."""
    if not message.buttons:
        return None
    view = discord.ui.View(timeout=None)
    for button in message.buttons:
        view.add_item(discord.ui.Button(label=button.label, url=button.url))
    return view


def safe_username(name: str) -> str:
    """A Post as name Discord accepts for a webhook message, or "" if nothing is left."""
    name = _FORBIDDEN_NAME.sub(lambda m: m.group()[:2] + "*" + m.group()[3:], name)
    return name.strip()[:MAX_USERNAME].strip()


@dataclass(frozen=True, slots=True)
class _Parts:
    content: str
    embed: discord.Embed | None
    view: discord.ui.View | None
    allowed_mentions: discord.AllowedMentions


@dataclass(frozen=True, slots=True)
class _Target:
    channel: Any  # TextChannel, Thread or ForumChannel
    forum: bool
    title: str = ""
    tags: tuple[Any, ...] = ()


def _build_parts(message: OutgoingMessage, *, fallback_image: str = "") -> _Parts:
    return _Parts(
        content=message.content,
        embed=build_embed(
            message.embed, fallback_image=fallback_image, published=message.published
        ),
        view=build_view(message),
        allowed_mentions=build_allowed_mentions(message.mention_role_ids),
    )


def _message_kwargs(parts: _Parts, file: discord.File | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"allowed_mentions": parts.allowed_mentions}
    if parts.content:
        kwargs["content"] = parts.content
    if parts.embed is not None:
        kwargs["embed"] = parts.embed
    if parts.view is not None:
        kwargs["view"] = parts.view
    if file is not None:
        kwargs["file"] = file
    return kwargs


def _make_file(image: ImageData | None) -> discord.File | None:
    # A new File per attempt: discord.py closes a File once it has been sent.
    if image is None:
        return None
    return discord.File(io.BytesIO(image.data), filename=image.filename or "cover")


def _close_file(file: discord.File | None) -> None:
    if file is not None:
        file.close()
        file.fp.close()  # File.close leaves a buffer it was handed open; free the bytes now


async def _resolve_channel(client: discord.Client, channel_id: int) -> Any:
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel


class DiscordDeliverer:
    def __init__(
        self,
        client: discord.Client,
        db: Database,
        fetcher: Fetcher,
        notifier: Notifier | None = None,
    ) -> None:
        self._client = client
        self._db = db
        self._fetcher = fetcher
        self._notifier = notifier
        # So two Feeds in a channel never create two webhooks at once.
        self._webhook_locks: dict[int, asyncio.Lock] = {}
        self._warned_channels: set[int] = set()

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        try:
            return await self._deliver(feed, message)
        except Exception:
            # Every expected failure is classified where it happens; this is the safety net.
            log.exception("deliver.error feed=%s channel=%s", feed.id, feed.channel_id)
            return DeliveryOutcome.RETRY

    async def cleanup_webhook(self, channel_id: int) -> None:
        """Delete the channel's webhook from Discord and the store. Never raises."""
        try:
            stored = self._db.get_webhook(channel_id)
            if stored is None:
                return
            try:
                await self._partial_webhook(*stored).delete(reason="No Feed needs it any more")
            except discord.HTTPException as exc:
                if exc.status not in (401, 403, 404):
                    # Keep the record so the webhook can be reused or cleaned up later.
                    log.warning(
                        "webhook.delete_failed channel=%s reason=%s", channel_id, quote(str(exc))
                    )
                    return
            self._db.delete_webhook(channel_id)
        except Exception:
            log.exception("Could not clean up the webhook of channel %s", channel_id)

    # -- one delivery --

    async def _deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        try:
            channel = await _resolve_channel(self._client, feed.channel_id)
        except Exception as exc:
            return classify_error(exc, Step.CHANNEL_FETCH)

        # The channel cache spans every Server; a Feed only ever posts in its own.
        guild = getattr(channel, "guild", None)
        if getattr(guild, "id", feed.server_id) != feed.server_id:
            return DeliveryOutcome.LOST_CHANNEL

        forum = feed.channel_kind is ChannelKind.FORUM
        expected = discord.ForumChannel if forum else (discord.TextChannel, discord.Thread)
        if not isinstance(channel, expected):
            return DeliveryOutcome.LOST_CHANNEL

        if forum:
            wanted = set(message.tag_ids)
            tags = tuple(tag for tag in channel.available_tags if tag.id in wanted)
            if not tags and channel.flags.require_tag:
                return DeliveryOutcome.NEEDS_TAG
            title = (message.thread_title or "").strip()[:MAX_THREAD_TITLE].strip()
            target = _Target(channel, True, title or DEFAULT_THREAD_TITLE, tags)
        else:
            target = _Target(channel, False)
            if isinstance(channel, discord.Thread) and channel.archived and not channel.locked:
                await self._unarchive(channel)

        cover_url = message.cover_image_url if forum else None
        try:
            parts = _build_parts(message, fallback_image=cover_url or "")
            cover_parts = _build_parts(message) if cover_url else parts
        except Exception:
            log.warning(
                "deliver.error feed=%s detail=%s",
                feed.id,
                quote("unbuildable message"),
                exc_info=True,
            )
            return DeliveryOutcome.REJECTED

        if cover_url:
            # No lock shared between Feeds here: a slow image or a hanging send for one Feed
            # must never hold up another. The Check loop's concurrency limit bounds memory.
            image = await self._download_cover(cover_url)
            if image is not None:
                outcome = await self._send(feed, target, message, cover_parts, image)
                image = None
                # The attachment itself may be what was refused (too large, or no
                # Attach Files permission), so try once more without it.
                if outcome not in (DeliveryOutcome.REJECTED, DeliveryOutcome.LOST_CHANNEL):
                    return outcome
        return await self._send(feed, target, message, parts, None)

    async def _unarchive(self, thread: discord.Thread) -> None:
        try:
            await thread.edit(archived=False)
        except Exception:
            log.debug("Could not unarchive thread %s", thread.id, exc_info=True)

    async def _download_cover(self, url: str) -> ImageData | None:
        try:
            return await self._fetcher.fetch_image(url)
        except Exception as exc:
            # Per Item, so DEBUG. Only the site is named: the address can hold a private key.
            log.debug("deliver.cover_skipped host=%s reason=%s", quote(site(url)), quote(str(exc)))
            return None

    async def _send(
        self,
        feed: Feed,
        target: _Target,
        message: OutgoingMessage,
        parts: _Parts,
        image: ImageData | None,
    ) -> DeliveryOutcome:
        username = safe_username(message.username) if message.username is not None else ""
        if username:
            outcome = await self._send_via_webhook(feed, target, message, username, parts, image)
            if outcome is not None:
                return outcome
        return await self._send_as_bot(target, parts, image)

    async def _send_as_bot(
        self, target: _Target, parts: _Parts, image: ImageData | None
    ) -> DeliveryOutcome:
        step = Step.THREAD_CREATE if target.forum else Step.SEND
        file: discord.File | None = None
        try:
            file = _make_file(image)
            kwargs = _message_kwargs(parts, file)
            if target.forum:
                if target.tags:
                    kwargs["applied_tags"] = list(target.tags)
                await target.channel.create_thread(name=target.title, **kwargs)
            else:
                # Announcement channels: the message is deliberately not published.
                await target.channel.send(**kwargs)
        except Exception as exc:
            return classify_error(exc, step)
        finally:
            _close_file(file)
        return DeliveryOutcome.DELIVERED

    # -- webhooks --

    async def _send_via_webhook(
        self,
        feed: Feed,
        target: _Target,
        message: OutgoingMessage,
        username: str,
        parts: _Parts,
        image: ImageData | None,
    ) -> DeliveryOutcome | None:
        """Send as `username`. None means: no webhook can be had, post as the bot instead."""
        for attempt in range(2):
            try:
                webhook = await self._get_webhook(feed.channel_id, target.channel)
            except Exception as exc:
                if webhook_unavailable(exc):
                    await self._warn_once(feed, exc)
                    return None
                return classify_error(exc, Step.WEBHOOK_CREATE)

            file: discord.File | None = None
            try:
                file = _make_file(image)
                kwargs = _message_kwargs(parts, file)
                kwargs["username"] = username
                if message.avatar_url:
                    kwargs["avatar_url"] = message.avatar_url
                if target.forum:
                    kwargs["thread_name"] = target.title
                    if target.tags:
                        kwargs["applied_tags"] = list(target.tags)
                elif isinstance(target.channel, discord.Thread):
                    kwargs["thread"] = target.channel
                # wait=True, or Discord answers before it has checked the message.
                await webhook.send(wait=True, **kwargs)
            except Exception as exc:
                if attempt == 0 and webhook_is_gone(exc):
                    self._db.delete_webhook(feed.channel_id)
                    continue
                return classify_error(exc, Step.WEBHOOK_SEND)
            finally:
                _close_file(file)
            return DeliveryOutcome.DELIVERED
        return DeliveryOutcome.RETRY  # not reached: the second attempt always returns

    async def _get_webhook(self, channel_id: int, channel: Any) -> discord.Webhook:
        """The channel's stored webhook, creating and storing one if there is none."""
        stored = self._db.get_webhook(channel_id)
        if stored is not None:
            return self._partial_webhook(*stored)
        # One lock per channel, taken only to create: a creation that hangs in one channel
        # must never hold up deliveries anywhere else.
        lock = self._webhook_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            stored = self._db.get_webhook(channel_id)
            if stored is not None:
                return self._partial_webhook(*stored)
            parent = channel
            if isinstance(channel, discord.Thread):
                # A thread has no webhooks of its own; its parent's are sent with a thread target.
                parent = await _resolve_channel(self._client, channel.parent_id)
            webhook = await parent.create_webhook(name=WEBHOOK_NAME, reason="Post as for RSS feeds")
            if not webhook.token:
                raise RuntimeError("Discord returned a webhook without a token")
            self._db.set_webhook(channel_id, webhook.id, webhook.token)
            return webhook

    def _partial_webhook(self, webhook_id: int, token: str) -> discord.Webhook:
        return discord.Webhook.partial(webhook_id, token, client=self._client)

    async def _warn_once(self, feed: Feed, exc: BaseException) -> None:
        """Tell the Logs channel, once per channel per run, why Post as is not being used."""
        if feed.channel_id in self._warned_channels:
            return
        self._warned_channels.add(feed.channel_id)
        log.warning(
            "webhook.unavailable channel=%s server=%s reason=%s fallback=bot",
            feed.channel_id,
            feed.server_id,
            quote(str(exc)),
        )
        if self._notifier is None:
            return
        if getattr(exc, "code", 0) == MAX_WEBHOOKS_REACHED:
            why = "that channel has reached Discord's limit on webhooks"
            fix = "Delete a webhook you no longer need there"
        else:
            why = "the bot is missing the **Manage Webhooks** permission there"
            fix = "Give the bot that permission"
        text = (
            f"Feeds in <#{feed.channel_id}> are being posted under the bot's own name, "
            f"because {why}. {fix} and they will go back to their own name and picture."
        )
        try:
            await self._notifier.notify(feed.server_id, text)
        except Exception:
            log.exception("The notifier failed for Server %s", feed.server_id)


class DiscordNotifier:
    """Posts to a Server's Logs channel: embeds, or plain text where the bot may not embed."""

    def __init__(self, client: discord.Client, db: Database) -> None:
        self._client = client
        self._db = db

    async def notify(self, server_id: int, text: str) -> None:
        try:
            channel = await self._logs_channel(server_id)
            if channel is None:
                return
            embed = logembed.build_note(text, self._shown(Actor.bot()))
            await self._post(channel, [[embed]], [text[:MAX_NOTE]])
        except Exception as exc:
            log.warning("logs_channel.post_failed server=%s reason=%s", server_id, quote(str(exc)))

    async def announce(self, server_id: int, entries: Sequence[LogEntry], actor: Actor) -> None:
        try:
            if not entries:
                return
            channel = await self._logs_channel(server_id)
            if channel is None:
                return
            actor = self._shown(actor)
            await self._post(
                channel,
                logembed.build_messages(entries, actor),
                logembed.render_text(entries, actor),
            )
        except Exception as exc:
            log.warning("logs_channel.post_failed server=%s reason=%s", server_id, quote(str(exc)))

    async def _logs_channel(self, server_id: int) -> Any:
        """The Server's Logs channel, or None if it has none that takes messages."""
        server = self._db.get_server(server_id)
        if server is None or server.logs_channel_id is None:
            return None
        channel = await _resolve_channel(self._client, server.logs_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return None
        # The channel cache spans every Server; a Server's reports only go to its own.
        guild = getattr(channel, "guild", None)
        if getattr(guild, "id", server_id) != server_id:
            return None
        return channel

    def _shown(self, actor: Actor) -> Actor:
        """The bot as an actor goes under the bot's own name and picture."""
        if not actor.is_bot:
            return actor
        user = getattr(self._client, "user", None)
        if user is None:
            return actor
        avatar = getattr(user, "display_avatar", None)
        return Actor(id=None, name=user.display_name, avatar_url=getattr(avatar, "url", ""))

    async def _post(
        self, channel: Any, messages: list[list[discord.Embed]], texts: list[str]
    ) -> None:
        """Send the embeds, or the plain text if the bot may not embed in this channel."""
        nobody = discord.AllowedMentions.none()
        if _may_embed(channel):
            for number, embeds in enumerate(messages):
                try:
                    await channel.send(embeds=embeds, allowed_mentions=nobody)
                except discord.Forbidden:
                    if number:
                        raise  # part of the report is out; do not say it all again
                    break  # the permission check could not tell: fall back to plain text
            else:
                return
        for text in texts:
            await channel.send(text, allowed_mentions=nobody)


def _may_embed(channel: Any) -> bool:
    """Whether the bot has Embed Links in the channel. True when that cannot be worked out."""
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is None:
        return True
    return bool(channel.permissions_for(me).embed_links)
