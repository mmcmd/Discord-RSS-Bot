from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import MEMBER, OWNER, SERVER, USER, FakeChannel, FakeInteraction, component_ids

import rssbot.commands as commands
from rssbot.commands import _ui as ui
from rssbot.commands import feed
from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import (
    Actor,
    Change,
    ChannelKind,
    Feed,
    Item,
    Level,
    LogEntry,
    LogKind,
    OutgoingMessage,
    ParsedFeed,
    PauseReason,
    PostAs,
    TargetKind,
)
from rssbot.opml import OpmlEntry, build_opml, parse_opml
from rssbot.ports import DeliveryOutcome, FetchError, FetchResult, ImageData
from rssbot.render import render_default, render_item
from rssbot.scheduler import Scheduler
from rssbot.service import FeedService, ImportFailure, OpmlImport, ServiceError

OTHER_SERVER = 200
STRANGER = 3
ROLE = 50
TEXT = 70
OTHER = 71
FORUM = 72
VOICE = 73
THREAD = 74
LOCKED = 75
URL = "https://example.com/feed.xml"
URL2 = "https://other.example/rss"
URL3 = "https://example.com/third"
URL4 = "https://example.com/fourth"
MISSING = "https://gone.example/feed"

COMPONENT = discord.InteractionType.component
MODAL = discord.InteractionType.modal_submit


def channels() -> tuple[FakeChannel, ...]:
    forum = FakeChannel(FORUM, "articles", discord.ChannelType.forum)
    forum.available_tags = [  # type: ignore[attr-defined]
        SimpleNamespace(id=901, name="News"),
        SimpleNamespace(id=902, name="Tech"),
    ]
    return (
        FakeChannel(TEXT, "news"),
        FakeChannel(OTHER, "more-news", discord.ChannelType.news),
        forum,
        FakeChannel(VOICE, "voice", discord.ChannelType.voice),
        FakeChannel(THREAD, "a-thread", discord.ChannelType.public_thread),
    )


def locked_channels() -> tuple[FakeChannel, ...]:
    """The usual channels, plus one the bot cannot see or post in."""
    return (*channels(), FakeChannel(LOCKED, "locked", bot_can_post=False))


def warning(channel_id: int = LOCKED) -> str:
    return (
        f"**Warning**: the bot cannot see or post in <#{channel_id}>. "
        "Nothing will be posted until the bot is given access to it."
    )


# -- fakes --


class Clock:
    t = 1_700_000_000

    def now(self) -> int:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += int(seconds)


def item(key: str) -> Item:
    return Item(
        key=key,
        title=f"Title {key}",
        link=f"https://example.com/{key}",
        summary=f"Summary {key}",
        content="",
        author="",
        published=None,
        categories=(),
        image="",
    )


class Web:
    """What every address serves. Stands in for the fetcher and the parser."""

    def __init__(self) -> None:
        self.listings: dict[str, ParsedFeed] = {}
        self.errors: dict[str, FetchError] = {}

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        if url in self.errors:
            raise self.errors[url]
        if url not in self.listings:
            raise FetchError("The address answered with error 404.")
        return FetchResult(False, url.encode(), None, None, url)

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        raise FetchError("no images here")

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        return self.listings[body.decode()]


