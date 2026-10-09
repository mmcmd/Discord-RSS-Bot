from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from rssbot import logembed
from rssbot.db import Database
from rssbot.deliver import (
    DEFAULT_THREAD_TITLE,
    WEBHOOK_NAME,
    DiscordDeliverer,
    DiscordNotifier,
    Step,
    build_allowed_mentions,
    build_embed,
    build_view,
    classify_error,
    safe_username,
    webhook_is_gone,
    webhook_unavailable,
)
from rssbot.models import (
    Actor,
    ButtonSpec,
    ChannelKind,
    EmbedSpec,
    Feed,
    FieldSpec,
    LogEntry,
    LogKind,
    OutgoingMessage,
)
from rssbot.ports import DeliveryOutcome, FetchError, ImageData

DELIVERED = DeliveryOutcome.DELIVERED
RETRY = DeliveryOutcome.RETRY
REJECTED = DeliveryOutcome.REJECTED
LOST = DeliveryOutcome.LOST_CHANNEL
NEEDS_TAG = DeliveryOutcome.NEEDS_TAG

SERVER = 1
TEXT, THREAD, FORUM, LOGS = 100, 200, 300, 400


def http_error(status: int, code: int = 0) -> discord.HTTPException:
    if status == 403:
        cls = discord.Forbidden
    elif status == 404:
        cls = discord.NotFound
    elif status >= 500:
        cls = discord.DiscordServerError
    else:
        cls = discord.HTTPException
    response = SimpleNamespace(status=status, reason="reason")
    return cls(response, {"code": code, "message": "message"})  # type: ignore[arg-type]


# -- fakes --