class Posts:
    """The fake deliverer."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, OutgoingMessage]] = []
        self.cleaned: list[int] = []
        self.outcome = DeliveryOutcome.DELIVERED

    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        self.sent.append((feed.id, message))
        return self.outcome

    async def cleanup_webhook(self, channel_id: int) -> None:
        self.cleaned.append(channel_id)


class Notes:
    async def notify(self, server_id: int, text: str) -> None:
        pass

    async def announce(self, server_id: int, entries: object, actor: object) -> None:
        pass


@dataclass
class Env:
    db: Database
    web: Web
    service: FeedService
    posts: Posts
    scheduler: Scheduler

    def interaction(self, *, message: bool | None = None, **kwargs: Any) -> FakeInteraction:
        """An interaction by a Manager; one from a component sits on a message."""
        kwargs.setdefault("channels", channels())
        interaction = FakeInteraction(self.db, **kwargs)
        interaction.client.deps.service = self.service
        interaction.client.deps.deliverer = self.posts
        interaction.client.deps.scheduler = self.scheduler
        if message if message is not None else kwargs.get("type") is COMPONENT:
            interaction.message = object()  # type: ignore[attr-defined]
        return interaction

    def click(self, **kwargs: Any) -> FakeInteraction:
        return self.interaction(type=COMPONENT, **kwargs)

    async def add(
        self,
        url: str = URL,
        channel_id: int = TEXT,
        kind: ChannelKind = ChannelKind.MESSAGES,
        server_id: int = SERVER,
        **kwargs: Any,
    ) -> Feed:
        added, _ = await self.service.add_feed(
            server_id, channel_id, kind, url, actor=MEMBER, **kwargs
        )
        return added

    def feed(self, feed_id: int) -> Feed:
        found = self.db.get_feed(feed_id)
        assert found is not None
        return found


@pytest.fixture
def env() -> Env:
    db = Database(":memory:")
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    web = Web()
    web.listings[URL] = ParsedFeed("Example News", "", "", tuple(item(k) for k in "abc"))
    web.listings[URL2] = ParsedFeed("Other Site", "", "", (item("x"),))
    clock, posts = Clock(), Posts()
    journal = Journal(db, clock, Notes())
    scheduler = Scheduler(db, web, posts, journal, clock, web.parse, render_item, render_default)
    return Env(db, web, FeedService(db, web, clock, journal, parse=web.parse), posts, scheduler)


def submission(
    custom_id: str,
    *,
    texts: dict[str, str] | None = None,
    choices: dict[str, str] | None = None,
    channel: tuple[int, discord.ChannelType] | None = None,
) -> dict[str, Any]:
    components: list[dict[str, Any]] = [
        {"type": 18, "component": {"type": 4, "custom_id": key, "value": value}}
        for key, value in (texts or {}).items()
    ]
    components += [
        {"type": 18, "component": {"type": 3, "custom_id": key, "values": [value]}}
        for key, value in (choices or {}).items()
    ]
    resolved: dict[str, Any] = {}
    if channel is not None:
        channel_id, kind = channel
        components.append(
            {"type": 18, "component": {"type": 8, "custom_id": "channel", "values": [channel_id]}}
        )
        resolved["channels"] = {
            str(channel_id): {"id": str(channel_id), "type": kind.value, "name": "picked"}
        }
    return {"custom_id": custom_id, "components": components, "resolved": resolved}


async def submit(env: Env, data: dict[str, Any], **kwargs: Any) -> FakeInteraction:
    interaction = env.interaction(type=MODAL, data=data, **kwargs)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    return interaction


def add_form(url: str = URL, channel_id: int = TEXT, **choices: str) -> dict[str, Any]:
    kind = {FORUM: discord.ChannelType.forum, VOICE: discord.ChannelType.voice}.get(
        channel_id, discord.ChannelType.text
    )
    picked = {"interval": "600", "post_as": "bot", **choices}
    return submission(
        "rss:m:feed_add", texts={"url": url}, choices=picked, channel=(channel_id, kind)
    )


def edit_form(found: Feed, channel_id: int, **changes: str) -> dict[str, Any]:
    kind = discord.ChannelType.forum if channel_id == FORUM else discord.ChannelType.text
    texts = {"name": changes.get("name", found.name), "url": changes.get("url", found.url)}
    interval = changes.get("interval", str(found.interval_s))
    return submission(
        f"rss:m:feed_edit:{found.id}",
        texts=texts,
        choices={"interval": interval},
        channel=(channel_id, kind),
    )


async def pick(select: ui.ActionSelect, interaction: FakeInteraction, values: list[Any]) -> None:
    select.item._values = values  # type: ignore[attr-defined]
    await select.callback(interaction)  # type: ignore[arg-type]


def rows(view: discord.ui.View) -> list[list[str]]:
    """The view's rows as labels (or placeholders), checked against Discord's limits."""
    payload = view.to_components()
    assert len(payload) <= 5
    for row in payload:
        assert 1 <= len(row["components"]) <= 5
    return [[c.get("label") or c.get("placeholder") for c in row["components"]] for row in payload]


def names(interaction: FakeInteraction) -> list[str]:
    return [name for name, _ in interaction.calls]


def form_fields(interaction: FakeInteraction) -> dict[str, dict[str, Any]]:
    name, sent = interaction.last
    assert name == "send_modal"
    payload = sent["modal"].to_dict()
    return {c["component"]["custom_id"]: c["component"] for c in payload["components"]}


# -- The command tree and names --


def _walk(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = [payload]
    for option in payload.get("options", []):
        found.extend(_walk(option))
    return found


def test_tree_serialises_within_discords_limits() -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = app_commands.CommandTree(client)
    feed.register(tree)

    (group,) = tree.get_commands()
    payload = group.to_dict(tree)
    assert payload["name"] == "feed"
    assert payload["contexts"] == [0]  # Servers only
    assert payload.get("default_member_permissions") is None
    assert [option["name"] for option in payload["options"]] == [
        "add",
        "list",
        "history",
        "edit",
        "remove",
        "pause",
        "resume",
        "refresh",
        "test",
        "import",
        "export",
    ]
    for node in _walk(payload):
        assert 1 <= len(node["name"]) <= 32
        assert 1 <= len(node["description"]) <= 100
        assert len(node.get("options", [])) <= 25
        assert len(node.get("choices", [])) <= 25
    by_name = {option["name"]: option for option in payload["options"]}
    assert by_name["add"].get("options", []) == []
    for name in ("history", "edit", "remove", "pause", "resume", "test"):
        (option,) = by_name[name]["options"]
        assert (option["name"], option["type"], option["autocomplete"]) == ("feed", 3, True)
        assert option["required"] is True
    (option,) = by_name["refresh"]["options"]
    assert (option["name"], option["type"], option["autocomplete"]) == ("feed", 3, True)
    assert option.get("required", False) is False
    file, channel = by_name["import"]["options"]
    assert (file["name"], file["type"], file["required"]) == ("file", 11, True)
    assert (channel["name"], channel["type"], channel["required"]) == ("channel", 7, False)
    assert sorted(channel["channel_types"]) == [0, 5, 10, 11, 12, 15]


def test_every_action_and_form_name_starts_with_feed() -> None:
    actions = [n for n, cls in ui._ACTIONS.items() if cls.__module__ == feed.__name__]
    forms = [n for n, form in ui._FORMS.items() if form.handler.__module__ == feed.__name__]
    assert len(actions) >= 20 and len(forms) == 4
    assert all(name.startswith("feed_") for name in actions + forms)


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (600, "every 10 minutes"),
        (3600, "every hour"),
        (21600, "every 6 hours"),
        (60, "every minute"),
        (5400, "every 90 minutes"),
    ],
)
def test_interval_words(seconds: int, words: str) -> None:
    assert feed.interval_words(seconds) == words


# -- Access --


async def test_commands_are_refused_for_a_non_manager(env: Env) -> None:
    found = await env.add()
    interaction = env.interaction(user_id=STRANGER)
    with pytest.raises(ui.UserError, match="Only Managers"):
        await feed.add_command.callback(interaction)  # type: ignore[arg-type]
    with pytest.raises(ui.UserError, match="Only Managers"):
        await feed.list_command.callback(interaction)  # type: ignore[arg-type]
    with pytest.raises(ui.UserError, match="Only Managers"):
        await feed.pause_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    with pytest.raises(ui.UserError, match="Only Managers"):
        await feed.export_command.callback(interaction)  # type: ignore[arg-type]
    assert interaction.calls == []
    assert env.feed(found.id).paused is None


async def test_a_button_is_refused_for_a_non_manager(env: Env) -> None:
    found = await env.add()
    interaction = env.click(user_id=STRANGER)
    await feed.PauseFeed(found.id).callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == ui.NEED_MANAGER
    assert env.feed(found.id).paused is None


async def test_a_form_is_refused_for_a_non_manager(env: Env) -> None:
    interaction = await submit(env, add_form(), user_id=STRANGER)
    assert interaction.text == ui.NEED_MANAGER
    assert env.db.list_feeds(SERVER) == []


async def test_another_servers_feed_is_refused_on_a_button(env: Env) -> None:
    foreign = await env.add(server_id=OTHER_SERVER)
    for button in (feed.PauseFeed, feed.RemoveFeed, feed.BackToPanel, feed.PostPreview):
        interaction = env.click()
        await button(foreign.id).callback(interaction)  # type: ignore[arg-type]
        assert interaction.text == ui.FEED_GONE
    assert env.feed(foreign.id).paused is None
    assert env.posts.sent == []

    interaction = await submit(env, edit_form(foreign, OTHER, name="Mine now"))
    assert interaction.text == ui.FEED_GONE
    assert env.feed(foreign.id).name == "Example News"


# -- /feed add --


async def test_add_opens_a_form_preset_to_the_current_channel(env: Env) -> None:
    interaction = env.interaction()
    interaction.channel = FakeChannel(THREAD, "a-thread", discord.ChannelType.public_thread)  # type: ignore[attr-defined]
    await feed.add_command.callback(interaction)  # type: ignore[arg-type]

    fields = form_fields(interaction)
    assert list(fields) == ["url", "channel", "interval", "post_as"]
    assert fields["channel"]["default_values"] == [{"id": THREAD, "type": "channel"}]
    assert sorted(fields["channel"]["channel_types"]) == [0, 5, 10, 11, 12, 15]
    assert [o["label"] for o in fields["interval"]["options"]] == [
        "5 minutes",
        "10 minutes",
        "15 minutes",
        "30 minutes",
        "1 hour",
        "3 hours",
        "6 hours",
        "12 hours",
        "24 hours",
    ]
    assert [o["value"] for o in fields["interval"]["options"] if o["default"]] == ["600"]
    assert [(o["value"], o["default"]) for o in fields["post_as"]["options"]] == [
        ("bot", True),
        ("site", False),
        ("custom", False),
    ]


async def test_add_does_not_preset_a_channel_that_cannot_hold_a_feed(env: Env) -> None:
    interaction = env.interaction()
    interaction.channel = FakeChannel(VOICE, "voice", discord.ChannelType.voice)  # type: ignore[attr-defined]
    await feed.add_command.callback(interaction)  # type: ignore[arg-type]
    assert not form_fields(interaction)["channel"].get("default_values")


async def test_add_creates_the_feed_and_shows_its_panel(env: Env) -> None:
    interaction = await submit(env, add_form(interval="3600"))

    (added,) = env.db.list_feeds(SERVER)
    assert (added.channel_id, added.channel_kind) == (TEXT, ChannelKind.MESSAGES)
    assert (added.interval_s, added.post_as) == (3600, PostAs.BOT)
    assert names(interaction) == ["defer", "followup"]
    assert interaction.calls[0][1] == {"ephemeral": True, "thinking": True}
    sent = interaction.last[1]
    assert sent["ephemeral"] is True and sent["allowed_mentions"] is ui.NO_MENTIONS
    assert f"Added the Feed **Example News** in <#{TEXT}>" in sent["content"]
    assert "lists 3 Items now. None of them will be posted" in sent["content"]
    assert "**Check interval**: every hour" in sent["content"]
    assert "custom name" not in sent["content"]
    assert f"rss:c:feed_settings:{added.id}" in component_ids(sent["view"])


async def test_add_as_custom_starts_as_the_bot_and_points_to_post_as(env: Env) -> None:
    interaction = await submit(env, add_form(post_as="custom"))
    (added,) = env.db.list_feeds(SERVER)
    assert added.post_as is PostAs.BOT
    assert "Press **Post as** below to enter the custom name and picture" in interaction.text


async def test_add_as_the_site_and_in_a_forum(env: Env) -> None:
    await submit(env, add_form(channel_id=FORUM, post_as="site"))
    (added,) = env.db.list_feeds(SERVER)
    assert (added.channel_kind, added.post_as) == (ChannelKind.FORUM, PostAs.SITE)
    assert added.site_name == "Example News"


async def test_add_refuses_a_channel_that_cannot_hold_a_feed(env: Env) -> None:
    interaction = await submit(env, add_form(channel_id=VOICE))
    assert interaction.text == feed.WRONG_CHANNEL
    assert names(interaction) == ["send_message"]  # refused before any fetching
    assert env.db.list_feeds(SERVER) == []


async def test_add_with_a_failing_address_shows_the_services_sentence(env: Env) -> None:
    interaction = await submit(env, add_form(url=MISSING))
    assert names(interaction) == ["defer", "followup"]
    assert interaction.text == f"The address answered with error 404. (`{MISSING}`)"
    assert env.db.list_feeds(SERVER) == []


async def test_add_that_fails_shows_the_address_cut_and_as_plain_text(env: Env) -> None:
    typed = "https://gone.example/`<@&50>`" + "a" * 1000
    interaction = await submit(env, add_form(url=typed))
    assert env.db.list_feeds(SERVER) == []
    sentence, _, address = interaction.text.partition(" (`")
    assert sentence.endswith(".")
    assert address.startswith("https://gone.example/<@&50>aaa") and address.endswith("…`)")
    assert interaction.text.count("`") == 2  # the address cannot break out of its code span
    assert len(address) <= 205


async def test_add_in_a_channel_the_bot_cannot_post_in_is_accepted_with_a_warning(
    env: Env,
) -> None:
    interaction = await submit(env, add_form(channel_id=LOCKED), channels=locked_channels())
    (added,) = env.db.list_feeds(SERVER)
    assert added.channel_id == LOCKED
    assert f"Added the Feed **Example News** in <#{LOCKED}>" in interaction.text
    assert interaction.text.count(warning()) == 1
    assert interaction.text.count("**Warning**") == 1


# -- /feed list --


async def test_list_points_to_add_when_there_are_no_feeds(env: Env) -> None:
    await env.add(server_id=OTHER_SERVER)
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == feed.NO_FEEDS
    assert "/feed add" in interaction.text
    assert "view" not in interaction.last[1]


async def test_list_of_one_page_has_no_page_buttons(env: Env) -> None:
    found = await env.add()
    await env.service.pause_feed(SERVER, found.id, actor=MEMBER)
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert sent["content"].splitlines() == [
        "**Feeds in this Server**: 1 (1 paused)",
        "",
        f"**Example News** in <#{TEXT}>: Paused by <@{USER}> · <t:{Clock.t}:R>",
        f"-# last worked <t:{found.last_success_at}:R>",
    ]
    assert component_ids(sent["view"]) == ["rss:c:feed_list_open"]
    (option,) = sent["view"].to_components()[0]["components"][0]["options"]
    assert (option["label"], option["value"]) == ("Example News", str(found.id))
    assert option["description"] == "#news"


async def test_list_menu_tells_same_named_feeds_apart(env: Env) -> None:
    long = "https://example.com/" + "p" * 120
    urls = (URL, URL2, long + "/1", long + "/2")
    for url in urls:
        env.web.listings[url] = ParsedFeed("n" * 100, "", "", ())
    twins = [await env.add(url=url, name="n" * 100) for url in urls]
    elsewhere = await env.add(url=URL, channel_id=OTHER, name="n" * 100)
    env.web.listings["https://solo.example/feed"] = ParsedFeed("Solo", "", "", ())
    solo = await env.add(url="https://solo.example/feed")
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    options = interaction.last[1]["view"].to_components()[0]["components"][0]["options"]
    shown = {int(o["value"]): (o["label"], o.get("description")) for o in options}
    name = "n" * 100
    assert shown == {
        twins[0].id: (name, "#news · example.com/feed.xml"),
        twins[1].id: (name, "#news · other.example"),
        twins[2].id: (name, f"#news · Feed {twins[2].id}"),
        twins[3].id: (name, f"#news · Feed {twins[3].id}"),
        elsewhere.id: (name, "#more-news"),  # already told apart by its channel
        solo.id: ("Solo", "#news"),
    }
    assert len(set(shown.values())) == len(shown)
    assert all(len(label) <= 100 and len(text) <= 100 for label, text in shown.values())


async def test_list_menu_tells_same_named_feeds_apart_without_a_channel_name(env: Env) -> None:
    first = await env.add(name="News")
    second = await env.add(url=URL2, name="News")
    interaction = env.interaction(channels=())
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    options = interaction.last[1]["view"].to_components()[0]["components"][0]["options"]
    assert {int(o["value"]): o.get("description") for o in options} == {
        first.id: "example.com",
        second.id: "other.example",
    }


async def test_list_pages_and_opens_a_panel(env: Env) -> None:
    for number in range(30):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed(f"Feed {number:02}", "", "", ())
        await env.add(url=url)
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    sent = interaction.last[1]
    blocks = sent["content"].split("\n\n")
    checked = env.db.list_feeds(SERVER)[0].last_checked_at
    assert blocks[0] == "**Feeds in this Server**: 30"
    # A page holds as many Feeds as the select under it can list.
    assert len(blocks) == 27 and blocks[-1] == "Page 1 of 2"
    assert blocks[1] == f"**Feed 00** in <#{TEXT}>: Working\n-# checked <t:{checked}:R>"
    assert component_ids(sent["view"]) == [
        "rss:c:feed_list_open",
        "rss:c:feed_list_page:0",
        "rss:c:feed_list_page:1",
    ]
    select_row, button_row = sent["view"].to_components()
    assert len(select_row["components"][0]["options"]) == 25
    assert [b["label"] for b in button_row["components"]] == ["Previous", "Next"]
    assert [b["disabled"] for b in button_row["components"]] == [True, False]

    click = env.click()
    await feed.FeedListPage(1).callback(click)  # type: ignore[arg-type]
    assert click.last[0] == "edit_message"
    blocks = click.text.split("\n\n")
    assert len(blocks) == 7 and blocks[-1] == "Page 2 of 2"
    assert len(click.last[1]["view"].to_components()[0]["components"][0]["options"]) == 5

    last = env.db.list_feeds(SERVER)[-1]
    chosen = env.click()
    await pick(feed.FeedListOpen(), chosen, [str(last.id)])
    assert chosen.last[0] == "send_message"  # a new private message; the list stays
    assert chosen.text.startswith(f"**Feed**: {last.name}")


async def test_list_is_shown_again_when_the_picked_feed_was_deleted(env: Env) -> None:
    gone = await env.add()
    kept = await env.add(url=URL2)
    env.db.delete_feed(gone.id)
    chosen = env.click()
    await pick(feed.FeedListOpen(), chosen, [str(gone.id)])

    assert names(chosen) == ["edit_message"]  # the stale list is replaced, nothing is added
    assert chosen.text.splitlines() == [
        "That Feed no longer exists.",
        "",
        "**Feeds in this Server**: 1",
        "",
        f"**Other Site** in <#{TEXT}>: Working",
        f"-# checked <t:{kept.last_checked_at}:R>",
    ]
    (option,) = chosen.last[1]["view"].to_components()[0]["components"][0]["options"]
    assert option["value"] == str(kept.id)

    env.db.delete_feed(kept.id)
    chosen = env.click()
    await pick(feed.FeedListOpen(), chosen, [str(kept.id)])
    assert names(chosen) == ["edit_message"]
    assert chosen.text == f"That Feed no longer exists.\n\n{feed.NO_FEEDS}"
    assert chosen.last[1]["view"] is None


async def test_list_does_not_open_another_servers_feed(env: Env) -> None:
    foreign = await env.add(server_id=OTHER_SERVER)
    chosen = env.click()
    await pick(feed.FeedListOpen(), chosen, [str(foreign.id)])
    assert chosen.text == f"That Feed no longer exists.\n\n{feed.NO_FEEDS}"


# -- /feed edit --


async def test_edit_opens_the_panel_not_a_form(env: Env) -> None:
    found = await env.add()
    interaction = env.interaction()
    await feed.edit_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    assert interaction.last[0] == "send_message"
    assert interaction.text.startswith(f"**Feed**: {found.name}")


async def test_settings_opens_a_prefilled_form_keeping_an_unlisted_interval(env: Env) -> None:
    found = await env.add()
    env.db.update_feed(found.id, interval_s=5400)
    interaction = env.click()
    await feed.SettingsButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert interaction.last[1]["modal"].to_dict()["custom_id"] == f"rss:m:feed_edit:{found.id}"
    fields = form_fields(interaction)
    assert list(fields) == ["name", "url", "channel", "interval"]
    assert fields["name"]["value"] == "Example News"
    assert fields["url"]["value"] == URL
    assert fields["channel"]["default_values"] == [{"id": TEXT, "type": "channel"}]
    options = fields["interval"]["options"]
    assert len(options) == 10
    assert [(o["value"], o["label"]) for o in options if o["default"]] == [("5400", "90 minutes")]


async def test_edit_rejects_an_option_that_is_not_a_feed(env: Env) -> None:
    with pytest.raises(ui.UserError, match="Choose a Feed"):
        await feed.edit_command.callback(env.interaction(), "news")  # type: ignore[arg-type]


async def test_edit_from_the_command_saves_and_replies_with_the_panel(env: Env) -> None:
    found = await env.add()
    interaction = await submit(env, edit_form(found, TEXT, name="Tech", url=URL2, interval="900"))

    changed = env.feed(found.id)
    assert (changed.name, changed.url, changed.interval_s) == ("Tech", URL2, 900)
    assert changed.channel_id == TEXT
    assert names(interaction) == ["defer", "followup"]
    assert interaction.calls[0][1] == {"ephemeral": True, "thinking": True}
    assert "**Feed**: Tech" in interaction.text
    assert "**Check interval**: every 15 minutes" in interaction.text
    assert env.posts.cleaned == []


async def test_edit_from_the_panel_updates_the_panel_in_place(env: Env) -> None:
    found = await env.add()
    click = env.click()
    await feed.SettingsButton(found.id).callback(click)  # type: ignore[arg-type]
    assert list(form_fields(click)) == ["name", "url", "channel", "interval"]

    interaction = await submit(env, edit_form(found, FORUM, name="Tech"), message=True)
    assert names(interaction) == ["defer", "edit_original_response"]
    assert interaction.calls[0][1] == {}  # a silent defer
    changed = env.feed(found.id)
    assert (changed.channel_id, changed.channel_kind) == (FORUM, ChannelKind.FORUM)
    assert f"**Channel**: <#{FORUM}> (forum)" in interaction.text
    assert "**Warning**" not in interaction.text


async def test_settings_saved_with_a_channel_the_bot_cannot_post_in_warns(env: Env) -> None:
    found = await env.add()
    interaction = await submit(
        env, edit_form(found, LOCKED), message=True, channels=locked_channels()
    )
    assert env.feed(found.id).channel_id == LOCKED  # accepted anyway
    assert interaction.last[0] == "edit_original_response"
    assert interaction.text.splitlines()[-1] == warning()
    assert rows(interaction.last[1]["view"])[0][0] == "Settings"


@pytest.mark.parametrize(
    ("moved_post_as", "stays_post_as", "cleaned"),
    [
        (PostAs.SITE, None, [TEXT]),  # nothing is left in the old channel
        (PostAs.SITE, PostAs.BOT, [TEXT]),  # what is left posts as the bot
        (PostAs.SITE, PostAs.SITE, []),  # another Feed there still needs the webhook
        (PostAs.BOT, PostAs.SITE, []),
    ],
)
async def test_moving_channels_cleans_up_the_webhook_only_when_unused(
    env: Env, moved_post_as: PostAs, stays_post_as: PostAs | None, cleaned: list[int]
) -> None:
    found = await env.add(post_as=moved_post_as)
    if stays_post_as is not None:
        await env.add(url=URL2, post_as=stays_post_as)
    await submit(env, edit_form(found, OTHER))
    assert env.feed(found.id).channel_id == OTHER
    assert env.posts.cleaned == cleaned


async def test_edit_without_moving_never_cleans_up(env: Env) -> None:
    found = await env.add(post_as=PostAs.SITE)
    await submit(env, edit_form(found, TEXT, name="Renamed"))
    assert env.feed(found.id).name == "Renamed"
    assert env.posts.cleaned == []


async def test_edit_shows_the_services_sentence(env: Env) -> None:
    found = await env.add()
    await env.add(url=URL2, channel_id=OTHER)
    interaction = await submit(env, edit_form(found, OTHER, url=URL2))
    assert interaction.text == "That channel already has a Feed for that address."
    assert env.feed(found.id).channel_id == TEXT


# -- /feed remove --


async def test_remove_asks_first(env: Env) -> None:
    found = await env.add()
    interaction = env.interaction()
    await feed.remove_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    assert f"Remove the Feed **Example News** from <#{TEXT}>?" in interaction.text
    view = interaction.last[1]["view"]
    assert component_ids(view) == [f"rss:c:feed_remove:{found.id}", "rss:c:cancel"]
    assert rows(view) == [["Remove", "Cancel"]]
    assert env.feed(found.id) is not None


@pytest.mark.parametrize(("other_post_as", "cleaned"), [(None, [TEXT]), (PostAs.SITE, [])])
async def test_confirming_removes_and_cleans_up_an_unused_webhook(
    env: Env, other_post_as: PostAs | None, cleaned: list[int]
) -> None:
    found = await env.add(post_as=PostAs.SITE)
    if other_post_as is not None:
        await env.add(url=URL2, post_as=other_post_as)
    interaction = env.click()
    await feed.RemoveFeed(found.id).callback(interaction)  # type: ignore[arg-type]

    assert env.db.get_feed(found.id) is None
    assert env.posts.cleaned == cleaned
    assert names(interaction) == ["defer", "edit_original_response"]
    assert interaction.text == f"Removed the Feed **Example News** from <#{TEXT}>."
    assert interaction.last[1]["view"] is None


async def test_cancelling_keeps_the_feed(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await ui.CancelButton().callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == "Cancelled."
    assert env.db.get_feed(found.id) is not None
    assert env.posts.cleaned == []


async def test_remove_from_the_panel_asks_in_place_and_cancel_returns(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await feed.AskRemoveButton(found.id).callback(interaction)  # type: ignore[arg-type]
    assert interaction.last[0] == "edit_message"
    assert "Remove the Feed **Example News**" in interaction.text
    view = interaction.last[1]["view"]
    assert component_ids(view) == [f"rss:c:feed_remove:{found.id}", f"rss:c:feed_panel:{found.id}"]
    assert rows(view) == [["Remove", "Cancel"]]

    back = env.click()
    await feed.BackToPanel(found.id).callback(back)  # type: ignore[arg-type]
    assert back.last[0] == "edit_message"
    assert back.text.startswith("**Feed**: Example News")
    assert env.db.get_feed(found.id) is not None


# -- /feed pause and resume --


async def test_pause_and_resume_act_at_once(env: Env) -> None:
    found = await env.add()
    paused = env.interaction()
    await feed.pause_command.callback(paused, str(found.id))  # type: ignore[arg-type]
    assert env.feed(found.id).paused is PauseReason.MANUAL
    assert paused.text.startswith("Paused the Feed **Example News**.")

    resumed = env.interaction()
    await feed.resume_command.callback(resumed, str(found.id))  # type: ignore[arg-type]
    assert env.feed(found.id).paused is None
    assert resumed.text.startswith("Resumed the Feed **Example News**.")


async def test_pause_and_resume_buttons_edit_the_panel(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await feed.PauseFeed(found.id).callback(interaction)  # type: ignore[arg-type]
    assert env.feed(found.id).paused is PauseReason.MANUAL
    assert interaction.last[0] == "edit_message"
    assert f"**Status**: Paused by <@{USER}> · <t:{Clock.t}:R>" in interaction.text
    assert rows(interaction.last[1]["view"])[2] == ["Test", "Resume", "Remove"]

    interaction = env.click()
    await feed.ResumeFeed(found.id).callback(interaction)  # type: ignore[arg-type]
    assert env.feed(found.id).paused is None
    assert "**Status**: Working" in interaction.text
    assert rows(interaction.last[1]["view"])[2] == ["Test", "Refresh", "Pause", "Remove"]
    assert interaction.text.startswith("**Feed**")  # nothing to warn about


# -- /feed refresh --


async def test_refresh_posts_new_items_without_waiting_for_the_feeds_turn(env: Env) -> None:
    found = await env.add()
    assert env.feed(found.id).next_check_at > Clock.t  # not due
    env.web.listings[URL] = ParsedFeed("Example News", "", "", tuple(item(k) for k in "abcd"))

    interaction = env.interaction()
    await feed.refresh_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    assert [feed_id for feed_id, _ in env.posts.sent] == [found.id]
    assert interaction.calls[0][0] == "defer"
    assert interaction.text == (
        f"Refreshed the Feed **Example News**. Any new Items are now in <#{TEXT}>."
    )

    # The button does the same and shows the panel again.
    env.web.listings[URL] = ParsedFeed("Example News", "", "", tuple(item(k) for k in "abcde"))
    click = env.click()
    await feed.RefreshButton(found.id).callback(click)  # type: ignore[arg-type]
    assert len(env.posts.sent) == 2
    assert click.last[0] == "edit_original_response"
    assert click.text.startswith("Refreshed the Feed **Example News**.")
    assert "**Status**: Working" in click.text


async def test_refresh_says_when_the_check_failed(env: Env) -> None:
    found = await env.add()
    del env.web.listings[URL]
    interaction = env.interaction()
    await feed.refresh_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    assert interaction.text == (
        "Refreshed the Feed **Example News**. Failing: The address answered with error 404."
    )


async def test_refresh_says_when_the_site_rate_limits(env: Env) -> None:
    found = await env.add()
    env.db.update_feed(found.id, rate_limited_since=Clock.t, next_check_at=Clock.t + 7200)
    env.web.errors[URL] = FetchError("slow down", slow_down=True)
    interaction = env.interaction()
    # One Rate-limited feed may be refreshed: it is one request, asked for on purpose.
    await feed.refresh_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    assert interaction.text == "Refreshed the Feed **Example News**. Rate limited"


async def test_refresh_refuses_a_paused_feed(env: Env) -> None:
    found = await env.add()
    await env.service.pause_feed(SERVER, found.id, actor=MEMBER)
    with pytest.raises(ui.UserError, match="That Feed is paused"):
        await feed.refresh_command.callback(env.interaction(), str(found.id))  # type: ignore[arg-type]
    with pytest.raises(ui.UserError, match="That Feed is paused"):
        await feed.RefreshButton(found.id).handle(env.click())  # type: ignore[arg-type]
    assert "Refresh" not in rows((await feed._panel(env.interaction(), env.feed(found.id)))[1])[2]  # type: ignore[arg-type]


async def test_refresh_without_a_feed_makes_every_feed_that_is_not_paused_due(env: Env) -> None:
    with pytest.raises(ui.UserError, match="no Feeds to refresh"):
        await feed.refresh_command.callback(env.interaction())  # type: ignore[arg-type]

    first = await env.add()
    second = await env.add(URL2)
    elsewhere = await env.add(channel_id=OTHER, server_id=OTHER_SERVER)
    await env.service.pause_feed(SERVER, second.id, actor=MEMBER)
    assert env.db.due_feeds(Clock.t) == []

    interaction = env.interaction()
    await feed.refresh_command.callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == (
        "1 Feed will be refreshed, usually within a minute. Paused feeds are left out."
    )
    assert [due.id for due in env.db.due_feeds(Clock.t)] == [first.id]
    assert env.feed(elsewhere.id).next_check_at > Clock.t

    env.web.listings[URL3] = ParsedFeed("Third", "", "", ())
    third = await env.add(URL3)
    env.db.update_feed(third.id, rate_limited_since=Clock.t, next_check_at=Clock.t + 7200)
    interaction = env.interaction()
    await feed.refresh_command.callback(interaction)  # type: ignore[arg-type]
    assert interaction.text == (
        "1 Feed will be refreshed, usually within a minute. Paused feeds are left out, "
        "and so is 1 Rate-limited feed: the site asked the bot to wait."
    )
    assert env.feed(third.id).next_check_at == Clock.t + 7200


TAG_AGAIN = (
    "This forum requires a tag. Choose one under Forum options or the Feed will be paused again."
)


async def test_resume_warns_when_the_channel_is_still_out_of_reach(env: Env) -> None:
    found = await env.add(channel_id=LOCKED)
    env.db.update_feed(found.id, paused=PauseReason.LOST_CHANNEL)
    command = env.interaction(channels=locked_channels())
    await feed.resume_command.callback(command, str(found.id))  # type: ignore[arg-type]
    assert env.feed(found.id).paused is None
    assert command.text.splitlines() == [
        "Resumed the Feed **Example News**. It is checked again from now on.",
        warning(),
    ]

    env.db.update_feed(found.id, paused=PauseReason.LOST_CHANNEL)
    click = env.click(channels=locked_channels())
    await feed.ResumeFeed(found.id).callback(click)  # type: ignore[arg-type]
    assert env.feed(found.id).paused is None
    assert click.last[0] == "edit_message"
    assert click.text.count(warning()) == 1

    # Once the bot has access again there is nothing to warn about.
    env.db.update_feed(found.id, paused=PauseReason.LOST_CHANNEL)
    fine = env.interaction(channels=(*channels(), FakeChannel(LOCKED, "unlocked")))
    await feed.resume_command.callback(fine, str(found.id))  # type: ignore[arg-type]
    assert fine.text == "Resumed the Feed **Example News**. It is checked again from now on."


async def test_resume_warns_when_the_forum_still_needs_a_tag(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    env.db.update_feed(found.id, paused=PauseReason.NEEDS_TAG)
    command = env.interaction()
    await feed.resume_command.callback(command, str(found.id))  # type: ignore[arg-type]
    assert env.feed(found.id).paused is None
    assert command.text.splitlines() == [
        "Resumed the Feed **Example News**. It is checked again from now on.",
        TAG_AGAIN,
    ]

    env.db.update_feed(found.id, paused=PauseReason.NEEDS_TAG)
    click = env.click()
    await feed.ResumeFeed(found.id).callback(click)  # type: ignore[arg-type]
    assert click.text.splitlines()[:3] == [TAG_AGAIN, "", "**Feed**: Example News"]
    assert f"rss:c:feed_forum:{found.id}" in component_ids(click.last[1]["view"])

    # With a tag chosen, or after a pause by a member, the plain answer is right.
    for changes in (
        {"paused": PauseReason.NEEDS_TAG, "forum_tag_ids": (901,)},
        {"paused": PauseReason.MANUAL, "forum_tag_ids": ()},
    ):
        env.db.update_feed(found.id, **changes)
        click = env.click()
        await feed.ResumeFeed(found.id).callback(click)  # type: ignore[arg-type]
        assert click.text.startswith("**Feed**: Example News")
        env.db.update_feed(found.id, **changes)
        command = env.interaction()
        await feed.resume_command.callback(command, str(found.id))  # type: ignore[arg-type]
        assert command.text == (
            "Resumed the Feed **Example News**. It is checked again from now on."
        )


# -- /feed test --


async def test_test_previews_privately_without_posting(env: Env) -> None:
    found = await env.add()
    await env.service.set_embed(
        SERVER, found.id, title="{{title}}", description="{{description}}", actor=MEMBER
    )
    await env.service.add_button(SERVER, found.id, "Read", "{{link}}", actor=MEMBER)
    await env.service.set_mentions(SERVER, found.id, [ROLE], actor=MEMBER)
    interaction = env.interaction()
    await feed.test_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    assert names(interaction) == ["defer", "followup", "followup"]
    preview, note = interaction.calls[1][1], interaction.calls[2][1]
    assert "**Title a**" in preview["content"] and "https://example.com/a" in preview["content"]
    assert preview["ephemeral"] is True and preview["allowed_mentions"] is ui.NO_MENTIONS
    assert preview["embed"].title == "Title a"
    (button,) = preview["view"].to_components()[0]["components"]
    assert (button["label"], button["url"]) == ("Read", "https://example.com/a")
    assert f"as it would be posted in <#{TEXT}>" in note["content"]
    assert "Nothing has been posted" in note["content"]
    assert "Forum post title" not in note["content"]
    assert note["content"].splitlines()[-1] == f"Posting it will mention <@&{ROLE}>."
    assert note["allowed_mentions"] is ui.NO_MENTIONS  # saying so pings nobody
    assert component_ids(note["view"]) == [
        f"rss:c:feed_post:{found.id}",
        f"rss:c:feed_panel:{found.id}",
    ]
    assert rows(note["view"]) == [["Post to channel", "Back to Feed"]]
    assert env.posts.sent == []

    # Back turns the note into the Feed panel; the preview above is another message.
    back = env.click()
    await feed.BackToPanel(found.id).callback(back)  # type: ignore[arg-type]
    assert names(back) == ["edit_message"]
    assert back.text.startswith("**Feed**: Example News")
    assert rows(back.last[1]["view"])[2] == ["Test", "Refresh", "Pause", "Remove"]


async def test_test_of_a_forum_feed_says_the_forum_post_title(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM, post_as=PostAs.SITE)
    await env.service.set_forum_title(SERVER, found.id, "{{feed_title}}: {{title}}", actor=MEMBER)
    interaction = env.click()
    await feed.TestButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert names(interaction) == ["defer", "followup", "followup"]
    assert interaction.calls[0][1] == {"ephemeral": True, "thinking": True}  # the panel stays
    assert "**Forum post title**: Example News: Title a" in interaction.text
    assert "**Post as**: Example News" in interaction.text
    assert "will mention" not in interaction.text  # the Feed has no mention roles
    assert rows(interaction.last[1]["view"]) == [["Post to channel", "Back to Feed"]]


async def test_test_shows_the_services_sentence(env: Env) -> None:
    found = await env.add()
    del env.web.listings[URL]
    interaction = env.interaction()
    with pytest.raises(ServiceError, match="error 404"):
        await feed.test_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    assert names(interaction) == ["defer"]


@pytest.mark.parametrize(
    ("outcome", "words", "again"),
    [
        (DeliveryOutcome.DELIVERED, f"Posted the newest Item in <#{TEXT}>.", False),
        (DeliveryOutcome.RETRY, "try again in a minute", True),
        (DeliveryOutcome.REJECTED, "Discord refused the message", True),
        (DeliveryOutcome.LOST_CHANNEL, f"The bot cannot post in <#{TEXT}>", True),
        (DeliveryOutcome.NEEDS_TAG, "requires a tag on every Forum post", True),
    ],
)
async def test_post_to_channel_reports_each_outcome(
    env: Env, outcome: DeliveryOutcome, words: str, again: bool
) -> None:
    found = await env.add()
    env.posts.outcome = outcome
    interaction = env.click()
    await feed.PostPreview(found.id).callback(interaction)  # type: ignore[arg-type]

    ((feed_id, message),) = env.posts.sent
    assert feed_id == found.id and "**Title a**" in message.content
    assert names(interaction) == ["defer", "edit_original_response"]
    assert words in interaction.text
    view = interaction.last[1]["view"]
    back = f"rss:c:feed_panel:{found.id}"
    if again:
        assert rows(view) == [["Try again", "Back to Feed"]]
        assert component_ids(view) == [f"rss:c:feed_post:{found.id}", back]
    else:
        assert rows(view) == [["Back to Feed"]]
        assert component_ids(view) == [back]
    button = view.to_components()[0]["components"][-1]
    assert button["style"] == discord.ButtonStyle.secondary.value  # as on the other screens

    returned = env.click()
    await feed.BackToPanel(found.id).callback(returned)  # type: ignore[arg-type]
    assert names(returned) == ["edit_message"]
    assert returned.text.startswith("**Feed**: Example News")
    wordings = {text for text in feed.OUTCOME_WORDS.values()}
    assert set(feed.OUTCOME_WORDS) == set(DeliveryOutcome) and len(wordings) == 5


# -- /feed import and export --


def attachment(data: bytes, size: int | None = None) -> Any:
    read: list[int] = []

    async def reader() -> bytes:
        read.append(1)
        return data

    return SimpleNamespace(size=len(data) if size is None else size, read=reader, was_read=read)


async def test_import_summarises_what_happened(env: Env) -> None:
    await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    data = build_opml(
        [OpmlEntry("Example", URL), OpmlEntry("Other", URL2), OpmlEntry("Gone *site*", MISSING)],
        "Mine",
    )
    interaction = env.interaction()
    target = SimpleNamespace(id=FORUM, type=discord.ChannelType.forum)
    await feed.import_command.callback(interaction, attachment(data), target)  # type: ignore[arg-type]

    assert names(interaction) == ["defer", "followup"]
    assert interaction.text.splitlines() == [
        f"Imported the file into <#{FORUM}>.",
        "**Added**: 1 (Other)",
        "Items their sources list now will not be posted, only later ones.",
        "**Skipped**: 1 (the channel already has a Feed for each of these)",
        "**Failed**: 1",
        "- Gone \\*site\\*: The address answered with error 404.",
    ]
    added = [f for f in env.db.list_feeds(SERVER) if f.url == URL2]
    assert [(f.channel_id, f.channel_kind) for f in added] == [(FORUM, ChannelKind.FORUM)]


async def test_import_defaults_to_the_current_channel(env: Env) -> None:
    interaction = env.interaction()
    interaction.channel = FakeChannel(TEXT, "news")  # type: ignore[attr-defined]
    data = build_opml([OpmlEntry("Example", URL)], "Mine")
    await feed.import_command.callback(interaction, attachment(data))  # type: ignore[arg-type]
    (added,) = env.db.list_feeds(SERVER)
    assert (added.channel_id, added.channel_kind) == (TEXT, ChannelKind.MESSAGES)
    assert f"Imported the file into <#{TEXT}>." in interaction.text


async def test_import_into_a_channel_the_bot_cannot_post_in_warns_once(env: Env) -> None:
    data = build_opml([OpmlEntry("Example", URL), OpmlEntry("Other", URL2)], "Mine")
    interaction = env.interaction(channels=locked_channels())
    target = SimpleNamespace(id=LOCKED, type=discord.ChannelType.text)
    await feed.import_command.callback(interaction, attachment(data), target)  # type: ignore[arg-type]

    assert len(env.db.list_feeds(SERVER)) == 2  # accepted anyway
    lines = interaction.text.splitlines()
    assert lines[:3] == [
        f"Imported the file into <#{LOCKED}>.",
        warning(),
        "**Added**: 2 (Example, Other)",
    ]
    assert interaction.text.count("**Warning**") == 1  # once for the channel, not per Feed

    # Nothing new was added the second time, so no channel is affected.
    again = env.interaction(channels=locked_channels())
    await feed.import_command.callback(again, attachment(data), target)  # type: ignore[arg-type]
    assert "**Warning**" not in again.text and "**Added**: 0" in again.text


def test_import_summary_caps_names_and_failures() -> None:
    result = OpmlImport(
        added=tuple(f"Feed {n}" for n in range(20)),
        skipped=0,
        failed=tuple(ImportFailure(f"T{n}", f"https://e/{n}", "No.") for n in range(13)),
        left_out=4,
    )
    lines = feed._import_summary(result, TEXT).splitlines()
    assert lines[1].startswith("**Added**: 20 (Feed 0, ")
    assert lines[1].endswith("Feed 14 and 5 more)")
    assert lines[3].startswith("**Left out**: 4 (one import adds at most 100 Feeds")
    assert lines[4] == "**Failed**: 13"
    assert len([line for line in lines if line.startswith("- ")]) == 10
    assert lines[-1] == "…and 3 more."
    assert not any(line.startswith("**Skipped**") for line in lines)


async def test_import_refuses_a_large_file_before_downloading(env: Env) -> None:
    interaction = env.interaction()
    interaction.channel = FakeChannel(TEXT, "news")  # type: ignore[attr-defined]
    file = attachment(b"<opml/>", size=1024 * 1024 + 1)
    with pytest.raises(ui.UserError, match="at most 1 MB"):
        await feed.import_command.callback(interaction, file)  # type: ignore[arg-type]
    assert file.was_read == [] and interaction.calls == []


async def test_import_refuses_a_channel_that_cannot_hold_a_feed(env: Env) -> None:
    interaction = env.interaction()
    target = SimpleNamespace(id=VOICE, type=discord.ChannelType.voice)
    file = attachment(b"<opml/>")
    with pytest.raises(ui.UserError, match="can only post in"):
        await feed.import_command.callback(interaction, file, target)  # type: ignore[arg-type]
    assert file.was_read == []


async def test_export_attaches_the_opml_file(env: Env) -> None:
    await env.add()
    await env.add(url=URL2, channel_id=OTHER)
    interaction = env.interaction()
    await feed.export_command.callback(interaction)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True and sent["allowed_mentions"] is ui.NO_MENTIONS
    assert sent["file"].filename == "feeds.opml"
    entries = parse_opml(sent["file"].fp.read())
    assert sorted(entry.url for entry in entries) == sorted([URL, URL2])


async def test_export_with_no_feeds_shows_the_services_sentence(env: Env) -> None:
    with pytest.raises(ServiceError, match="This Server has no Feeds yet"):
        await feed.export_command.callback(env.interaction())  # type: ignore[arg-type]


# -- The Feed panel --


async def test_panel_of_a_feed_in_a_text_channel(env: Env) -> None:
    found = await env.add()
    await env.service.add_field(SERVER, found.id, "By", "{{author}}", actor=MEMBER)
    await env.service.add_button(SERVER, found.id, "Read", "{{link}}", actor=MEMBER)
    await env.service.set_mentions(SERVER, found.id, [ROLE, ROLE + 1], actor=MEMBER)
    await env.service.add_filters(SERVER, found.id, "block", "any", ["a", "b", "c"], actor=MEMBER)
    interaction = env.interaction()
    await feed.open_panel(interaction, found.id)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True and sent["allowed_mentions"] is ui.NO_MENTIONS
    assert sent["content"].splitlines() == [
        "**Feed**: Example News",
        f"**Address**: <{URL}>",
        f"**Channel**: <#{TEXT}>",
        "**Check interval**: every 10 minutes",
        "**Status**: Working",
        f"**Last checked**: <t:{found.last_checked_at}:R>",
        f"**Next Check**: <t:{found.next_check_at}:R>",
        f"**Added by**: <@{USER}> · <t:{Clock.t}:D>",
        "**Post as**: The bot",
        "**Message text**: yes · **Embed**: yes",
        "**Filters**: 3 · **Fields**: 1 · **Buttons**: 1 · **Mention roles**: 2",
    ]
    assert rows(sent["view"]) == [
        ["Settings", "Message text", "Embed", "Fields", "Buttons"],
        ["Filters", "Mentions", "Post as"],
        ["Test", "Refresh", "Pause", "Remove"],
    ]
    ids = component_ids(sent["view"])
    assert len(set(ids)) == len(ids) == 12
    assert all(custom_id.endswith(f":{found.id}") for custom_id in ids)
    assert sent["view"].timeout is None


async def test_panel_warns_when_the_bot_cannot_post_in_the_channel(env: Env) -> None:
    found = await env.add(channel_id=LOCKED)
    interaction = env.interaction(channels=locked_channels())
    await feed.open_panel(interaction, found.id)  # type: ignore[arg-type]
    lines = interaction.text.splitlines()
    assert lines[0] == "**Feed**: Example News"
    assert lines[-1] == warning()
    assert rows(interaction.last[1]["view"])[2] == ["Test", "Refresh", "Pause", "Remove"]

    # A channel the Server's cache does not have is not known to be out of reach.
    unknown = env.interaction()
    await feed.open_panel(unknown, found.id)  # type: ignore[arg-type]
    assert "**Warning**" not in unknown.text


async def test_panel_of_a_feed_in_a_forum(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM, post_as=PostAs.SITE)
    interaction = env.click()
    await feed.open_panel(interaction, found.id, edit=True)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "edit_message"
    lines = sent["content"].splitlines()
    assert f"**Channel**: <#{FORUM}> (forum)" in lines
    assert "**Post as**: The site's name and icon (Example News)" in lines
    assert lines[-1] == (
        "**Forum post title**: `{{title||feed_title}}` · **Tags**: 0 · **Cover image**: on"
    )
    assert rows(sent["view"]) == [
        ["Settings", "Message text", "Embed", "Fields", "Buttons"],
        ["Filters", "Mentions", "Post as", "Forum options"],
        ["Test", "Refresh", "Pause", "Remove"],
    ]


async def test_panel_buttons_hand_over_to_the_other_modules_undeferred(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    def stand_in(name: str, openers: tuple[str, ...]) -> None:
        module = types.ModuleType(f"rssbot.commands.{name}")
        for opener in openers:

            async def opened(interaction: Any, feed_id: int, opener: str = opener) -> None:
                assert not interaction.response.is_done()
                calls.append((opener, feed_id))

            setattr(module, opener, opened)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(commands, name, module, raising=False)

    stand_in("template", ("open_text", "open_embed", "open_fields", "open_buttons"))
    stand_in("filter", ("open_filters",))
    found = await env.add()
    buttons = (
        feed.TextButton,
        feed.EmbedButton,
        feed.FieldsButton,
        feed.ButtonsButton,
        feed.FiltersButton,
    )
    for button in buttons:
        interaction = env.click()
        await button(found.id).callback(interaction)  # type: ignore[arg-type]
        assert interaction.calls == []
    assert calls == [
        ("open_text", found.id),
        ("open_embed", found.id),
        ("open_fields", found.id),
        ("open_buttons", found.id),
        ("open_filters", found.id),
    ]


# -- Mentions --


async def test_mentions_are_picked_and_cleared_from_the_panel(env: Env) -> None:
    found = await env.add()
    await env.service.set_mentions(SERVER, found.id, [ROLE], actor=MEMBER)
    interaction = env.click()
    await feed.MentionsButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert interaction.last[0] == "edit_message"
    assert f"**Mention roles now**: <@&{ROLE}>" in interaction.text
    view = interaction.last[1]["view"]
    assert rows(view) == [["Choose the roles to mention"], ["Clear", "Back to Feed"]]
    select = view.to_components()[0]["components"][0]
    assert select["type"] == discord.ComponentType.role_select.value
    assert (select["min_values"], select["max_values"]) == (0, 10)
    assert select["default_values"] == [{"id": ROLE, "type": "role"}]

    picked = env.click()
    await pick(feed.MentionsSelect(found.id), picked, [discord.Object(51), discord.Object(52)])
    assert env.feed(found.id).mention_role_ids == (51, 52)
    assert picked.last[0] == "edit_message"
    assert "**Mention roles**: 2" in picked.text  # back on the panel

    cleared = env.click()
    await feed.ClearMentions(found.id).callback(cleared)  # type: ignore[arg-type]
    assert env.feed(found.id).mention_role_ids == ()
    assert "**Mention roles**: 0" in cleared.text


async def test_mentions_screen_preselects_only_roles_that_still_exist(env: Env) -> None:
    found = await env.add()
    await env.service.set_mentions(SERVER, found.id, [ROLE, 51], actor=MEMBER)
    interaction = env.click()
    existing = {ROLE: SimpleNamespace(id=ROLE)}  # role 51 was deleted from the Server
    interaction.guild.get_role = existing.get  # type: ignore[union-attr]
    await feed.MentionsButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert interaction.last[0] == "edit_message"
    view = interaction.last[1]["view"]
    select = view.to_components()[0]["components"][0]
    assert select["default_values"] == [{"id": ROLE, "type": "role"}]
    assert f"**Mention roles now**: <@&{ROLE}>, <@&51>" in interaction.text
    assert rows(view) == [["Choose the roles to mention"], ["Clear", "Back to Feed"]]
    assert view.to_components()[1]["components"][0]["disabled"] is False  # 51 can be cleared


EVERYONE_LEFT_OUT = "`@everyone` cannot be a mention role, so it was left out."


async def test_picking_everyone_as_a_mention_role_is_said_to_be_left_out(env: Env) -> None:
    found = await env.add()
    only = env.click()
    await pick(feed.MentionsSelect(found.id), only, [discord.Object(SERVER)])
    assert env.feed(found.id).mention_role_ids == ()
    assert only.last[0] == "edit_message"
    assert only.text.splitlines()[:3] == [EVERYONE_LEFT_OUT, "", "**Feed**: Example News"]

    mixed = env.click()
    await pick(feed.MentionsSelect(found.id), mixed, [discord.Object(SERVER), discord.Object(51)])
    assert env.feed(found.id).mention_role_ids == (51,)
    assert mixed.text.startswith(EVERYONE_LEFT_OUT)
    assert "**Mention roles**: 1" in mixed.text

    plain = env.click()
    await pick(feed.MentionsSelect(found.id), plain, [discord.Object(52)])
    assert plain.text.startswith("**Feed**: Example News")


# -- Post as --


async def test_post_as_shows_the_choices(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await feed.PostAsButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert "**Post as now**: The bot" in interaction.text
    view = interaction.last[1]["view"]
    assert rows(view) == [["Choose who the Feed posts as"], ["Back to Feed"]]
    options = view.to_components()[0]["components"][0]["options"]
    assert [(o["label"], o["value"], o["default"]) for o in options] == [
        ("The bot", "bot", True),
        ("The site's name and icon", "site", False),
        ("A custom name and picture", "custom", False),
    ]


async def test_post_as_the_site_applies_after_a_defer(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await pick(feed.PostAsSelect(found.id), interaction, ["site"])

    assert names(interaction) == ["defer", "edit_original_response"]
    assert interaction.calls[0][1] == {}
    assert env.feed(found.id).post_as is PostAs.SITE
    assert "**Post as**: The site's name and icon (Example News)" in interaction.text
    assert env.posts.cleaned == []


@pytest.mark.parametrize(("other_post_as", "cleaned"), [(None, [TEXT]), (PostAs.SITE, [])])
async def test_post_as_the_bot_cleans_up_an_unused_webhook(
    env: Env, other_post_as: PostAs | None, cleaned: list[int]
) -> None:
    found = await env.add(post_as=PostAs.SITE)
    if other_post_as is not None:
        await env.add(url=URL2, post_as=other_post_as)
    interaction = env.click()
    await pick(feed.PostAsSelect(found.id), interaction, ["bot"])

    assert env.feed(found.id).post_as is PostAs.BOT
    assert env.posts.cleaned == cleaned
    assert "**Post as**: The bot" in interaction.text


async def test_post_as_custom_opens_a_form_then_applies(env: Env) -> None:
    found = await env.add()
    interaction = env.click()
    await pick(feed.PostAsSelect(found.id), interaction, ["custom"])

    assert names(interaction) == ["send_modal"]  # the first response, nothing deferred
    assert interaction.last[1]["modal"].to_dict()["custom_id"] == f"rss:m:feed_custom:{found.id}"
    fields = form_fields(interaction)
    assert list(fields) == ["name", "picture"]
    assert fields["name"]["max_length"] == 80 and fields["picture"]["required"] is False
    assert env.feed(found.id).post_as is PostAs.BOT

    texts = {"name": "The Daily", "picture": "https://example.com/p.png"}
    submitted = await submit(
        env, submission(f"rss:m:feed_custom:{found.id}", texts=texts), message=True
    )
    changed = env.feed(found.id)
    assert (changed.post_as, changed.custom_name) == (PostAs.CUSTOM, "The Daily")
    assert changed.custom_avatar == "https://example.com/p.png"
    assert submitted.last[0] == "edit_message"
    assert "**Post as**: A custom name and picture (The Daily)" in submitted.text

    refused = await submit(
        env, submission(f"rss:m:feed_custom:{found.id}", texts={"name": ""}), message=True
    )
    assert refused.text == "A custom name is required to post under a custom name."


# -- Forum options --


async def test_forum_options_show_title_tags_and_cover(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    await env.service.set_forum_tags(SERVER, found.id, [902], actor=MEMBER)
    interaction = env.click()
    await feed.ForumButton(found.id).callback(interaction)  # type: ignore[arg-type]

    assert interaction.last[0] == "edit_message"
    assert interaction.text.splitlines() == [
        f"Forum options of **Example News** in <#{FORUM}>.",
        "**Forum post title**: `{{title||feed_title}}`",
        "**Tags**: Tech",
        "**Cover image**: on",
    ]
    view = interaction.last[1]["view"]
    assert rows(view) == [
        ["Choose the tags put on every Forum post"],
        ["Post title", "Cover image: On", "Back to Feed"],
    ]
    assert component_ids(view) == [
        f"rss:c:feed_forum_tags:{found.id}",
        f"rss:c:feed_forum_title:{found.id}",
        f"rss:c:feed_forum_cover:{found.id}:0",
        f"rss:c:feed_panel:{found.id}",
    ]
    select = view.to_components()[0]["components"][0]
    assert (select["min_values"], select["max_values"]) == (0, 2)
    assert [(o["label"], o["value"], o["default"]) for o in select["options"]] == [
        ("News", "901", False),
        ("Tech", "902", True),
    ]


async def test_forum_options_cap_the_tags_at_discords_limits(env: Env) -> None:
    tags = [SimpleNamespace(id=n, name=f"Tag {n}") for n in range(1, 31)]
    select = feed.ForumTagsSelect(1, tags=tags, current=[2]).item.to_component_dict()
    assert len(select["options"]) == 25 and select["max_values"] == 5


async def test_forum_tags_and_cover_are_set_and_return_to_the_panel(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    interaction = env.click()
    await pick(feed.ForumTagsSelect(found.id), interaction, ["901", "902"])
    assert env.feed(found.id).forum_tag_ids == (901, 902)
    assert interaction.last[0] == "edit_message"
    assert "**Tags**: 2" in interaction.text

    interaction = env.click()
    await feed.ForumCoverButton(found.id, 0).callback(interaction)  # type: ignore[arg-type]
    assert env.feed(found.id).forum_cover is False
    assert "**Cover image**: off" in interaction.text

    interaction = env.click()
    await feed.ForumCoverButton(found.id, 1).callback(interaction)  # type: ignore[arg-type]
    assert env.feed(found.id).forum_cover is True


async def test_forum_post_title_is_set_through_a_form(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    interaction = env.click()
    await feed.ForumTitleButton(found.id).callback(interaction)  # type: ignore[arg-type]
    fields = form_fields(interaction)
    assert fields["title"]["value"] == "{{title||feed_title}}"
    assert fields["title"]["max_length"] == 200

    data = submission(f"rss:m:feed_title:{found.id}", texts={"title": "New: {{title}}"})
    submitted = await submit(env, data, message=True)
    assert env.feed(found.id).forum_title_template == "New: {{title}}"
    assert submitted.last[0] == "edit_message"
    assert "**Forum post title**: `New: {{title}}`" in submitted.text


async def test_forum_options_are_refused_for_a_feed_outside_a_forum(env: Env) -> None:
    found = await env.add()
    for button in (feed.ForumButton(found.id), feed.ForumCoverButton(found.id, 0)):
        interaction = env.click()
        await button.callback(interaction)  # type: ignore[arg-type]
        assert interaction.text == feed.NOT_A_FORUM
    assert env.feed(found.id).forum_cover is True


async def test_list_shows_each_status_with_its_times(env: Env) -> None:
    working = await env.add()
    limited = await env.add(URL2)
    env.web.listings[URL3] = ParsedFeed("Old Site", "", "", ())
    env.web.listings[URL4] = ParsedFeed("Comics", "", "", ())
    failing = await env.add(URL3)
    paused = await env.add(URL4)
    env.db.update_feed(
        limited.id, rate_limited_since=Clock.t, next_check_at=Clock.t + 7200, fail_count=1
    )
    env.db.update_feed(
        failing.id, fail_count=3, last_error="The site is down.", next_check_at=Clock.t - 5
    )
    await env.service.pause_feed(SERVER, paused.id, actor=MEMBER)
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    at = f"<t:{working.last_checked_at}:R>"
    assert interaction.text.split("\n\n") == [
        "**Feeds in this Server**: 4 (1 working, 1 failing, 1 rate limited, 1 paused)",
        f"**Comics** in <#{TEXT}>: Paused by <@{USER}> · <t:{Clock.t}:R>\n-# last worked {at}",
        f"**Example News** in <#{TEXT}>: Working\n-# checked {at}",
        f"**Old Site** in <#{TEXT}>: Failing: The site is down.\n"
        f"-# checked {at} · next Check due now · last worked {at}",
        f"**Other Site** in <#{TEXT}>: Rate limited\n"
        f"-# checked {at} · next Check <t:{Clock.t + 7200}:R> · last worked {at}",
    ]


async def test_list_page_holds_fewer_feeds_when_their_lines_are_long(env: Env) -> None:
    for number in range(12):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed(f"Feed {number:02}", "", "", ())
        added = await env.add(url=url)
        env.db.update_feed(added.id, fail_count=1, last_error="x" * 300)
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    shown = 0
    for page in range(3):
        click = env.click()
        await feed.FeedListPage(page).callback(click)  # type: ignore[arg-type]
        assert len(click.text) <= ui.MESSAGE_LIMIT and not click.text.endswith("…")
        assert click.text.endswith(f"Page {page + 1} of 3")
        shown += click.text.count("\n\n**Feed ")
    assert shown == 12


async def test_panel_of_a_rate_limited_feed_says_when_it_last_worked(env: Env) -> None:
    found = await env.add()
    env.db.update_feed(found.id, rate_limited_since=Clock.t, next_check_at=Clock.t + 7200)
    interaction = env.interaction()
    await feed.open_panel(interaction, found.id)  # type: ignore[arg-type]
    assert interaction.text.splitlines()[4:8] == [
        "**Status**: Rate limited",
        f"**Last checked**: <t:{found.last_checked_at}:R>",
        f"**Next Check**: <t:{Clock.t + 7200}:R>",
        f"**Last worked**: <t:{found.last_success_at}:R>",
    ]


# -- Log entries: who did it comes from the interaction --

ROBIN = {"display_name": "Robin"}


def logged(env: Env) -> list[LogEntry]:
    """The Server's Log entries, oldest first."""
    return env.db.list_log_entries(SERVER, limit=100)[::-1]


def kinds_by_robin(env: Env, since: int) -> list[LogKind]:
    entries = logged(env)[since:]
    assert all((e.actor_id, e.actor_name) == (USER, "Robin") for e in entries), entries
    return [entry.kind for entry in entries]


async def test_adding_and_editing_are_saved_by_the_member_who_did_it(env: Env) -> None:
    await submit(env, add_form(), **ROBIN)
    found = env.db.list_feeds(SERVER)[0]
    await submit(env, edit_form(found, OTHER, name="Tech", interval="900"), **ROBIN)
    await submit(env, edit_form(env.feed(found.id), OTHER), **ROBIN)  # saved as it was
    assert kinds_by_robin(env, 0) == [LogKind.FEED_ADDED, LogKind.FEED_EDITED]
    assert logged(env)[1].changes == (
        Change("Name", "Example News", "Tech"),
        Change("Channel", f"<#{TEXT}>", f"<#{OTHER}>"),
        Change("Check interval", "10 minutes", "15 minutes"),
    )


async def test_pause_resume_and_remove_are_saved_by_the_member_who_did_it(env: Env) -> None:
    found = await env.add()
    await feed.pause_command.callback(env.interaction(**ROBIN), str(found.id))  # type: ignore[arg-type]
    await feed.PauseFeed(found.id).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    await feed.ResumeFeed(found.id).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    await feed.resume_command.callback(env.interaction(**ROBIN), str(found.id))  # type: ignore[arg-type]
    await feed.PauseFeed(found.id).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    await feed.resume_command.callback(env.interaction(**ROBIN), str(found.id))  # type: ignore[arg-type]
    await feed.RemoveFeed(found.id).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    assert kinds_by_robin(env, 1) == [
        LogKind.FEED_PAUSED,
        LogKind.FEED_RESUMED,
        LogKind.FEED_PAUSED,
        LogKind.FEED_RESUMED,
        LogKind.FEED_REMOVED,
    ]
    assert logged(env)[-1].feed_name == "Example News"