class Calls:
    """Records calls and raises queued errors, one per call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.errors: list[BaseException] = []

    def record(self, kwargs: dict[str, Any]) -> None:
        file = kwargs.get("file")
        if file is not None:
            kwargs = {**kwargs, "file_bytes": file.fp.read(), "filename": file.filename}
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)


class WebhookMaker:
    def _init_webhooks(self) -> None:
        self.created: list[FakeWebhook] = []
        self.create_errors: list[BaseException] = []

    async def create_webhook(self, *, name: str, reason: str | None = None) -> FakeWebhook:
        if self.create_errors:
            raise self.create_errors.pop(0)
        webhook = FakeWebhook(9000 + len(self.created), f"token{len(self.created)}", name=name)
        self.created.append(webhook)
        return webhook


class FakeText(WebhookMaker, discord.TextChannel):
    def __init__(self, id: int = TEXT) -> None:
        self.id = id
        self.sends = Calls()
        self._init_webhooks()

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if content is not None:
            kwargs["content"] = content
        self.sends.record(kwargs)


class FakeThread(discord.Thread):
    def __init__(self, id: int = THREAD, parent_id: int = TEXT) -> None:
        self.id = id
        self.parent_id = parent_id
        self.archived = False
        self.locked = False
        self.sends = Calls()
        self.edits: list[dict[str, Any]] = []
        self.edit_error: BaseException | None = None

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if content is not None:
            kwargs["content"] = content
        self.sends.record(kwargs)

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        if self.edit_error:
            raise self.edit_error


class FakeForum(WebhookMaker, discord.ForumChannel):
    def __init__(self, id: int = FORUM, tag_ids: tuple[int, ...] = (), require_tag: bool = False):
        self.id = id
        self.tags = [SimpleNamespace(id=tag_id) for tag_id in tag_ids]
        self.require_tag = require_tag
        self.posts = Calls()
        self._init_webhooks()

    @property
    def flags(self) -> Any:
        return SimpleNamespace(require_tag=self.require_tag)

    @property
    def available_tags(self) -> Any:
        return self.tags

    async def create_thread(self, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        self.posts.record(kwargs)


class FakeWebhook:
    def __init__(self, id: int, token: str | None, name: str = "") -> None:
        self.id = id
        self.token = token
        self.name = name
        self.sends = Calls()
        self.deleted = False
        self.delete_error: BaseException | None = None

    async def send(self, **kwargs: Any) -> None:
        await asyncio.sleep(0)
        self.sends.record(kwargs)

    async def delete(self, **kwargs: Any) -> None:
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


class FakeClient:
    def __init__(self) -> None:
        self.cached: dict[int, Any] = {}
        self.fetchable: dict[int, Any] = {}
        self.fetch_error: BaseException | None = None
        self.get_error: BaseException | None = None
        self.fetched: list[int] = []

    def get_channel(self, channel_id: int) -> Any:
        if self.get_error:
            raise self.get_error
        return self.cached.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> Any:
        self.fetched.append(channel_id)
        if self.fetch_error:
            raise self.fetch_error
        if channel_id not in self.fetchable:
            raise http_error(404, 10003)
        return self.fetchable[channel_id]


class FakeFetcher:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.urls: list[str] = []
        self.events: list[str] = []

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        self.urls.append(url)
        self.events.append("download")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return ImageData(b"IMG:" + url.encode(), "cover.png", "image/png")


class FakeNotifier:
    def __init__(self) -> None:
        self.notes: list[tuple[int, str]] = []
        self.error: BaseException | None = None

    async def notify(self, server_id: int, text: str) -> None:
        self.notes.append((server_id, text))
        if self.error:
            raise self.error

    async def announce(self, server_id: int, entries: object, actor: object) -> None:
        pass


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db = Database(":memory:")
        self.client = FakeClient()
        self.fetcher = FakeFetcher()
        self.notifier = FakeNotifier()
        self.text = FakeText()
        self.thread = FakeThread()
        self.forum = FakeForum(tag_ids=(1, 2, 3))
        for channel in (self.text, self.thread, self.forum):
            self.client.cached[channel.id] = channel
        self.partials: dict[int, FakeWebhook] = {}  # webhooks rebuilt from the store
        monkeypatch.setattr(discord.Webhook, "partial", self._partial)
        self.deliverer = DiscordDeliverer(
            self.client,  # type: ignore[arg-type]
            self.db,
            self.fetcher,  # type: ignore[arg-type]
            self.notifier,
        )

    def _partial(self, id: int, token: str, **kwargs: Any) -> FakeWebhook:
        assert kwargs == {"client": self.client}
        return self.partials.setdefault(id, FakeWebhook(id, token))

    def feed(self, channel_id: int = TEXT, kind: ChannelKind = ChannelKind.MESSAGES) -> Feed:
        return self.db.create_feed(
            server_id=SERVER,
            channel_id=channel_id,
            channel_kind=kind,
            name="Feed",
            url="https://example.com/feed",
            now=0,
        )

    def forum_feed(self) -> Feed:
        return self.feed(FORUM, ChannelKind.FORUM)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


MSG = OutgoingMessage(content="hello")
AS_SITE = OutgoingMessage(content="hello", username="The Site", avatar_url="https://e.com/a.png")


# -- classification --


@pytest.mark.parametrize(
    ("exc", "step", "expected"),
    [
        (http_error(500), Step.SEND, RETRY),
        (http_error(503), Step.WEBHOOK_SEND, RETRY),
        (http_error(502), Step.CHANNEL_FETCH, RETRY),
        (http_error(429), Step.SEND, RETRY),
        (http_error(401), Step.SEND, RETRY),
        (discord.RateLimited(60.0), Step.SEND, RETRY),
        (TimeoutError(), Step.SEND, RETRY),
        (OSError("network"), Step.THREAD_CREATE, RETRY),
        (RuntimeError("?"), Step.WEBHOOK_SEND, RETRY),
        (ValueError("?"), Step.CHANNEL_FETCH, RETRY),
        (http_error(400, 50035), Step.SEND, REJECTED),
        (http_error(400, 50035), Step.THREAD_CREATE, REJECTED),
        (http_error(400, 50035), Step.WEBHOOK_SEND, REJECTED),
        (http_error(400, 50006), Step.SEND, REJECTED),
        (http_error(413, 40005), Step.THREAD_CREATE, REJECTED),
        (http_error(400), Step.CHANNEL_FETCH, RETRY),
        (http_error(400, 50035), Step.WEBHOOK_CREATE, RETRY),
        (http_error(400, 30007), Step.WEBHOOK_CREATE, RETRY),
        (http_error(405), Step.SEND, RETRY),
        (http_error(404, 10003), Step.SEND, LOST),
        (http_error(404, 10003), Step.CHANNEL_FETCH, LOST),
        (http_error(404, 10003), Step.WEBHOOK_SEND, LOST),
        (http_error(404, 10003), Step.WEBHOOK_CREATE, LOST),
        (http_error(404), Step.THREAD_CREATE, LOST),
        (http_error(404, 10004), Step.CHANNEL_FETCH, LOST),
        (http_error(403, 50001), Step.SEND, LOST),
        (http_error(403, 50001), Step.CHANNEL_FETCH, LOST),
        (http_error(403, 50001), Step.WEBHOOK_CREATE, LOST),
        (http_error(403, 50013), Step.SEND, LOST),
        (http_error(403, 50013), Step.THREAD_CREATE, LOST),
        (http_error(403), Step.SEND, LOST),
        (http_error(403, 50013), Step.WEBHOOK_CREATE, RETRY),
        (http_error(403), Step.WEBHOOK_SEND, RETRY),
        (http_error(400, 50083), Step.SEND, LOST),
        (http_error(400, 160005), Step.SEND, LOST),
        (http_error(403, 160005), Step.WEBHOOK_SEND, LOST),
        (discord.InvalidData("unknown type"), Step.CHANNEL_FETCH, LOST),
        (discord.InvalidData("unknown type"), Step.SEND, RETRY),
        (http_error(400, 40067), Step.THREAD_CREATE, NEEDS_TAG),
        (http_error(400, 40067), Step.WEBHOOK_SEND, NEEDS_TAG),
        (http_error(404, 10015), Step.WEBHOOK_SEND, RETRY),
        (http_error(401, 50027), Step.WEBHOOK_SEND, RETRY),
        (http_error(404), Step.WEBHOOK_SEND, RETRY),
    ],
)
def test_classify_error(exc: BaseException, step: Step, expected: DeliveryOutcome) -> None:
    assert classify_error(exc, step) is expected


def test_webhook_is_gone() -> None:
    assert webhook_is_gone(http_error(404, 10015))
    assert webhook_is_gone(http_error(404))
    assert webhook_is_gone(http_error(401, 50027))
    assert webhook_is_gone(http_error(403, 50027))
    assert not webhook_is_gone(http_error(404, 10003))  # the target thread is gone, not the hook
    assert not webhook_is_gone(http_error(400, 50035))
    assert not webhook_is_gone(http_error(500))
    assert not webhook_is_gone(RuntimeError())


def test_webhook_unavailable() -> None:
    assert webhook_unavailable(http_error(403, 50013))
    assert webhook_unavailable(http_error(403))
    assert webhook_unavailable(http_error(400, 30007))
    assert not webhook_unavailable(http_error(403, 50001))
    assert not webhook_unavailable(http_error(404, 10003))
    assert not webhook_unavailable(http_error(500))
    assert not webhook_unavailable(OSError())


# -- building --


def test_allowed_mentions_permit_only_the_given_roles() -> None:
    mentions = build_allowed_mentions((11, 22))
    assert mentions.to_dict() == {"parse": [], "roles": [11, 22]}
    # The client's own defaults cannot widen it.
    merged = discord.AllowedMentions.all().merge(mentions)
    assert merged.to_dict() == {"parse": [], "roles": [11, 22]}


def test_allowed_mentions_without_roles_permit_nothing() -> None:
    merged = discord.AllowedMentions.all().merge(build_allowed_mentions(()))
    assert merged.to_dict() == {"parse": []}


def _embed_dict(embed: discord.Embed) -> dict[str, Any]:
    data = dict(embed.to_dict())
    data.pop("flags", None)  # discord.py adds its own, always 0 here
    return data


def test_embed_is_built_from_the_spec() -> None:
    spec = EmbedSpec(
        title="Title",
        description="Body",
        url="https://e.com/item",
        image="https://e.com/i.png",
        footer="Foot",
        colour=0x112233,
        fields=(FieldSpec("A", "1", inline=True), FieldSpec("B", "2")),
    )
    embed = build_embed(spec)
    assert embed is not None
    assert _embed_dict(embed) == {
        "type": "rich",
        "title": "Title",
        "description": "Body",
        "url": "https://e.com/item",
        "color": 0x112233,
        "image": {"url": "https://e.com/i.png"},
        "footer": {"text": "Foot"},
        "fields": [
            {"name": "A", "value": "1", "inline": True},
            {"name": "B", "value": "2", "inline": False},
        ],
    }


def test_embed_edge_cases() -> None:
    assert build_embed(None) is None
    assert build_embed(EmbedSpec()) is None
    minimal = build_embed(EmbedSpec(title="T"))
    assert minimal is not None and _embed_dict(minimal) == {"type": "rich", "title": "T"}
    fallback = build_embed(EmbedSpec(title="T"), fallback_image="https://e.com/c.png")
    assert fallback is not None and fallback.image.url == "https://e.com/c.png"
    kept = build_embed(EmbedSpec(image="https://e.com/own.png"), fallback_image="https://e.com/c")
    assert kept is not None and kept.image.url == "https://e.com/own.png"


async def test_view_holds_link_buttons_and_leaks_nothing() -> None:
    assert build_view(OutgoingMessage(content="x")) is None
    before = len(asyncio.all_tasks())
    message = OutgoingMessage(
        content="x",
        buttons=(ButtonSpec("Read", "https://e.com/1"), ButtonSpec("More", "https://e.com/2")),
    )
    view = build_view(message)
    assert view is not None
    assert view.timeout is None
    assert not view.is_dispatchable()  # so discord.py never registers it with the client
    assert len(asyncio.all_tasks()) == before
    buttons = view.to_components()[0]["components"]
    assert [(b["label"], b["url"], b["style"]) for b in buttons] == [
        ("Read", "https://e.com/1", discord.ButtonStyle.link.value),
        ("More", "https://e.com/2", discord.ButtonStyle.link.value),
    ]
    assert all("custom_id" not in b for b in buttons)


def test_safe_username() -> None:
    assert safe_username("The Site") == "The Site"
    assert "discord" not in safe_username("Discord Blog").lower()
    assert "clyde" not in safe_username("Clyde's News").lower()
    assert safe_username("   ") == ""
    assert len(safe_username("x" * 200)) == 80
    assert "discord" not in WEBHOOK_NAME.lower() and "clyde" not in WEBHOOK_NAME.lower()


# -- sending as the bot --


async def test_sends_as_the_bot(world: World) -> None:
    message = OutgoingMessage(
        content="hello <@&5>",
        embed=EmbedSpec(title="T"),
        buttons=(ButtonSpec("Read", "https://e.com"),),
        mention_role_ids=(5,),
    )
    assert await world.deliverer.deliver(world.feed(), message) is DELIVERED
    (call,) = world.text.sends.calls
    assert call["content"] == "hello <@&5>"
    assert call["embed"].title == "T"
    assert call["allowed_mentions"].to_dict() == {"parse": [], "roles": [5]}
    assert len(call["view"].children) == 1
    assert "file" not in call
    assert world.text.created == [] and world.client.fetched == []


async def test_empty_parts_are_left_out(world: World) -> None:
    message = OutgoingMessage(content="", embed=EmbedSpec(title="T"))
    assert await world.deliverer.deliver(world.feed(), message) is DELIVERED
    (call,) = world.text.sends.calls
    assert set(call) == {"embed", "allowed_mentions"}


async def test_cover_image_is_ignored_outside_forums(world: World) -> None:
    message = OutgoingMessage(content="x", cover_image_url="https://e.com/c.png")
    assert await world.deliverer.deliver(world.feed(), message) is DELIVERED
    assert world.fetcher.urls == []


async def test_channel_is_fetched_when_not_cached(world: World) -> None:
    del world.client.cached[TEXT]
    world.client.fetchable[TEXT] = world.text
    assert await world.deliverer.deliver(world.feed(), MSG) is DELIVERED
    assert world.client.fetched == [TEXT]
    assert len(world.text.sends.calls) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(404, 10003), LOST),
        (http_error(403, 50001), LOST),
        (discord.InvalidData("type"), LOST),
        (http_error(500), RETRY),
        (TimeoutError(), RETRY),
        (KeyError("guild_id"), RETRY),
    ],
)
async def test_channel_fetch_failures(
    world: World, error: BaseException, expected: DeliveryOutcome
) -> None:
    del world.client.cached[TEXT]
    world.client.fetch_error = error
    assert await world.deliverer.deliver(world.feed(), MSG) is expected


async def test_missing_channel_is_lost(world: World) -> None:
    assert await world.deliverer.deliver(world.feed(channel_id=999), MSG) is LOST


async def test_kind_mismatch_is_lost(world: World) -> None:
    deliver = world.deliverer.deliver
    assert await deliver(world.feed(FORUM, ChannelKind.MESSAGES), MSG) is LOST
    assert await deliver(world.feed(TEXT, ChannelKind.FORUM), MSG) is LOST
    assert await deliver(world.feed(THREAD, ChannelKind.FORUM), MSG) is LOST
    world.client.cached[500] = SimpleNamespace(id=500)  # e.g. a category
    assert await deliver(world.feed(500), MSG) is LOST
    assert world.text.sends.calls == [] and world.forum.posts.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(500), RETRY),
        (http_error(429), RETRY),
        (discord.RateLimited(99.0), RETRY),
        (TimeoutError(), RETRY),
        (ConnectionResetError(), RETRY),
        (RuntimeError("boom"), RETRY),
        (http_error(400, 50035), REJECTED),
        (http_error(403, 50013), LOST),
        (http_error(403, 50001), LOST),
        (http_error(404, 10003), LOST),
    ],
)
async def test_bot_send_failures(
    world: World, error: BaseException, expected: DeliveryOutcome
) -> None:
    world.text.sends.errors.append(error)
    assert await world.deliverer.deliver(world.feed(), MSG) is expected


async def test_thread_send_and_locked_thread(world: World) -> None:
    feed = world.feed(THREAD)
    assert await world.deliverer.deliver(feed, MSG) is DELIVERED
    assert world.thread.edits == []
    world.thread.archived = world.thread.locked = True
    world.thread.sends.errors.append(http_error(400, 50083))
    assert await world.deliverer.deliver(feed, MSG) is LOST
    assert world.thread.edits == []


async def test_archived_thread_is_reopened_first(world: World) -> None:
    world.thread.archived = True
    world.thread.edit_error = http_error(403, 50013)  # failing to reopen is not fatal
    assert await world.deliverer.deliver(world.feed(THREAD), MSG) is DELIVERED
    assert world.thread.edits == [{"archived": False}]


# -- webhooks --


async def test_webhook_is_created_stored_and_reused(world: World) -> None:
    first, second = world.feed(), world.feed()  # two Feeds in one channel
    assert await world.deliverer.deliver(first, AS_SITE) is DELIVERED
    (created,) = world.text.created
    assert created.name == WEBHOOK_NAME
    assert world.db.get_webhook(TEXT) == (created.id, created.token)
    (call,) = created.sends.calls
    assert call["username"] == "The Site"
    assert call["avatar_url"] == "https://e.com/a.png"
    assert call["content"] == "hello"
    assert call["wait"] is True
    assert call["allowed_mentions"].to_dict() == {"parse": []}
    assert "thread" not in call and "thread_name" not in call

    other = OutgoingMessage(content="again", username="Other")
    assert await world.deliverer.deliver(second, other) is DELIVERED
    assert len(world.text.created) == 1
    (reused,) = world.partials[created.id].sends.calls
    assert reused["username"] == "Other"
    assert "avatar_url" not in reused
    assert world.text.sends.calls == []


async def test_concurrent_feeds_create_one_webhook(world: World) -> None:
    feeds = [world.feed() for _ in range(4)]
    outcomes = await asyncio.gather(*(world.deliverer.deliver(f, AS_SITE) for f in feeds))
    assert outcomes == [DELIVERED] * 4
    assert len(world.text.created) == 1


@pytest.mark.parametrize("gone", [http_error(404, 10015), http_error(401, 50027)])
async def test_webhook_is_recreated_once_when_gone(world: World, gone: BaseException) -> None:
    world.db.set_webhook(TEXT, 77, "old")
    world.partials[77] = old = FakeWebhook(77, "old")
    old.sends.errors.append(gone)
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is DELIVERED
    (created,) = world.text.created
    assert world.db.get_webhook(TEXT) == (created.id, created.token)
    assert len(old.sends.calls) == 1 and len(created.sends.calls) == 1


async def test_webhook_gone_twice_is_a_retry(world: World) -> None:
    world.db.set_webhook(TEXT, 77, "old")
    world.partials[77] = old = FakeWebhook(77, "old")
    old.sends.errors.append(http_error(404, 10015))

    async def create_broken(*, name: str, reason: str | None = None) -> FakeWebhook:
        webhook = FakeWebhook(78, "new")
        webhook.sends.errors.append(http_error(404, 10015))
        world.text.created.append(webhook)
        return webhook

    world.text.create_webhook = create_broken  # type: ignore[method-assign]
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is RETRY
    assert len(world.text.created) == 1  # recreated once, not in a loop
    assert world.text.sends.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(400, 50035), REJECTED),
        (http_error(500), RETRY),
        (TimeoutError(), RETRY),
        (http_error(404, 10003), LOST),
        (http_error(403), RETRY),
        (ValueError("no token"), RETRY),
    ],
)
async def test_webhook_send_failures(
    world: World, error: BaseException, expected: DeliveryOutcome
) -> None:
    world.db.set_webhook(TEXT, 77, "tok")
    world.partials[77] = hook = FakeWebhook(77, "tok")
    hook.sends.errors.append(error)
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is expected
    assert world.db.get_webhook(TEXT) == (77, "tok")
    assert world.text.created == [] and world.text.sends.calls == []


async def test_without_manage_webhooks_falls_back_to_the_bot_and_notifies_once(
    world: World,
) -> None:
    world.text.create_errors = [http_error(403, 50013) for _ in range(3)]
    feed = world.feed()
    for _ in range(3):
        assert await world.deliverer.deliver(feed, AS_SITE) is DELIVERED
    assert len(world.text.sends.calls) == 3
    assert "username" not in world.text.sends.calls[0]
    assert world.db.get_webhook(TEXT) is None
    ((server_id, text),) = world.notifier.notes
    assert server_id == SERVER
    assert "Manage Webhooks" in text and f"<#{TEXT}>" in text

    # Another channel gets its own note.
    world.client.cached[101] = other = FakeText(101)
    other.create_errors = [http_error(403, 50013)]
    assert await world.deliverer.deliver(world.feed(101), AS_SITE) is DELIVERED
    assert len(world.notifier.notes) == 2


async def test_webhook_limit_falls_back_to_the_bot(world: World) -> None:
    world.text.create_errors = [http_error(400, 30007)]
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is DELIVERED
    assert len(world.text.sends.calls) == 1
    ((_, text),) = world.notifier.notes
    assert "limit" in text


async def test_fallback_works_without_a_notifier_or_with_a_failing_one(world: World) -> None:
    world.notifier.error = RuntimeError("notifier down")
    world.text.create_errors = [http_error(403, 50013)]
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is DELIVERED

    bare = DiscordDeliverer(world.client, world.db, world.fetcher)  # type: ignore[arg-type]
    world.text.create_errors = [http_error(403, 50013)]
    assert await bare.deliver(world.feed(), AS_SITE) is DELIVERED
    assert len(world.text.sends.calls) == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(500), RETRY),
        (TimeoutError(), RETRY),
        (http_error(404, 10003), LOST),
        (http_error(403, 50001), LOST),
    ],
)
async def test_webhook_create_failures(
    world: World, error: BaseException, expected: DeliveryOutcome
) -> None:
    world.text.create_errors = [error]
    assert await world.deliverer.deliver(world.feed(), AS_SITE) is expected
    assert world.text.sends.calls == [] and world.notifier.notes == []


async def test_blank_username_posts_as_the_bot(world: World) -> None:
    message = OutgoingMessage(content="x", username="  ")
    assert await world.deliverer.deliver(world.feed(), message) is DELIVERED
    assert len(world.text.sends.calls) == 1 and world.text.created == []


async def test_forbidden_words_are_taken_out_of_the_username(world: World) -> None:
    message = OutgoingMessage(content="x", username="Discord Blog")
    assert await world.deliverer.deliver(world.feed(), message) is DELIVERED
    (call,) = world.text.created[0].sends.calls
    assert "discord" not in call["username"].lower()


async def test_thread_uses_a_webhook_of_its_parent(world: World) -> None:
    assert await world.deliverer.deliver(world.feed(THREAD), AS_SITE) is DELIVERED
    (created,) = world.text.created  # made in the parent channel
    assert world.db.get_webhook(THREAD) == (created.id, created.token)
    (call,) = created.sends.calls
    assert call["thread"] is world.thread
    assert world.thread.sends.calls == []


async def test_thread_parent_is_fetched_when_not_cached(world: World) -> None:
    del world.client.cached[TEXT]
    world.client.fetchable[TEXT] = world.text
    assert await world.deliverer.deliver(world.feed(THREAD), AS_SITE) is DELIVERED
    assert world.client.fetched == [TEXT]
    del world.client.fetchable[TEXT]
    world.db.delete_webhook(THREAD)
    assert await world.deliverer.deliver(world.feed(THREAD), AS_SITE) is LOST


# -- forums --


async def test_forum_post_as_the_bot(world: World) -> None:
    message = OutgoingMessage(content="body", thread_title="  A title  ", tag_ids=(2, 99, 1))
    assert await world.deliverer.deliver(world.forum_feed(), message) is DELIVERED
    (call,) = world.forum.posts.calls
    assert call["name"] == "A title"
    assert call["content"] == "body"
    assert [tag.id for tag in call["applied_tags"]] == [1, 2]  # 99 no longer exists
    assert call["allowed_mentions"].to_dict() == {"parse": []}


async def test_forum_title_default_and_limit(world: World) -> None:
    feed = world.forum_feed()
    assert await world.deliverer.deliver(feed, MSG) is DELIVERED
    long = OutgoingMessage(content="x", thread_title="t" * 300)
    assert await world.deliverer.deliver(feed, long) is DELIVERED
    first, second = world.forum.posts.calls
    assert first["name"] == DEFAULT_THREAD_TITLE
    assert "applied_tags" not in first
    assert second["name"] == "t" * 100


async def test_forum_post_through_a_webhook(world: World) -> None:
    message = OutgoingMessage(content="body", username="Site", thread_title="T", tag_ids=(3,))
    assert await world.deliverer.deliver(world.forum_feed(), message) is DELIVERED
    (created,) = world.forum.created
    (call,) = created.sends.calls
    assert call["thread_name"] == "T"
    assert [tag.id for tag in call["applied_tags"]] == [3]
    assert call["username"] == "Site"
    assert "thread" not in call
    assert world.forum.posts.calls == []


async def test_forum_requiring_a_tag_is_checked_up_front(world: World) -> None:
    world.forum.require_tag = True
    feed = world.forum_feed()
    assert await world.deliverer.deliver(feed, MSG) is NEEDS_TAG
    stale = OutgoingMessage(content="x", tag_ids=(99,))
    assert await world.deliverer.deliver(feed, stale) is NEEDS_TAG
    assert world.forum.posts.calls == []
    tagged = OutgoingMessage(content="x", tag_ids=(1,))
    assert await world.deliverer.deliver(feed, tagged) is DELIVERED


async def test_forum_refusing_for_a_missing_tag(world: World) -> None:
    feed = world.forum_feed()
    world.forum.posts.errors.append(http_error(400, 40067))
    assert await world.deliverer.deliver(feed, MSG) is NEEDS_TAG
    world.db.set_webhook(FORUM, 77, "tok")
    world.partials[77] = hook = FakeWebhook(77, "tok")
    hook.sends.errors.append(http_error(400, 40067))
    assert await world.deliverer.deliver(feed, AS_SITE) is NEEDS_TAG


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (http_error(400, 50035), REJECTED),
        (http_error(403, 50013), LOST),
        (http_error(404, 10003), LOST),
        (http_error(503), RETRY),
        (OSError(), RETRY),
    ],
)
async def test_forum_post_failures(
    world: World, error: BaseException, expected: DeliveryOutcome
) -> None:
    world.forum.posts.errors.append(error)
    assert await world.deliverer.deliver(world.forum_feed(), MSG) is expected


# -- cover images --

COVER = "https://e.com/cover.png"
WITH_COVER = OutgoingMessage(
    content="body", embed=EmbedSpec(title="T"), thread_title="T", cover_image_url=COVER
)


async def test_cover_image_is_attached(world: World) -> None:
    assert await world.deliverer.deliver(world.forum_feed(), WITH_COVER) is DELIVERED
    (call,) = world.forum.posts.calls
    assert call["file_bytes"] == b"IMG:" + COVER.encode()
    assert call["filename"] == "cover.png"
    assert call["file"].fp.closed  # nothing keeps the bytes afterwards
    assert call["embed"].image.url is None
    assert world.fetcher.urls == [COVER]


async def test_cover_image_through_a_webhook(world: World) -> None:
    message = OutgoingMessage(content="b", username="Site", cover_image_url=COVER)
    assert await world.deliverer.deliver(world.forum_feed(), message) is DELIVERED
    (call,) = world.forum.created[0].sends.calls
    assert call["file_bytes"].startswith(b"IMG:")
    assert call["file"].fp.closed


async def test_cover_gets_a_new_file_when_the_webhook_is_recreated(world: World) -> None:
    world.db.set_webhook(FORUM, 77, "old")
    world.partials[77] = old = FakeWebhook(77, "old")
    old.sends.errors.append(http_error(404, 10015))
    message = OutgoingMessage(content="b", username="Site", cover_image_url=COVER)
    assert await world.deliverer.deliver(world.forum_feed(), message) is DELIVERED
    (call,) = world.forum.created[0].sends.calls
    assert call["file_bytes"] == old.sends.calls[0]["file_bytes"]
    assert call["file"] is not old.sends.calls[0]["file"]
    assert world.fetcher.urls == [COVER]  # downloaded once


@pytest.mark.parametrize("error", [FetchError("too large"), TimeoutError(), RuntimeError("x")])
async def test_cover_download_failure_posts_without_it(world: World, error: BaseException) -> None:
    world.fetcher.error = error
    assert await world.deliverer.deliver(world.forum_feed(), WITH_COVER) is DELIVERED
    (call,) = world.forum.posts.calls
    assert "file" not in call
    assert call["embed"].image.url == COVER  # shown in the Embed instead


async def test_cover_download_failure_keeps_the_embeds_own_image(world: World) -> None:
    world.fetcher.error = FetchError("nope")
    message = OutgoingMessage(
        content="b", embed=EmbedSpec(image="https://e.com/own.png"), cover_image_url=COVER
    )
    assert await world.deliverer.deliver(world.forum_feed(), message) is DELIVERED
    assert world.forum.posts.calls[0]["embed"].image.url == "https://e.com/own.png"

    world.fetcher.error = FetchError("nope")
    plain = OutgoingMessage(content="b", cover_image_url=COVER)
    assert await world.deliverer.deliver(world.forum_feed(), plain) is DELIVERED
    assert "embed" not in world.forum.posts.calls[1]


@pytest.mark.parametrize("error", [http_error(413, 40005), http_error(403, 50013)])
async def test_refused_cover_is_retried_without_it(world: World, error: BaseException) -> None:
    world.forum.posts.errors.append(error)
    assert await world.deliverer.deliver(world.forum_feed(), WITH_COVER) is DELIVERED
    with_file, without = world.forum.posts.calls
    assert "file" in with_file and "file" not in without
    assert without["embed"].image.url == COVER


async def test_cover_failure_that_is_not_about_the_file_is_not_resent(world: World) -> None:
    world.forum.posts.errors.append(http_error(500))
    assert await world.deliverer.deliver(world.forum_feed(), WITH_COVER) is RETRY
    assert len(world.forum.posts.calls) == 1


async def test_every_cover_is_downloaded_and_sent(world: World) -> None:
    original = world.forum.create_thread

    async def create_thread(**kwargs: Any) -> None:
        await asyncio.sleep(0)
        await original(**kwargs)
        world.fetcher.events.append("sent")

    world.forum.create_thread = create_thread  # type: ignore[method-assign]
    feeds = [world.forum_feed() for _ in range(5)]
    outcomes = await asyncio.gather(*(world.deliverer.deliver(f, WITH_COVER) for f in feeds))
    assert outcomes == [DELIVERED] * 5
    # Downloads are serialised; sends are deliberately not held behind them, so a send that
    # hangs for one Feed cannot block another Feed's Cover image.
    assert sorted(world.fetcher.events) == ["download"] * 5 + ["sent"] * 5


# -- never raising --


class Boom(Exception):
    pass


async def test_deliver_never_raises(world: World) -> None:
    deliver = world.deliverer.deliver
    cover_site = OutgoingMessage(content="b", username="Site", cover_image_url=COVER)

    world.client.get_error = Boom()
    assert await deliver(world.feed(), MSG) is RETRY
    world.client.get_error = None

    world.text.sends.errors.append(Boom())
    assert await deliver(world.feed(), MSG) is RETRY

    world.text.create_errors = [Boom()]
    assert await deliver(world.feed(), AS_SITE) is RETRY

    world.forum.posts.errors.append(Boom())
    assert await deliver(world.forum_feed(), MSG) is RETRY

    world.forum.create_errors = [Boom()]
    assert await deliver(world.forum_feed(), cover_site) is RETRY

    world.fetcher.error = Boom()
    assert await deliver(world.forum_feed(), cover_site) is DELIVERED
    world.fetcher.error = None

    world.db.set_webhook(FORUM, 1, "t")
    world.partials[1] = hook = FakeWebhook(1, "t")
    hook.sends.errors.append(Boom())
    assert await deliver(world.forum_feed(), cover_site) is RETRY

    world.thread.archived = True
    world.thread.edit_error = Boom()
    world.thread.sends.errors.append(Boom())
    assert await deliver(world.feed(THREAD), MSG) is RETRY

    # A message that cannot even be built can never be sent.
    bad = OutgoingMessage(content="x", embed=EmbedSpec(title="T", colour="red"))  # type: ignore[arg-type]
    assert await deliver(world.feed(), bad) is REJECTED


async def test_deliver_survives_a_broken_database_and_forum(world: World) -> None:
    feed, forum_feed = world.feed(), world.forum_feed()

    class BrokenForum(FakeForum):
        @property
        def available_tags(self) -> Any:
            raise Boom()

    world.client.cached[FORUM] = BrokenForum()
    assert await world.deliverer.deliver(forum_feed, MSG) is RETRY

    world.db.close()  # every store call now raises
    assert await world.deliverer.deliver(feed, AS_SITE) is RETRY
    assert await world.deliverer.deliver(feed, MSG) is DELIVERED
    await world.deliverer.cleanup_webhook(TEXT)


async def test_cancellation_is_not_swallowed(world: World) -> None:
    world.text.sends.errors.append(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await world.deliverer.deliver(world.feed(), MSG)


# -- cleanup --


async def test_cleanup_webhook_deletes_it_everywhere(world: World) -> None:
    world.db.set_webhook(TEXT, 77, "tok")
    world.partials[77] = hook = FakeWebhook(77, "tok")
    await world.deliverer.cleanup_webhook(TEXT)
    assert hook.deleted
    assert world.db.get_webhook(TEXT) is None
    await world.deliverer.cleanup_webhook(TEXT)  # nothing stored: nothing to do
    assert list(world.partials) == [77]


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (http_error(404, 10015), False),
        (http_error(401, 50027), False),
        (http_error(500), True),
        (Boom(), True),
    ],
)
async def test_cleanup_webhook_never_raises(world: World, error: BaseException, kept: bool) -> None:
    world.db.set_webhook(TEXT, 77, "tok")
    world.partials[77] = hook = FakeWebhook(77, "tok")
    hook.delete_error = error
    await world.deliverer.cleanup_webhook(TEXT)
    assert (world.db.get_webhook(TEXT) is not None) is kept


# -- the notifier --


class FakeLogs(FakeText):
    """A Logs channel in a Server, where the bot may or may not embed."""

    def __init__(self, id: int = LOGS, *, server_id: int = SERVER, embed_links: bool = True):
        super().__init__(id)
        self.guild = SimpleNamespace(id=server_id, me=SimpleNamespace(id=9))
        self.embed_links = embed_links
        self.asked: list[Any] = []

    def permissions_for(self, member: Any) -> discord.Permissions:
        self.asked.append(member)
        return discord.Permissions(send_messages=True, embed_links=self.embed_links)


ALEX = Actor(987, "alex", "https://cdn.example/alex.png")
NOBODY = {"parse": []}  # allowed mentions that ping nobody


def log_entry(kind: LogKind = LogKind.FEED_PAUSED, **overrides: Any) -> LogEntry:
    args: dict[str, Any] = {
        "id": 1,
        "server_id": SERVER,
        "at": 1_760_000_000,
        "actor_id": 987,
        "actor_name": "alex",
        "kind": kind,
        "feed_id": 45,
        "feed_name": "BBC News",
        "channel_id": TEXT,
        "feed_url": "https://example.com/rss",
    }
    args.update(overrides)
    return LogEntry(**args)


@pytest.fixture
def logs(world: World) -> FakeLogs:
    world.client.cached[LOGS] = channel = FakeLogs()
    world.client.user = SimpleNamespace(  # type: ignore[attr-defined]
        display_name="RSS Bot", display_avatar=SimpleNamespace(url="https://cdn.example/bot.png")
    )
    world.db.set_logs_channel(SERVER, LOGS)
    return channel


@pytest.fixture
def notifier(world: World) -> DiscordNotifier:
    world.client.cached.setdefault(LOGS, FakeText(LOGS))
    return DiscordNotifier(world.client, world.db)  # type: ignore[arg-type]


async def test_notifier_posts_a_note_as_an_amber_embed_from_the_bot(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    await notifier.notify(SERVER, "@everyone <@1> <@&2> " + "x" * 5000)
    (call,) = logs.sends.calls
    (embed,) = call["embeds"]
    assert "content" not in call
    assert embed.description.startswith("@everyone <@1> <@&2> xxx") and len(embed) <= 6000
    assert embed.colour.value == logembed.AMBER
    assert (embed.author.name, embed.author.icon_url) == ("RSS Bot", "https://cdn.example/bot.png")
    assert call["allowed_mentions"].to_dict() == NOBODY


async def test_notifier_posts_a_note_as_text_where_the_bot_may_not_embed(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    logs.embed_links = False
    await notifier.notify(SERVER, "@everyone <@1> <@&2> " + "x" * 3000)
    (call,) = logs.sends.calls
    assert len(call["content"]) == 2000 and "embeds" not in call
    assert call["allowed_mentions"].to_dict() == NOBODY


async def test_the_post_as_warning_reaches_the_logs_channel_as_an_embed(
    world: World, logs: FakeLogs
) -> None:
    deliverer = DiscordDeliverer(
        world.client,  # type: ignore[arg-type]
        world.db,
        world.fetcher,  # type: ignore[arg-type]
        DiscordNotifier(world.client, world.db),  # type: ignore[arg-type]
    )
    world.text.create_errors = [http_error(403, 50013)]
    assert await deliverer.deliver(world.feed(), AS_SITE) is DELIVERED
    (call,) = logs.sends.calls
    (embed,) = call["embeds"]
    assert "Manage Webhooks" in embed.description and f"<#{TEXT}>" in embed.description
    assert embed.colour.value == logembed.AMBER and embed.author.name == "RSS Bot"


async def test_notifier_without_a_logs_channel(world: World, notifier: DiscordNotifier) -> None:
    await notifier.notify(SERVER, "unknown server")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    world.db.ensure_server(SERVER)
    await notifier.notify(SERVER, "no logs channel")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    assert world.client.cached[LOGS].sends.calls == []
    assert world.client.fetched == []


async def test_notifier_with_a_missing_or_unusable_channel(
    world: World, notifier: DiscordNotifier
) -> None:
    world.db.set_logs_channel(SERVER, 999)
    await notifier.notify(SERVER, "gone")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    assert world.client.fetched == [999, 999]
    world.client.fetch_error = Boom()
    await notifier.notify(SERVER, "fetch blew up")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    world.db.set_logs_channel(SERVER, FORUM)  # not a channel that takes messages
    await notifier.notify(SERVER, "forum")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    assert world.forum.posts.calls == []


async def test_notifier_never_posts_in_another_servers_channel(
    world: World, notifier: DiscordNotifier
) -> None:
    world.client.cached[LOGS] = elsewhere = FakeLogs(server_id=SERVER + 1)
    world.db.set_logs_channel(SERVER, LOGS)
    await notifier.notify(SERVER, "note")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    assert elsewhere.sends.calls == []


async def test_notifier_with_a_failing_send(world: World, notifier: DiscordNotifier) -> None:
    world.db.set_logs_channel(SERVER, LOGS)
    logs = world.client.cached[LOGS]
    logs.sends.errors.extend([http_error(500), Boom(), http_error(500), Boom()])
    await notifier.notify(SERVER, "one")
    await notifier.notify(SERVER, "two")
    await notifier.announce(SERVER, [log_entry()], ALEX)
    await notifier.announce(SERVER, [log_entry()], ALEX)
    await notifier.notify(SERVER, "three")
    assert len(logs.sends.calls) == 5

    world.db.close()
    await notifier.notify(SERVER, "store is broken")
    await notifier.announce(SERVER, [log_entry()], ALEX)


async def test_announce_posts_a_member_action_as_an_embed(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    await notifier.announce(SERVER, [log_entry()], ALEX)
    (call,) = logs.sends.calls
    (embed,) = call["embeds"]
    assert "content" not in call
    assert embed.title == "Feed paused"
    assert (embed.author.name, embed.author.icon_url) == ("alex", ALEX.avatar_url)
    assert embed.description.splitlines() == [
        f"**BBC News** in <#{TEXT}>",
        "<https://example.com/rss>",
        "By <@987> `987`",
    ]
    assert call["allowed_mentions"].to_dict() == NOBODY
    assert logs.asked == [logs.guild.me]  # the bot's own permissions were looked at


async def test_announce_posts_the_bots_reports_under_the_bots_name(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    report = log_entry(LogKind.FEED_BROKEN, actor_id=None, actor_name="", detail="The site: 500")
    await notifier.announce(SERVER, [report], Actor.bot())
    (embed,) = logs.sends.calls[0]["embeds"]
    assert (embed.author.name, embed.author.icon_url) == ("RSS Bot", "https://cdn.example/bot.png")
    assert embed.title == "Broken feed" and "<@" not in embed.description


async def test_announce_before_the_bot_knows_its_own_name(
    world: World, logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    world.client.user = None  # type: ignore[attr-defined]
    await notifier.announce(SERVER, [log_entry(actor_id=None)], Actor.bot())
    (embed,) = logs.sends.calls[0]["embeds"]
    assert embed.author.name is None and embed.title == "Feed paused"


async def test_announce_falls_back_to_text_without_embed_links(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    logs.embed_links = False
    await notifier.announce(SERVER, [log_entry()], ALEX)
    (call,) = logs.sends.calls
    assert "embeds" not in call
    assert call["content"].splitlines() == [
        "**Feed paused** by **alex** (<@987> `987`) <t:1760000000:f>",
        f"**BBC News** in <#{TEXT}>",
        "<https://example.com/rss>",
    ]
    assert call["allowed_mentions"].to_dict() == NOBODY


async def test_announce_falls_back_to_text_when_discord_forbids_the_embed(
    world: World, notifier: DiscordNotifier
) -> None:
    # A channel whose permissions cannot be worked out: the send itself is the test.
    world.db.set_logs_channel(SERVER, LOGS)
    logs = world.client.cached[LOGS]
    logs.sends.errors.append(http_error(403, 50013))
    await notifier.announce(SERVER, [log_entry()], ALEX)
    embedded, plain = logs.sends.calls
    assert "embeds" in embedded and "content" not in embedded
    assert plain["content"].startswith("**Feed paused** by **alex**") and "embeds" not in plain
    assert plain["allowed_mentions"].to_dict() == NOBODY

    logs.sends.errors.extend([http_error(403, 50013), http_error(403, 50013)])
    await notifier.announce(SERVER, [log_entry()], ALEX)  # text refused too: given up, no raise
    assert len(logs.sends.calls) == 4


async def test_announce_sends_a_long_import_as_several_messages(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    entries = [
        log_entry(LogKind.FEED_ADDED, id=n, feed_id=n, feed_name=f"{n:03d} " + "x" * 96)
        for n in range(100)
    ]
    await notifier.announce(SERVER, entries, ALEX)
    assert len(logs.sends.calls) > 1
    listed: list[str] = []
    for call in logs.sends.calls:
        assert len(call["embeds"]) <= 10 and sum(len(e) for e in call["embeds"]) <= 6000
        assert call["allowed_mentions"].to_dict() == NOBODY
        listed += [line for e in call["embeds"] for line in e.description.splitlines()]
    assert [line[4:7] for line in listed[:-1]] == [f"{n:03d}" for n in range(100)]

    logs.sends.calls.clear()
    logs.embed_links = False
    await notifier.announce(SERVER, entries, ALEX)
    assert len(logs.sends.calls) > 1
    assert all(len(call["content"]) <= 2000 for call in logs.sends.calls)
    text = "\n".join(call["content"] for call in logs.sends.calls)
    assert all(f"**{n:03d} " in text for n in range(100))


async def test_a_report_cut_off_part_way_is_not_said_again_as_text(
    logs: FakeLogs, notifier: DiscordNotifier
) -> None:
    entries = [
        log_entry(LogKind.FEED_ADDED, id=n, feed_id=n, feed_name="x" * 100) for n in range(100)
    ]

    class SecondFails(Calls):
        def record(self, kwargs: dict[str, Any]) -> None:
            super().record(kwargs)
            if len(self.calls) == 2:
                raise http_error(403, 50013)

    logs.sends = SecondFails()
    await notifier.announce(SERVER, entries, ALEX)
    assert len(logs.sends.calls) == 2 and all("embeds" in call for call in logs.sends.calls)


async def test_announce_with_nothing_to_say(logs: FakeLogs, notifier: DiscordNotifier) -> None:
    await notifier.announce(SERVER, [], ALEX)
    assert logs.sends.calls == []