async def test_the_panels_settings_are_saved_by_the_member_who_did_it(env: Env) -> None:
    found = await env.add(channel_id=FORUM, kind=ChannelKind.FORUM)
    await pick(feed.MentionsSelect(found.id), env.click(**ROBIN), [discord.Object(51)])
    await feed.ClearMentions(found.id).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    texts = {"name": "Newsdesk", "picture": ""}
    custom = submission(f"rss:m:feed_custom:{found.id}", texts=texts)
    await submit(env, custom, message=True, **ROBIN)
    await pick(feed.PostAsSelect(found.id), env.click(**ROBIN), ["bot"])
    await pick(feed.ForumTagsSelect(found.id), env.click(**ROBIN), ["901"])
    await feed.ForumCoverButton(found.id, 0).callback(env.click(**ROBIN))  # type: ignore[arg-type]
    title = submission(f"rss:m:feed_title:{found.id}", texts={"title": "New: {{title}}"})
    await submit(env, title, message=True, **ROBIN)
    assert kinds_by_robin(env, 1) == [
        LogKind.MENTIONS_CHANGED,
        LogKind.MENTIONS_CHANGED,
        LogKind.POST_AS_CHANGED,
        LogKind.POST_AS_CHANGED,
        LogKind.FORUM_TAGS_CHANGED,
        LogKind.TEMPLATE_CHANGED,
        LogKind.TEMPLATE_CHANGED,
    ]


async def test_an_import_is_saved_by_the_member_who_did_it(env: Env) -> None:
    data = build_opml([OpmlEntry("Example", URL), OpmlEntry("Other", URL2)], "Mine")
    interaction = env.interaction(**ROBIN)
    target = SimpleNamespace(id=TEXT, type=discord.ChannelType.text)
    await feed.import_command.callback(interaction, attachment(data), target)  # type: ignore[arg-type]
    assert kinds_by_robin(env, 0) == [LogKind.FEED_ADDED, LogKind.FEED_ADDED]


async def test_refresh_previews_exports_and_refusals_are_not_saved(env: Env) -> None:
    found = await env.add()
    await feed.refresh_command.callback(env.interaction(), str(found.id))  # type: ignore[arg-type]
    await feed.refresh_command.callback(env.interaction())  # type: ignore[arg-type]
    await feed.RefreshButton(found.id).callback(env.click())  # type: ignore[arg-type]
    await feed.TestButton(found.id).callback(env.click())  # type: ignore[arg-type]
    await feed.test_command.callback(env.interaction(), str(found.id))  # type: ignore[arg-type]
    await feed.export_command.callback(env.interaction())  # type: ignore[arg-type]
    await feed.list_command.callback(env.interaction())  # type: ignore[arg-type]
    await feed.PauseFeed(found.id).callback(env.click(user_id=STRANGER))  # type: ignore[arg-type]
    await submit(env, add_form(url=MISSING))
    assert [entry.kind for entry in logged(env)] == [LogKind.FEED_ADDED]
    assert env.feed(found.id).paused is None


SECRET_URL = "https://example.com/private/feed.xml?key=s3cret-t0ken"


async def test_a_feed_address_never_reaches_the_container_log(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rssbot")
    env.web.listings[SECRET_URL] = ParsedFeed("Private", "", "", (item("p"),))
    gone = "https://gone.example/private?key=s3cret-t0ken"

    refused = await submit(env, add_form(url=gone))  # shown to the member, who typed it
    assert "s3cret-t0ken" in refused.text
    added = await submit(env, add_form(url=SECRET_URL))
    assert "Added the Feed **Private**" in added.text
    found = next(f for f in env.db.list_feeds(SERVER) if f.url == SECRET_URL)
    await submit(env, edit_form(found, TEXT, url=gone))  # refused
    env.web.listings[gone] = ParsedFeed("Private", "", "", (item("p"),))
    await submit(env, edit_form(found, TEXT, url=gone))
    assert env.feed(found.id).url == gone

    lines = [r.getMessage() for r in caplog.records if r.name == "rssbot.commands"]
    start = 'command name="form feed_{}" server=100 channel=55 by="Alex" by_id=2 outcome='
    assert lines == [
        start.format("add") + 'refused reason="The address answered with error 404."',
        start.format("add") + "ok",
        start.format("edit") + 'refused reason="The address answered with error 404."',
        start.format("edit") + "ok",
    ]
    saved = [r.getMessage() for r in caplog.records if r.name == "rssbot.journal"]
    assert [line.split()[0] for line in saved] == ["feed.add", "feed.edit"]
    assert 'changes="Address: https://example.com/… -> https://gone.example/…"' in saved[1]
    for record in caplog.records:
        assert "s3cret" not in record.getMessage(), record
        assert "/private" not in record.getMessage(), record
    # The Log entry itself keeps the address: Managers see it in the Server.
    assert logged(env)[-1].changes == (Change("Address", SECRET_URL, gone),)


# -- Who added and who paused --

SAM = Actor(id=STRANGER, name="Sam *Q*")
KIM = Actor(id=4, name="Kim")
FELLOW = {"cached": (STRANGER, 4)}  # members who are in the Server's cache


def pause_by_bot(env: Env, found: Feed, reason: PauseReason = PauseReason.LOST_CHANNEL) -> int:
    """Pause a Feed as the scheduler does, saving the Log entry; returns when."""
    env.db.update_feed(found.id, paused=reason)
    env.db.add_log_entry(
        server_id=SERVER,
        at=Clock.t + 5,
        actor_id=None,
        actor_name="",
        kind=LogKind.FEED_AUTO_PAUSED,
        feed_id=found.id,
        feed_name=found.name,
        channel_id=found.channel_id,
        detail="The bot can no longer post in that channel.",
    )
    return Clock.t + 5


def blocks(interaction: FakeInteraction) -> dict[str, str]:
    """The list's Feeds by name, each with its times line."""
    found = {}
    for block in interaction.text.split("\n\n")[1:]:
        found[block.split("**")[1]] = block.split(": ", 1)[1].split("\n")[0]
    return found


async def test_list_and_panel_word_the_three_kinds_of_pause(env: Env) -> None:
    env.web.listings[URL3] = ParsedFeed("Old Site", "", "", ())
    by_member = await env.add()
    by_bot = await env.add(URL2)
    before_this_feature = await env.add(URL3)
    await env.service.pause_feed(SERVER, by_member.id, actor=MEMBER)
    bot_at = pause_by_bot(env, by_bot)
    env.db.update_feed(before_this_feature.id, paused=PauseReason.MANUAL)  # no Log entry
    interaction = env.interaction()
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    assert blocks(interaction) == {
        "Example News": f"Paused by <@{USER}> · <t:{Clock.t}:R>",
        "Old Site": "Paused: by a member",
        "Other Site": "Paused by the bot: the bot can no longer post in its channel"
        f" · <t:{bot_at}:R>",
    }
    assert interaction.guild.fetched == []  # type: ignore[union-attr]  # the member is the clicker
    for found, status in (
        (by_member, f"Paused by <@{USER}> · <t:{Clock.t}:R>"),
        (by_bot, f"Paused by the bot: the bot can no longer post in its channel · <t:{bot_at}:R>"),
        (before_this_feature, "Paused: by a member"),
    ):
        panel = env.interaction()
        await feed.open_panel(panel, found.id)  # type: ignore[arg-type]
        assert f"**Status**: {status}" in panel.text.splitlines()


async def test_list_asks_who_paused_once(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for number in range(3):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed(f"Feed {number}", "", "", ())
        await env.service.pause_feed(SERVER, (await env.add(url=url)).id, actor=MEMBER)
    calls: list[str] = []
    real = env.db.feed_attributions
    monkeypatch.setattr(env.db, "feed_attributions", lambda sid: calls.append("all") or real(sid))
    monkeypatch.setattr(env.db, "feed_attribution", lambda fid: calls.append("one"))
    await feed.list_command.callback(env.interaction())  # type: ignore[arg-type]
    assert calls == ["all"]


async def test_list_and_panel_name_a_member_who_left(env: Env) -> None:
    found = await env.add()
    other = await env.add(URL2)
    await env.service.pause_feed(SERVER, found.id, actor=SAM)
    await env.service.pause_feed(SERVER, other.id, actor=SAM)
    interaction = env.interaction(members=(USER,))
    await feed.list_command.callback(interaction)  # type: ignore[arg-type]

    left = f"Paused by Sam \\*Q\\* (left the Server) · <t:{Clock.t}:R>"
    assert blocks(interaction) == {"Example News": left, "Other Site": left}
    assert interaction.guild.fetched == [STRANGER]  # type: ignore[union-attr]  # once
    assert names(interaction) == ["defer", "followup"]
    assert interaction.last[1]["allowed_mentions"] is ui.NO_MENTIONS

    panel = env.interaction(members=(USER,))
    await feed.open_panel(panel, found.id)  # type: ignore[arg-type]
    assert f"**Status**: {left}" in panel.text.splitlines()
    assert panel.guild.fetched == [STRANGER]  # type: ignore[union-attr]

    here = env.interaction(**FELLOW)
    await feed.list_command.callback(here)  # type: ignore[arg-type]
    assert blocks(here)["Example News"] == f"Paused by <@{STRANGER}> · <t:{Clock.t}:R>"
    assert here.guild.fetched == [] and names(here) == ["send_message"]  # type: ignore[union-attr]


async def test_list_shows_the_mention_when_a_lookup_fails_or_there_are_too_many(env: Env) -> None:
    found = await env.add()
    await env.service.pause_feed(SERVER, found.id, actor=SAM)
    failing = env.interaction(members=(), fetch_fails=(STRANGER,))
    await feed.list_command.callback(failing)  # type: ignore[arg-type]
    assert blocks(failing)["Example News"] == f"Paused by <@{STRANGER}> · <t:{Clock.t}:R>"

    for number in range(ui.MAX_LOOKUPS + 1):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed(f"Feed {number:02}", "", "", ())
        extra = await env.add(url=url)
        await env.service.pause_feed(SERVER, extra.id, actor=Actor(1000 + number, "Gone"))
    crowded = env.interaction(members=())
    await feed.list_command.callback(crowded)  # type: ignore[arg-type]
    assert crowded.guild.fetched == [] and names(crowded) == ["send_message"]  # type: ignore[union-attr]
    assert crowded.text.count("(left the Server)") == 0 and "<@1005>" in crowded.text


async def test_list_looks_up_only_the_members_on_the_page_shown(env: Env) -> None:
    for number in range(30):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed(f"Feed {number:02}", "", "", ())
        added = await env.add(url=url)
        if number == 0:
            await env.service.pause_feed(SERVER, added.id, actor=SAM)
        if number == 29:
            await env.service.pause_feed(SERVER, added.id, actor=KIM)
    first = env.interaction(members=())
    await feed.list_command.callback(first)  # type: ignore[arg-type]
    assert first.guild.fetched == [STRANGER] and "Feed 29" not in first.text  # type: ignore[union-attr]

    second = env.click(members=())
    await feed.FeedListPage(1).callback(second)  # type: ignore[arg-type]
    assert second.guild.fetched == [KIM.id]  # type: ignore[union-attr]
    assert "Kim (left the Server)" in second.text and "Sam" not in second.text
    assert names(second) == ["defer", "edit_original_response"]


async def test_list_pages_fit_when_pauses_name_long_members(env: Env) -> None:
    for number in range(20):
        url = f"https://example.com/{number}"
        env.web.listings[url] = ParsedFeed("_" * 60, "", "", ())
        added = await env.add(url=url, name="_" * 60)
        await env.service.pause_feed(SERVER, added.id, actor=Actor(1000 + number, "_" * 80))
    seen = 0
    pages = 1
    page = 0
    while page < pages:
        click = env.click(members=())
        await feed.FeedListPage(page).callback(click)  # type: ignore[arg-type]
        assert len(click.text) <= ui.MESSAGE_LIMIT and not click.text.endswith("…")
        assert click.text.count("(left the Server)") == click.text.count("Paused by ")
        pages = int(click.text.rsplit(" ", 1)[1])
        seen += click.text.count("Paused by ")
        page += 1
    assert pages > 1 and seen == 20


async def test_panel_says_who_added_the_feed_and_when(env: Env) -> None:
    found = await env.add()
    interaction = env.interaction()
    await feed.open_panel(interaction, found.id)  # type: ignore[arg-type]
    lines = interaction.text.splitlines()
    assert f"**Added by**: <@{USER}> · <t:{Clock.t}:D>" in lines
    assert lines.index(f"**Added by**: <@{USER}> · <t:{Clock.t}:D>") < lines.index(
        "**Post as**: The bot"
    )

    left = env.interaction(members=(USER,))
    added, _ = await env.service.add_feed(SERVER, TEXT, ChannelKind.MESSAGES, URL2, actor=SAM)
    await feed.open_panel(left, added.id)  # type: ignore[arg-type]
    gone = f"**Added by**: Sam \\*Q\\* (left the Server) · <t:{Clock.t}:D>"
    assert gone in left.text.splitlines()

    env.db._conn.execute("DELETE FROM log_entries")  # a Feed from before Log entries
    old = env.interaction()
    await feed.open_panel(old, found.id)  # type: ignore[arg-type]
    assert "Added by" not in old.text


async def test_the_panel_asks_about_a_left_member_once(env: Env) -> None:
    added, _ = await env.service.add_feed(SERVER, TEXT, ChannelKind.MESSAGES, URL, actor=SAM)
    await env.service.pause_feed(SERVER, added.id, actor=SAM)
    interaction = env.interaction(members=())
    await feed.open_panel(interaction, added.id)  # type: ignore[arg-type]
    assert interaction.guild.fetched == [STRANGER]  # type: ignore[union-attr]
    assert interaction.text.count("(left the Server)") == 2


async def test_these_messages_ping_nobody(env: Env) -> None:
    found = await env.add()
    await env.service.pause_feed(SERVER, found.id, actor=SAM)
    assert ui.NO_MENTIONS.to_dict() == discord.AllowedMentions.none().to_dict()
    assert ui.NO_MENTIONS.to_dict() == {"parse": []}
    sent = [
        (feed.list_command, ()),
        (feed.history_command, (str(found.id),)),
    ]
    for command, args in sent:
        for options in ({}, {"members": (USER,)}):
            interaction = env.interaction(**options)
            await command.callback(interaction, *args)  # type: ignore[arg-type]
            assert interaction.last[1]["allowed_mentions"] is ui.NO_MENTIONS
    panel = env.interaction()
    await feed.open_panel(panel, found.id)  # type: ignore[arg-type]
    assert panel.last[1]["allowed_mentions"] is ui.NO_MENTIONS
    assert f"<@{STRANGER}>" in panel.text  # a real mention, which does not notify


# -- /feed history --


def edit_entry(env: Env, found: Feed, number: int, **fields: Any) -> LogEntry:
    return env.db.add_log_entry(
        server_id=found.server_id,
        at=Clock.t + number,
        actor_id=USER,
        actor_name="Alex",
        kind=LogKind.FEED_EDITED,
        feed_id=found.id,
        feed_name=found.name,
        channel_id=found.channel_id,
        **fields,
    )


async def test_history_shows_the_feeds_log_entries_newest_first(env: Env) -> None:
    other = await env.add(URL2)
    found = await env.add()
    await env.service.pause_feed(SERVER, found.id, actor=SAM)
    await env.service.resume_feed(SERVER, found.id, actor=KIM)
    edit_entry(env, found, 9, changes=[Change("Check interval", "10 minutes", "1 hour")])
    bot_at = pause_by_bot(env, found)
    interaction = env.interaction(**FELLOW)
    await feed.history_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message" and sent["ephemeral"] is True
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    assert sent["content"].splitlines() == [
        "**History of Example News**: 5 Log entries",
        f"<t:{bot_at}:f> the bot paused this Feed · The bot can no longer post in that channel.",
        f"<t:{Clock.t + 9}:f> <@{USER}> edited this Feed · Check interval: 10 minutes → 1 hour",
        f"<t:{Clock.t}:f> <@4> resumed this Feed",
        f"<t:{Clock.t}:f> <@{STRANGER}> paused this Feed",
        f"<t:{Clock.t}:f> <@{USER}> added this Feed",
    ]
    assert "view" not in sent  # one page
    assert "Other Site" not in sent["content"] and other.id != found.id


async def test_history_has_pages_of_ten_that_keep_the_feed(env: Env) -> None:
    found = await env.add()
    for number in range(1, 25):
        edit_entry(env, found, number, detail=f"number {number}")
    interaction = env.interaction()
    await feed.history_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    lines = interaction.text.splitlines()
    assert lines[0] == "**History of Example News**: 25 Log entries"
    assert len(lines) == 12 and lines[-1] == "Page 1 of 3"
    assert lines[1].endswith("number 24") and lines[10].endswith("number 15")
    view = interaction.last[1]["view"]
    assert component_ids(view) == [
        f"rss:c:feed_history_page:{found.id}:0",
        f"rss:c:feed_history_page:{found.id}:1",
    ]

    last = env.click()
    await feed.HistoryPage(found.id, 2).callback(last)  # type: ignore[arg-type]
    assert last.last[0] == "edit_message"
    lines = last.text.splitlines()
    assert lines[-1] == "Page 3 of 3" and len(lines) == 7
    assert lines[-2].endswith("added this Feed")
    assert component_ids(last.last[1]["view"]) == [
        f"rss:c:feed_history_page:{found.id}:1",
        f"rss:c:feed_history_page:{found.id}:2",
    ]
    beyond = env.click()
    await feed.HistoryPage(found.id, 50).callback(beyond)  # type: ignore[arg-type]
    assert beyond.text.splitlines()[-1] == "Page 3 of 3"


async def test_history_of_a_feed_without_log_entries(env: Env) -> None:
    found = await env.add()
    env.db._conn.execute("DELETE FROM log_entries")
    interaction = env.interaction()
    await feed.history_command.callback(interaction, str(found.id))  # type: ignore[arg-type]
    assert interaction.text == "This Feed has no Log entries yet."
    assert "view" not in interaction.last[1]


async def test_history_of_a_removed_feed_is_not_available_by_its_id(env: Env) -> None:
    found = await env.add()
    await env.service.remove_feed(SERVER, found.id, actor=MEMBER)
    with pytest.raises(ui.UserError, match="That Feed no longer exists"):
        await feed.history_command.callback(env.interaction(), str(found.id))  # type: ignore[arg-type]
    click = env.click()
    await feed.HistoryPage(found.id, 0).callback(click)  # type: ignore[arg-type]
    assert click.text == ui.FEED_GONE and click.last[1]["view"] is None


async def test_history_is_for_managers_only(env: Env) -> None:
    found = await env.add()
    stranger = env.interaction(user_id=STRANGER)
    with pytest.raises(ui.UserError, match="Only Managers"):
        await feed.history_command.callback(stranger, str(found.id))  # type: ignore[arg-type]
    assert stranger.calls == []
    click = env.click(user_id=STRANGER)
    await feed.HistoryPage(found.id, 0).callback(click)  # type: ignore[arg-type]
    assert click.text == ui.NEED_MANAGER

    admin = env.interaction(user_id=OWNER)  # an Admin can do all a Manager can
    await feed.history_command.callback(admin, str(found.id))  # type: ignore[arg-type]
    assert "added this Feed" in admin.text


async def test_history_does_not_show_another_servers_feed(env: Env) -> None:
    foreign = await env.add(server_id=OTHER_SERVER)
    with pytest.raises(ui.UserError, match="That Feed no longer exists"):
        await feed.history_command.callback(env.interaction(), str(foreign.id))  # type: ignore[arg-type]
    click = env.click()
    await feed.HistoryPage(foreign.id, 0).callback(click)  # type: ignore[arg-type]
    assert click.text == ui.FEED_GONE
    assert "view" not in click.last[1] or click.last[1]["view"] is None
    # Not even through a Log entry that names the other Server's Feed.
    mine = await env.add(URL2)
    env.db.add_log_entry(
        server_id=OTHER_SERVER,
        at=Clock.t,
        actor_id=USER,
        actor_name="Alex",
        kind=LogKind.FEED_EDITED,
        feed_id=mine.id,
        feed_name="Spoof",
        detail="spoof",
    )
    interaction = env.interaction()
    await feed.history_command.callback(interaction, str(mine.id))  # type: ignore[arg-type]
    assert "spoof" not in interaction.text.lower()


async def test_history_defuses_text_and_fits_the_message(env: Env) -> None:
    found = await env.add()
    hostile = "**b** [x](https://evil.example) @everyone @here <@&1> " + "a" * 3000
    for number in range(1, 12):
        edit_entry(
            env,
            found,
            number,
            detail=hostile,
            changes=[Change("L" * 80, hostile, hostile), Change("Name", "", "")],
        )
    interaction = env.interaction(members=())
    await feed.history_command.callback(interaction, str(found.id))  # type: ignore[arg-type]

    text = interaction.text
    assert len(text) <= ui.MESSAGE_LIMIT and not text.endswith("…")
    for danger in ("@everyone", "@here", "[x](", "<@&1>", "**b**"):
        assert danger not in text
    lines = text.splitlines()
    assert len(lines) == 12  # header, 10 entries, footer
    assert all("edited this Feed" in line for line in lines[1:11])
