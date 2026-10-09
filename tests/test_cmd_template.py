from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import MEMBER, SERVER, USER, FakeInteraction, component_ids

from rssbot.commands import _ui as ui
from rssbot.commands import template
from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import (
    DEFAULT_TEXT_TEMPLATE,
    MAX_BUTTONS,
    MAX_EMBED_FIELDS,
    ButtonSpec,
    ChannelKind,
    EmbedSpec,
    Feed,
    FieldSpec,
    Item,
    Level,
    LogKind,
    ParsedFeed,
    TargetKind,
)
from rssbot.ports import FetchError, FetchResult, ImageData
from rssbot.service import FeedService

OTHER_SERVER = 200
CHANNEL = 500
URL = "https://example.com/feed.xml"
COMPONENT = discord.InteractionType.component
MODAL = discord.InteractionType.modal_submit


# -- fakes --


class Clock:
    def now(self) -> int:
        return 1_700_000_000

    async def sleep(self, seconds: float) -> None:
        pass


def item(key: str, published: int | None = None, **kwargs: Any) -> Item:
    values: dict[str, Any] = {
        "key": key,
        "title": f"Title {key}",
        "link": f"https://example.com/{key}",
        "summary": f"Summary {key}",
        "content": "",
        "author": "",
        "published": published,
        "categories": (),
        "image": "",
    }
    values.update(kwargs)
    return Item(**values)


class Web:
    """What the Feed's address serves. Stands in for the fetcher and the parser."""

    def __init__(self) -> None:
        self.items: tuple[Item, ...] = (item("old", 10), item("new", 20))
        self.error: Exception | None = None
        self.fetches = 0

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return FetchResult(False, b"feed", None, None, url)

    async def fetch_image(self, url: str, *, max_bytes: int = 0) -> ImageData:
        raise FetchError("no images here")

    def parse(self, body: bytes, url: str) -> ParsedFeed:
        return ParsedFeed(
            title="Example News", link="https://example.com/", image="", items=self.items
        )


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def web() -> Web:
    return Web()


@pytest.fixture
def service(db: Database, web: Web) -> FeedService:
    return FeedService(db, web, Clock(), Journal(db, Clock()), parse=web.parse)


@pytest.fixture
def feed(db: Database) -> Feed:
    return make_feed(db)


def make_feed(db: Database, server_id: int = SERVER, **kwargs: Any) -> Feed:
    kwargs.setdefault("name", "News")
    return db.create_feed(
        server_id=server_id,
        channel_id=CHANNEL,
        channel_kind=ChannelKind.MESSAGES,
        url=URL,
        now=1,
        **kwargs,
    )


def button_labels(view: discord.ui.View) -> list[str]:
    return [b["label"] for b in view.to_components()[1]["components"]]


def labels(view: discord.ui.View) -> list[str]:
    return [child.item.label for child in view.children]  # type: ignore[attr-defined]


def member(db: Database, service: FeedService, **kwargs: Any) -> FakeInteraction:
    """An interaction from a member without access, wired to the service."""
    interaction = FakeInteraction(db, **kwargs)
    interaction.client.deps.service = service
    return interaction


def manager(db: Database, service: FeedService, **kwargs: Any) -> FakeInteraction:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    return member(db, service, **kwargs)


def submission(custom_id: str, **values: str | list[str]) -> dict[str, Any]:
    components = []
    for key, value in values.items():
        if isinstance(value, list):
            inner: dict[str, Any] = {"type": 3, "custom_id": key, "values": value}
        else:
            inner = {"type": 4, "custom_id": key, "value": value}
        components.append({"type": 18, "component": inner})
    return {"custom_id": custom_id, "components": components}


async def submit(
    db: Database,
    service: FeedService,
    custom_id: str,
    *,
    from_message: bool = False,
    **values: str | list[str],
) -> FakeInteraction:
    interaction = manager(db, service, type=MODAL, data=submission(custom_id, **values))
    if from_message:
        interaction.message = object()  # type: ignore[attr-defined]
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    return interaction


async def click(interaction: FakeInteraction, control: ui.Action, *picked: str) -> None:
    if picked:
        control.item._values = list(picked)  # type: ignore[attr-defined]
    await control.callback(interaction)  # type: ignore[arg-type]


def form_of(interaction: FakeInteraction) -> dict[str, Any]:
    name, sent = interaction.last
    assert name == "send_modal"
    assert interaction.calls == [interaction.last]  # the form is the first response
    return sent["modal"].to_dict()


def boxes(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["component"]["custom_id"]: {**c["component"], "label": c} for c in form["components"]}


def check_form_limits(form: dict[str, Any]) -> None:
    assert 1 <= len(form["title"]) <= 45
    assert 1 <= len(form["components"]) <= 5
    for component in form["components"]:
        assert 1 <= len(component["label"]) <= 45
        assert len(component.get("description") or "") <= 100


def stored(db: Database, feed: Feed) -> Feed:
    found = db.get_feed(feed.id)
    assert found is not None
    return found


def private(interaction: FakeInteraction) -> None:
    for name, sent in interaction.calls:
        if name in ("send_message", "followup"):
            assert sent["ephemeral"] is True
        if name == "defer":
            continue
        if name != "send_modal":
            assert sent["allowed_mentions"] is ui.NO_MENTIONS


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for option in node.get("options", []):
        yield from _walk(option)


# -- registration --


def test_tree_serialises_within_discords_limits() -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = app_commands.CommandTree(client)
    template.register(tree)

    (command,) = tree.get_commands()
    payload = command.to_dict(tree)
    assert payload["name"] == "template"
    assert payload["contexts"] == [0]  # Servers only
    assert payload.get("default_member_permissions") is None
    assert [o["name"] for o in payload["options"]] == [
        "text",
        "embed",
        "fields",
        "buttons",
        "reset",
        "placeholders",
    ]
    for node in _walk(payload):
        assert 1 <= len(node["name"]) <= 32
        assert 1 <= len(node["description"]) <= 100
        assert len(node.get("options", [])) <= 25
    for sub in payload["options"]:
        assert sub["type"] == 1
        feed = sub["options"][0]
        assert (feed["name"], feed["type"], feed["autocomplete"], feed["required"]) == (
            "feed",
            3,
            True,
            True,
        )
    colour = payload["options"][1]["options"][1]
    assert (colour["name"], colour["type"], colour["required"]) == ("colour", 3, False)


def test_every_action_and_form_is_prefixed() -> None:
    actions = {n for n, cls in ui._ACTIONS.items() if cls.__module__ == template.__name__}
    forms = {n for n, f in ui._FORMS.items() if f.handler.__module__ == template.__name__}
    assert actions == {
        "tpl_back",
        "tpl_text",
        "tpl_embed",
        "tpl_colour",
        "tpl_embed_time",
        "tpl_embed_remove",
        "tpl_embed_remove_yes",
        "tpl_embed_keep",
        "tpl_fields",
        "tpl_field_add",
        "tpl_field_remove",
        "tpl_buttons",
        "tpl_button_add",
        "tpl_button_remove",
        "tpl_reset",
    }
    assert forms == {"tpl_text", "tpl_embed", "tpl_field", "tpl_button"}
    assert all(name.startswith("tpl_") for name in actions | forms)


# -- access --


@pytest.mark.parametrize(
    "command",
    [
        template.text_command,
        template.embed_command,
        template.fields_command,
        template.buttons_command,
        template.reset_command,
        template.placeholders_command,
    ],
)
async def test_commands_are_refused_for_a_non_manager(
    db: Database, service: FeedService, feed: Feed, command: Any
) -> None:
    interaction = member(db, service)
    with pytest.raises(ui.UserError, match="Only Managers"):
        await command.callback(interaction, feed=str(feed.id))
    assert interaction.calls == []


@pytest.mark.parametrize(
    "opener",
    [template.open_text, template.open_embed, template.open_fields, template.open_buttons],
)
async def test_openers_recheck_access_and_the_server(
    db: Database, service: FeedService, feed: Feed, opener: Any
) -> None:
    with pytest.raises(ui.UserError, match="Only Managers"):
        await opener(member(db, service), feed.id)
    foreign = make_feed(db, OTHER_SERVER)
    with pytest.raises(ui.UserError, match="no longer exists"):
        await opener(manager(db, service), foreign.id)


@pytest.mark.parametrize(
    "control",
    [
        template.EditText,
        template.OpenFields,
        template.AddField,
        template.RemoveField,
        template.OpenButtons,
        template.AddButton,
        template.RemoveButton,
        template.ColourSelect,
        template.RemoveEmbed,
        template.ToggleTimestamp,
        template.RemoveEmbedConfirmed,
        template.KeepEmbed,
        template.ResetTemplate,
        template.BackToFeed,
    ],
)
async def test_controls_are_refused_for_a_non_manager_and_for_another_server(
    db: Database, service: FeedService, control: type[ui.Action]
) -> None:
    spec = EmbedSpec(title="T", fields=(FieldSpec("a", "b"),))
    button = ButtonSpec("Read", "{{link}}")
    feed = make_feed(db, text_template="mine", embed=spec, buttons=(button,))
    interaction = member(db, service, type=COMPONENT)
    await click(interaction, control(feed.id), "1")
    assert interaction.calls == [
        (
            "send_message",
            {"content": ui.NEED_MANAGER, "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]

    foreign = make_feed(db, OTHER_SERVER, text_template="theirs", embed=spec, buttons=(button,))
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, control(foreign.id), "1")
    assert interaction.last[0] == "send_message"
    assert interaction.text == ui.FEED_GONE
    assert stored(db, feed) == feed
    assert stored(db, foreign) == foreign


async def test_embed_button_is_refused_like_the_others(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = member(db, service, type=COMPONENT)
    await click(interaction, template.EditEmbed(feed.id, 0))
    assert interaction.text == ui.NEED_MANAGER
    foreign = make_feed(db, OTHER_SERVER)
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.EditEmbed(foreign.id, 0))
    assert interaction.text == ui.FEED_GONE


@pytest.mark.parametrize(
    ("custom_id", "values"),
    [
        ("rss:m:tpl_text:{}", {"text": "hacked"}),
        ("rss:m:tpl_embed:{}:0", {"title": "hacked"}),
        ("rss:m:tpl_field:{}", {"name": "hacked", "value": "x", "inline": ["no"]}),
        ("rss:m:tpl_button:{}", {"label": "hacked", "url": "https://example.com"}),
    ],
)
async def test_forms_are_refused_for_a_non_manager_and_for_another_server(
    db: Database, service: FeedService, feed: Feed, custom_id: str, values: dict[str, Any]
) -> None:
    data = submission(custom_id.format(feed.id), **values)
    interaction = member(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert interaction.text == ui.NEED_MANAGER
    assert stored(db, feed) == feed

    foreign = make_feed(db, OTHER_SERVER)
    interaction = await submit(db, service, custom_id.format(foreign.id), **values)
    assert interaction.text == ui.FEED_GONE
    assert stored(db, foreign) == foreign


# -- text --


async def test_text_command_opens_a_prefilled_form(db: Database, service: FeedService) -> None:
    feed = make_feed(db, text_template="Hello {{title}}")
    interaction = manager(db, service)
    await template.text_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    form = form_of(interaction)
    check_form_limits(form)
    assert form["custom_id"] == f"rss:m:tpl_text:{feed.id}"
    box = boxes(form)["text"]
    assert box["value"] == "Hello {{title}}"
    assert (box["style"], box["max_length"], box["required"]) == (2, 2000, False)
    assert "{{title}}" in box["label"]["description"]


async def test_text_button_opens_the_same_form(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.EditText(feed.id))
    assert boxes(form_of(interaction))["text"]["value"] == DEFAULT_TEXT_TEMPLATE


async def test_saving_text_confirms_with_a_preview(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_text:{feed.id}", text="  New: {{title}} {{url}}  "
    )

    assert stored(db, feed).text_template == "New: {{title}} {{url}}"
    assert [name for name, _ in interaction.calls] == ["defer", "followup"]
    assert interaction.calls[0][1] == {"ephemeral": True, "thinking": True}
    private(interaction)
    assert "Saved the message text of **News**." in interaction.text
    assert "New: Title new https://example.com/new" in interaction.text  # the newest Item
    sent = interaction.last[1]
    assert "embed" not in sent
    assert component_ids(sent["view"]) == [
        f"rss:c:tpl_text:{feed.id}",
        f"rss:c:tpl_back:{feed.id}",
    ]
    assert labels(sent["view"]) == ["Edit again", "Back to Feed"]
    assert sent["view"].timeout is None


async def test_saving_text_from_a_message_replaces_that_message(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_text:{feed.id}", from_message=True, text="{{title}}"
    )
    assert [name for name, _ in interaction.calls] == ["defer", "edit_original_response"]
    assert interaction.calls[0][1] == {}  # a silent defer
    assert "Title new" in interaction.text


async def test_preview_shows_the_embed_and_link_buttons_under_the_controls(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(
        db,
        embed=EmbedSpec(title="{{title}}", colour=0x3498DB),
        buttons=(ButtonSpec("Read", "{{link}}"),),
    )
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")

    sent = interaction.last[1]
    assert sent["embed"].title == "Title new"
    rows = sent["view"].to_components()
    assert [len(row["components"]) for row in rows] == [2, 1]
    assert rows[1]["components"][0]["url"] == "https://example.com/new"
    assert len(sent["content"]) <= 2000


async def test_a_long_preview_still_fits(db: Database, service: FeedService, web: Web) -> None:
    web.items = (item("big", 5, title="x" * 3000),)
    feed = make_feed(db)
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")
    assert len(interaction.text) == 2000
    assert interaction.text.startswith("Saved the message text")


async def test_text_is_saved_even_if_the_preview_fails(
    db: Database, service: FeedService, feed: Feed, web: Web
) -> None:
    web.error = FetchError("The address answered with error 503.")
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")

    assert stored(db, feed).text_template == "{{title}}"
    assert "Saved the message text" in interaction.text
    assert (
        "The preview could not be shown: The address answered with error 503." in interaction.text
    )
    assert len(interaction.last[1]["view"].children) == 2


async def test_text_is_confirmed_even_if_discord_refuses_the_preview(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(
        db, service, type=MODAL, data=submission(f"rss:m:tpl_text:{feed.id}", text="{{title}}")
    )
    sends: list[dict[str, Any]] = []

    async def followup(**kwargs: Any) -> None:
        sends.append(kwargs)
        if len(sends) == 1:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "no")  # type: ignore[arg-type]

    interaction.followup = SimpleNamespace(send=followup)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]

    assert len(sends) == 2
    assert sends[1]["content"].startswith("Saved the message text of **News**.")
    assert "Discord refused it" in sends[1]["content"]
    assert len(sends[1]["view"].children) == 2


async def test_unknown_placeholder_shows_the_error_with_what_was_sent(
    db: Database, service: FeedService, feed: Feed, web: Web
) -> None:
    typed = "Breaking: {{titel}}\n```py\ncode\n```"
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text=typed)

    assert stored(db, feed).text_template == DEFAULT_TEXT_TEMPLATE
    assert web.fetches == 0
    assert interaction.last[0] == "send_message"
    private(interaction)
    text = interaction.text
    assert '"titel" in {{titel}} is not a Placeholder name' in text
    assert "Nothing was saved." in text
    assert "**Message text**\n```\nBreaking: {{titel}}\n" in text
    assert text.count("```") == 2  # what was typed cannot close the code block
    assert labels(interaction.last[1]["view"]) == ["Edit again", "Back to Feed"]


async def test_a_long_rejected_text_is_cut_to_fit(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_text:{feed.id}", text="{{nope}}" + "y" * 1990
    )
    assert len(interaction.text) == 2000
    assert interaction.text.endswith("…\n```")
    assert (
        "Nothing was saved. What you sent did not fit here completely, "
        "so only the beginning is shown:\n" in interaction.text
    )


async def test_a_rejected_text_that_fits_is_not_called_incomplete(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{nope}} short")
    assert "This is what you sent, so that you can copy it:" in interaction.text
    assert "did not fit" not in interaction.text
    assert "{{nope}} short\n```" in interaction.text


async def test_a_rejected_embed_says_when_a_box_is_shown_only_in_part(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db,
        service,
        f"rss:m:tpl_embed:{feed.id}:{template.KEEP_COLOUR}",
        title="{{titel}}",
        description="d" * 3000,
        url="",
        image="",
        footer="The end",
    )
    assert stored(db, feed).embed is None
    assert len(interaction.text) <= 2000
    assert "so only the beginning is shown:" in interaction.text
    assert "**Title**\n```\n{{titel}}\n```" in interaction.text
    assert "**Footer**\n```\nThe end\n```" in interaction.text
    assert "ddd…\n```" in interaction.text


async def test_empty_text_is_saved_and_offers_an_embed(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="   ")
    assert db.get_feed(feed.id).text_template == ""
    assert "Until it has an Embed, the default message text is posted." in interaction.text
    assert "Add Embed" in [item.item.label for item in interaction.last[1]["view"].children]


async def test_empty_text_with_an_embed_posts_only_the_embed(
    db: Database, service: FeedService, feed: Feed
) -> None:
    await service.set_embed(SERVER, feed.id, title="{{title}}", actor=MEMBER)
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="")
    assert "only its Embed is posted" in interaction.text
    message, _ = await service.preview(SERVER, feed.id)
    assert message.content == "" and message.embed is not None


# -- embed --

EMBED = EmbedSpec(
    title="{{title}}",
    description="{{description:200}}",
    url="{{link}}",
    image="{{image}}",
    footer="{{feed_title}}",
    colour=0x112233,
)


async def test_embed_command_opens_a_prefilled_form(db: Database, service: FeedService) -> None:
    feed = make_feed(db, embed=EMBED)
    interaction = manager(db, service)
    await template.embed_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    form = form_of(interaction)
    check_form_limits(form)
    assert form["custom_id"] == f"rss:m:tpl_embed:{feed.id}:0"
    found = boxes(form)
    assert list(found) == ["title", "description", "url", "image", "footer"]
    assert [found[key].get("value") for key in found] == [
        "{{title}}",
        "{{description:200}}",
        "{{link}}",
        "{{image}}",
        "{{feed_title}}",
    ]
    assert [found[key]["label"]["label"] for key in found] == [
        "Title",
        "Description",
        "Link",
        "Image",
        "Footer",
    ]
    assert found["description"]["style"] == 2
    assert all(box["required"] is False for box in found.values())


async def test_embed_form_is_empty_for_a_feed_without_an_embed(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.EditEmbed(feed.id, 0))
    form = form_of(interaction)
    assert all(box.get("value") is None for box in boxes(form).values())


@pytest.mark.parametrize(
    ("option", "code"),
    [(None, 0), ("none", 1), (" None ", 1), ("#ff8800", 0xFF8800 + 2), ("000000", 2)],
)
async def test_colour_option_travels_in_the_form_id(
    db: Database, service: FeedService, feed: Feed, option: str | None, code: int
) -> None:
    interaction = manager(db, service)
    await template.embed_command.callback(interaction, feed=str(feed.id), colour=option)  # type: ignore[arg-type]
    assert form_of(interaction)["custom_id"] == f"rss:m:tpl_embed:{feed.id}:{code}"
    assert stored(db, feed) == feed  # nothing changes until the form is sent


@pytest.mark.parametrize("option", ["orange", "#ff88", "#gg8800", "", "#"])
async def test_bad_colour_option_is_refused(
    db: Database, service: FeedService, feed: Feed, option: str
) -> None:
    interaction = manager(db, service)
    with pytest.raises(ui.UserError, match="hex code such as #ff8800"):
        await template.embed_command.callback(interaction, feed=str(feed.id), colour=option)  # type: ignore[arg-type]
    assert interaction.calls == []


@pytest.mark.parametrize(
    ("code", "expected"), [(0, 0x112233), (0xFF8800 + 2, 0xFF8800), (2, 0), (1, None)]
)
async def test_saving_the_embed_keeps_changes_or_clears_the_colour(
    db: Database, service: FeedService, code: int, expected: int | None
) -> None:
    feed = make_feed(db, embed=EMBED)
    interaction = await submit(
        db,
        service,
        f"rss:m:tpl_embed:{feed.id}:{code}",
        title="{{title}}!",
        description="",
        url="{{link}}",
        image="",
        footer="By {{author||feed_title}}",
    )

    assert stored(db, feed).embed == EmbedSpec(
        title="{{title}}!",
        url="{{link}}",
        footer="By {{author||feed_title}}",
        colour=expected,
    )
    assert [name for name, _ in interaction.calls] == ["defer", "followup"]
    private(interaction)
    sent = interaction.last[1]
    assert "Saved the Embed of **News**." in sent["content"]
    assert sent["embed"].title == "Title new!"
    assert sent["embed"].footer.text == "By News"
    assert component_ids(sent["view"]) == [
        f"rss:c:tpl_colour:{feed.id}",
        f"rss:c:tpl_embed:{feed.id}:0",
        f"rss:c:tpl_fields:{feed.id}",
        f"rss:c:tpl_embed_time:{feed.id}",
        f"rss:c:tpl_embed_remove:{feed.id}",
        f"rss:c:tpl_back:{feed.id}",
    ]
    select, row = sent["view"].to_components()
    assert [b["label"] for b in row["components"]] == [
        "Edit again",
        "Fields",
        "Append post date to footer (local time): on",
        "Remove Embed",
        "Back to Feed",
    ]
    options = select["components"][0]["options"]
    assert [o["label"] for o in options] == [
        "Red",
        "Orange",
        "Yellow",
        "Green",
        "Teal",
        "Blue",
        "Purple",
        "Pink",
        "Grey",
        "Dark",
        "No colour",
    ]
    assert [o["label"] for o in options if o.get("default")] == (
        ["No colour"] if expected is None else []
    )
    if expected == 0xFF8800:
        assert select["components"][0]["placeholder"] == "Colour: #ff8800"


async def test_an_out_of_range_colour_in_the_form_id_is_refused(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, embed=EMBED)
    code = 0xFFFFFF + 3
    interaction = await submit(db, service, f"rss:m:tpl_embed:{feed.id}:{code}", title="T")
    assert stored(db, feed).embed == EMBED
    assert "The colour must be a hex code" in interaction.text


async def test_saving_an_empty_embed_form_means_no_embed(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, embed=EMBED)
    interaction = await submit(
        db,
        service,
        f"rss:m:tpl_embed:{feed.id}:{0xFF8800 + 2}",
        title="",
        description=" ",
        url="",
        image="",
        footer="",
    )

    assert stored(db, feed).embed is None
    sent = interaction.last[1]
    assert "**News** has no Embed now: every box was left empty." in sent["content"]
    assert "embed" not in sent
    assert component_ids(sent["view"]) == [
        f"rss:c:tpl_embed:{feed.id}:0",
        f"rss:c:tpl_back:{feed.id}",
    ]


async def test_an_empty_embed_form_keeps_an_embed_that_has_fields(
    db: Database, service: FeedService
) -> None:
    fields = (FieldSpec("Author", "{{author}}"),)
    feed = make_feed(db, embed=EmbedSpec(title="T", colour=5, fields=fields))
    interaction = await submit(db, service, f"rss:m:tpl_embed:{feed.id}:0", title="")

    assert stored(db, feed).embed == EmbedSpec(colour=5, fields=fields)
    assert "Saved the Embed" in interaction.text


async def test_rejected_embed_shows_every_box_that_was_filled(
    db: Database, service: FeedService, feed: Feed
) -> None:
    code = 0xFF8800 + 2
    interaction = await submit(
        db,
        service,
        f"rss:m:tpl_embed:{feed.id}:{code}",
        title="{{headline}}",
        description="d" * 4000,
        url="{{link}}",
        image="",
        footer="f" * 30,
    )

    assert stored(db, feed).embed is None
    text = interaction.text
    assert len(text) <= 2000
    assert '"headline" in {{headline}} is not a Placeholder name' in text
    assert "**Title**\n```\n{{headline}}\n```" in text
    assert "**Link**\n```\n{{link}}\n```" in text
    assert "**Footer**\n```\n" + "f" * 30 + "\n```" in text  # short boxes are shown whole
    assert "**Description**\n```\ndddd" in text
    assert "**Image**" not in text
    # Edit again keeps the colour option.
    assert component_ids(interaction.last[1]["view"]) == [
        f"rss:c:tpl_embed:{feed.id}:{code}",
        f"rss:c:tpl_back:{feed.id}",
    ]


async def test_colour_select_applies_at_once(db: Database, service: FeedService) -> None:
    feed = make_feed(db, embed=EMBED)
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.ColourSelect(feed.id), "2ecc71")

    assert stored(db, feed).embed == EmbedSpec(**{**_as_dict(EMBED), "colour": 0x2ECC71})
    assert [name for name, _ in interaction.calls] == ["defer", "edit_original_response"]
    sent = interaction.last[1]
    assert "Set the colour of the Embed of **News** to Green." in sent["content"]
    assert sent["embed"].colour.value == 0x2ECC71
    options = sent["view"].to_components()[0]["components"][0]["options"]
    assert [o["label"] for o in options if o.get("default")] == ["Green"]

    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.ColourSelect(feed.id), "none")
    assert stored(db, feed).embed.colour is None  # type: ignore[union-attr]
    assert "Removed the colour of the Embed of" in interaction.text


def _as_dict(spec: EmbedSpec) -> dict[str, Any]:
    return {name: getattr(spec, name) for name in spec.__slots__}


@pytest.mark.parametrize("picked", ["ff8800", "zzzzzz", ""])
async def test_colour_select_takes_only_its_own_options(
    db: Database, service: FeedService, picked: str
) -> None:
    feed = make_feed(db, embed=EMBED)
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.ColourSelect(feed.id), picked)
    assert stored(db, feed).embed == EMBED
    assert interaction.text == "Choose a colour from the list."


async def test_colour_select_does_not_make_an_embed(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.ColourSelect(feed.id), "2ecc71")
    assert stored(db, feed).embed is None
    assert "has no Embed" in interaction.text


async def test_timestamp_button_flips_the_post_date_and_says_so(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, embed=EmbedSpec(title="T"))
    assert stored(db, feed).embed.timestamp is True  # on by default

    off = manager(db, service, type=COMPONENT)
    await click(off, template.ToggleTimestamp(feed.id))
    assert stored(db, feed).embed.timestamp is False
    assert "no longer shows the post date" in off.text
    assert "Append post date to footer (local time): off" in button_labels(off.last[1]["view"])

    on = manager(db, service, type=COMPONENT)
    await click(on, template.ToggleTimestamp(feed.id))
    assert stored(db, feed).embed.timestamp is True
    assert "Append post date to footer (local time): on" in button_labels(on.last[1]["view"])


async def test_remove_embed_asks_in_place_then_removes(db: Database, service: FeedService) -> None:
    fields = (FieldSpec("a", "b"), FieldSpec("c", "d"))
    feed = make_feed(db, embed=EmbedSpec(title="T", fields=fields))
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveEmbed(feed.id))

    assert stored(db, feed).embed is not None
    # The question replaces the Embed screen: no second message, no preview left above it.
    assert [name for name, _ in interaction.calls] == ["edit_message"]
    private(interaction)
    assert interaction.text == "Remove the Embed of **News** with its 2 Fields?"
    assert interaction.last[1]["embed"] is None
    view = interaction.last[1]["view"]
    assert component_ids(view) == [
        f"rss:c:tpl_embed_remove_yes:{feed.id}",
        f"rss:c:tpl_embed_keep:{feed.id}",
    ]
    assert labels(view) == ["Remove Embed", "Cancel"]
    assert view.children[0].item.style is discord.ButtonStyle.danger

    confirm = manager(db, service, type=COMPONENT)
    await click(confirm, template.RemoveEmbedConfirmed(feed.id))
    assert stored(db, feed).embed is None
    assert [name for name, _ in confirm.calls] == ["edit_message"]
    assert confirm.text == "Removed the Embed of **News**."
    assert confirm.last[1]["embed"] is None
    assert component_ids(confirm.last[1]["view"]) == [
        f"rss:c:tpl_embed:{feed.id}:0",
        f"rss:c:tpl_back:{feed.id}",
    ]
    assert labels(confirm.last[1]["view"]) == ["Add Embed", "Back to Feed"]


async def test_cancelling_the_removal_returns_to_the_embed_screen(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, embed=EMBED)
    asked = manager(db, service, type=COMPONENT)
    await click(asked, template.RemoveEmbed(feed.id))
    cancel = asked.last[1]["view"].children[1]
    assert cancel.item.label == "Cancel"

    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, type(cancel)(feed.id))

    assert stored(db, feed).embed == EMBED
    assert [name for name, _ in interaction.calls] == ["defer", "edit_original_response"]
    sent = interaction.last[1]
    assert sent["content"].startswith("Kept the Embed of **News**.\n**Preview** of the newest Item")
    assert sent["embed"] is not None
    assert component_ids(sent["view"]) == [
        f"rss:c:tpl_colour:{feed.id}",
        f"rss:c:tpl_embed:{feed.id}:0",
        f"rss:c:tpl_fields:{feed.id}",
        f"rss:c:tpl_embed_time:{feed.id}",
        f"rss:c:tpl_embed_remove:{feed.id}",
        f"rss:c:tpl_back:{feed.id}",
    ]
    assert [b["label"] for b in sent["view"].to_components()[1]["components"]] == [
        "Edit again",
        "Fields",
        "Append post date to footer (local time): on",
        "Remove Embed",
        "Back to Feed",
    ]


async def test_cancelling_the_removal_of_an_embed_already_gone_says_so(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.KeepEmbed(feed.id))

    assert "**News** has no Embed." in interaction.text
    assert labels(interaction.last[1]["view"]) == ["Edit again", "Back to Feed"]


# -- fields --


async def test_fields_command_lists_none(db: Database, service: FeedService, feed: Feed) -> None:
    interaction = manager(db, service)
    await template.fields_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    assert interaction.last[0] == "send_message"
    private(interaction)
    assert "**Fields of the Embed of **News**** (0 of 25)" in interaction.text
    assert "There are no Fields yet. Adding one makes the Embed." in interaction.text
    view = interaction.last[1]["view"]
    assert component_ids(view) == [
        f"rss:c:tpl_field_add:{feed.id}",
        f"rss:c:tpl_embed:{feed.id}:0",
        f"rss:c:tpl_back:{feed.id}",
    ]
    assert view.children[0].item.disabled is False


async def test_fields_list_shows_each_field(db: Database, service: FeedService) -> None:
    fields = (FieldSpec("Author", "{{author}}", True), FieldSpec("About", "line one\nline `two`"))
    feed = make_feed(db, embed=EmbedSpec(fields=fields))
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.OpenFields(feed.id))

    assert interaction.last[0] == "edit_message"
    assert "1. `Author`: `{{author}}` (side by side)" in interaction.text
    assert "2. `About`: `line one line 'two'`" in interaction.text
    view = interaction.last[1]["view"]
    assert component_ids(view)[-1] == f"rss:c:tpl_field_remove:{feed.id}"
    options = view.to_components()[1]["components"][0]["options"]
    assert [(o["label"], o["description"]) for o in options] == [
        ("1. Author", "{{author}}"),
        ("2. About", "line one line `two`"),
    ]
    # The value carries the position and a checksum of the Field it stood for.
    assert [o["value"].split(":")[0] for o in options] == ["1", "2"]
    assert all(len(o["value"]) == len("1:89abcdef") for o in options)


async def test_add_field_opens_a_form(db: Database, service: FeedService, feed: Feed) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.AddField(feed.id))

    form = form_of(interaction)
    check_form_limits(form)
    assert form["custom_id"] == f"rss:m:tpl_field:{feed.id}"
    found = boxes(form)
    assert list(found) == ["name", "value", "inline"]
    assert (found["name"]["max_length"], found["value"]["max_length"]) == (256, 1024)
    assert found["value"]["style"] == 2
    assert [(o["label"], o["default"]) for o in found["inline"]["options"]] == [
        ("No", True),
        ("Yes", False),
    ]


@pytest.mark.parametrize(("choice", "inline"), [("yes", True), ("no", False)])
async def test_adding_a_field_shows_the_list_again(
    db: Database, service: FeedService, feed: Feed, choice: str, inline: bool
) -> None:
    interaction = await submit(
        db,
        service,
        f"rss:m:tpl_field:{feed.id}",
        from_message=True,
        name="Author",
        value="{{author}}",
        inline=[choice],
    )

    assert stored(db, feed).embed == EmbedSpec(fields=(FieldSpec("Author", "{{author}}", inline),))
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("Added Field 1.\n")
    assert "(1 of 25)" in interaction.text
    assert ("(side by side)" in interaction.text) is inline


async def test_rejected_field_shows_what_was_sent(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_field:{feed.id}", name="Who", value="{{writer}}", inline=["no"]
    )
    assert stored(db, feed).embed is None
    assert "is not a Placeholder name" in interaction.text
    assert "**Name**\n```\nWho\n```" in interaction.text
    assert "**Value**\n```\n{{writer}}\n```" in interaction.text
    assert component_ids(interaction.last[1]["view"]) == [
        f"rss:c:tpl_field_add:{feed.id}",
        f"rss:c:tpl_fields:{feed.id}",
    ]


async def test_removing_a_field_shows_the_list_again(db: Database, service: FeedService) -> None:
    fields = (FieldSpec("a", "1"), FieldSpec("b", "2"), FieldSpec("c", "3"))
    feed = make_feed(db, embed=EmbedSpec(title="T", fields=fields))
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveField(feed.id), option_values(feed, "fields")[1])

    assert stored(db, feed).embed == EmbedSpec(title="T", fields=(fields[0], fields[2]))
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("Removed Field 2.\n")
    assert "2. `c`: `3`" in interaction.text


def option_values(feed: Feed, kind: str) -> list[str]:
    """The option values of the remove select on the Fields or Buttons panel."""
    panel = template._fields_panel if kind == "fields" else template._buttons_panel
    _, view = panel(feed)
    options = view.to_components()[1]["components"][0]["options"]
    assert all(len(option["value"]) <= 100 for option in options)
    assert len({option["value"] for option in options}) == len(options)
    return [option["value"] for option in options]


async def test_removing_a_field_from_a_stale_list_removes_nothing(
    db: Database, service: FeedService
) -> None:
    fields = (FieldSpec("Author", "{{author}}"), FieldSpec("Date", "x"), FieldSpec("Tags", "y"))
    feed = make_feed(db, embed=EmbedSpec(fields=fields))
    stale = option_values(feed, "fields")[1]  # "2. Date" on a panel shown before the change
    await service.remove_field(
        feed.server_id, feed.id, 1, actor=MEMBER
    )  # another Manager removes "Author"
    changed = stored(db, feed)

    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveField(feed.id), stale)

    assert stored(db, feed) == changed  # "Tags", now number 2, is still there
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("The list has changed. Choose again.\n")
    assert "1. `Date`: `x`" in interaction.text
    assert "2. `Tags`: `y`" in interaction.text
    options = interaction.last[1]["view"].to_components()[1]["components"][0]["options"]
    assert [option["value"] for option in options] == option_values(changed, "fields")


async def test_removing_the_same_field_still_works_after_another_was_added(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, embed=EmbedSpec(fields=(FieldSpec("a", "1"), FieldSpec("b", "2"))))
    picked = option_values(feed, "fields")[1]
    await service.add_field(feed.server_id, feed.id, "c", "3", actor=MEMBER)

    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveField(feed.id), picked)
    assert stored(db, feed).embed == EmbedSpec(fields=(FieldSpec("a", "1"), FieldSpec("c", "3")))
    assert interaction.text.startswith("Removed Field 2.\n")


@pytest.mark.parametrize("picked", ["9:00000000", "0:00000000", "1:00000000", "1", "9"])
async def test_removing_a_field_that_is_not_the_one_listed(
    db: Database, service: FeedService, picked: str
) -> None:
    feed = make_feed(db, embed=EmbedSpec(fields=(FieldSpec("a", "1"),)))
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveField(feed.id), picked)
    assert stored(db, feed) == feed
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("The list has changed. Choose again.\n")


@pytest.mark.parametrize("picked", ["x", "x:1", ":1", ""])
async def test_removing_a_field_with_a_value_that_is_no_option(
    db: Database, service: FeedService, picked: str
) -> None:
    feed = make_feed(db, embed=EmbedSpec(fields=(FieldSpec("a", "1"),)))
    interaction = manager(db, service, type=COMPONENT)
    control = template.RemoveField(feed.id)
    control.item._values = [picked]  # type: ignore[attr-defined]
    await control.callback(interaction)  # type: ignore[arg-type]
    assert stored(db, feed) == feed
    assert interaction.last[0] == "send_message"
    assert interaction.text == "Choose the Field to remove."


async def test_field_limit(db: Database, service: FeedService) -> None:
    fields = tuple(FieldSpec(f"name {n} " + "n" * 250, "v" * 1024) for n in range(MAX_EMBED_FIELDS))
    feed = make_feed(db, embed=EmbedSpec(fields=fields))
    interaction = manager(db, service)
    await template.fields_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert len(sent["content"]) <= 2000
    assert "(25 of 25)" in sent["content"]
    assert "25. `name 24" in sent["content"]
    assert sent["content"].endswith("The limit of 25 Fields is reached. Remove one to add another.")
    assert sent["view"].children[0].item.disabled is True
    assert len(sent["view"].to_components()[1]["components"][0]["options"]) == 25

    # A disabled button can still be sent by hand, and so can the form.
    click_it = manager(db, service, type=COMPONENT)
    await click(click_it, template.AddField(feed.id))
    assert click_it.text == "An Embed can have at most 25 Fields."
    sent_form = await submit(
        db, service, f"rss:m:tpl_field:{feed.id}", name="one", value="more", inline=["no"]
    )
    assert "An Embed can have at most 25 Fields." in sent_form.text
    assert stored(db, feed) == feed


# -- buttons --


async def test_buttons_command_lists_them(db: Database, service: FeedService) -> None:
    buttons = (ButtonSpec("Read", "{{link}}"), ButtonSpec("Site", "https://example.com/"))
    feed = make_feed(db, buttons=buttons)
    interaction = manager(db, service)
    await template.buttons_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    private(interaction)
    assert "**Buttons of **News**** (2 of 5)" in interaction.text
    assert "1. `Read` opens `{{link}}`" in interaction.text
    assert "2. `Site` opens `https://example.com/`" in interaction.text
    view = interaction.last[1]["view"]
    assert component_ids(view) == [
        f"rss:c:tpl_button_add:{feed.id}",
        f"rss:c:tpl_back:{feed.id}",
        f"rss:c:tpl_button_remove:{feed.id}",
    ]
    options = view.to_components()[1]["components"][0]["options"]
    assert [(o["label"], o["description"]) for o in options] == [
        ("1. Read", "{{link}}"),
        ("2. Site", "https://example.com/"),
    ]
    # The value carries the position and a checksum of the Button it stood for.
    assert [o["value"].split(":")[0] for o in options] == ["1", "2"]
    assert all(len(o["value"]) == len("1:89abcdef") for o in options)


async def test_buttons_list_when_there_are_none(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.OpenButtons(feed.id))
    assert interaction.last[0] == "edit_message"
    assert "There are no Buttons yet." in interaction.text
    assert len(interaction.last[1]["view"].children) == 2


async def test_add_button_opens_a_form(db: Database, service: FeedService, feed: Feed) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.AddButton(feed.id))

    form = form_of(interaction)
    check_form_limits(form)
    assert form["custom_id"] == f"rss:m:tpl_button:{feed.id}"
    found = boxes(form)
    assert [found[key]["label"]["label"] for key in found] == ["Label", "Address"]
    assert found["label"].get("value") is None
    assert found["url"]["value"] == "{{link}}"
    assert "{{link}} is the Item's address" in found["url"]["label"]["description"]
    assert (found["label"]["max_length"], found["url"]["max_length"]) == (80, 512)


async def test_adding_and_removing_a_button(db: Database, service: FeedService, feed: Feed) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", from_message=True, label="Read", url="{{link}}"
    )
    assert stored(db, feed).buttons == (ButtonSpec("Read", "{{link}}"),)
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("Added Button 1.\n")

    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="Site", url="https://example.com/"
    )
    assert interaction.last[0] == "send_message"  # not opened from a message: a new reply
    assert "(2 of 5)" in interaction.text

    interaction = manager(db, service, type=COMPONENT)
    stale = option_values(stored(db, feed), "buttons")
    await click(interaction, template.RemoveButton(feed.id), stale[0])
    assert stored(db, feed).buttons == (ButtonSpec("Site", "https://example.com/"),)
    assert interaction.last[0] == "edit_message"
    assert interaction.text.startswith("Removed Button 1.\n")
    assert "1. `Site` opens" in interaction.text

    # The old panel still offers "1. Read"; number 1 is "Site" now and must stay.
    for picked in (stale[0], "1", "7:00000000"):
        interaction = manager(db, service, type=COMPONENT)
        await click(interaction, template.RemoveButton(feed.id), picked)
        assert stored(db, feed).buttons == (ButtonSpec("Site", "https://example.com/"),)
        assert interaction.last[0] == "edit_message"
        assert interaction.text.startswith("The list has changed. Choose again.\n")
        assert "1. `Site` opens" in interaction.text

    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.RemoveButton(feed.id), "x")
    assert interaction.text == "Choose the Button to remove."


async def test_rejected_button_shows_what_was_sent(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="Read", url="example.com/page"
    )
    assert stored(db, feed).buttons == ()
    assert interaction.text.startswith(BAD_ADDRESS.format("The Button address"))
    assert "**Address**\n```\nexample.com/page\n```" in interaction.text
    assert component_ids(interaction.last[1]["view"]) == [
        f"rss:c:tpl_button_add:{feed.id}",
        f"rss:c:tpl_buttons:{feed.id}",
    ]


BAD_ADDRESS = (
    "{} must start with http://, https:// or one of the Placeholders that hold a web address: "
    "{{{{link}}}}, {{{{image}}}} or {{{{feed_link}}}}."
)


@pytest.mark.parametrize(
    "address",
    ["{{title}}", "{{title||link}}", "{{link||title}}", "{{date}}/x", "ftp://e.com", "{{{link}}}"],
)
async def test_a_button_address_that_can_never_be_a_web_address_is_refused(
    db: Database, service: FeedService, feed: Feed, address: str
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="Read", url=address
    )
    assert stored(db, feed).buttons == ()
    assert interaction.text.startswith(
        "The Button address must start with http://, https:// or one of the Placeholders "
        "that hold a web address: {{link}}, {{image}} or {{feed_link}}.\nNothing was saved."
    )
    assert f"**Address**\n```\n{address}\n```" in interaction.text


@pytest.mark.parametrize(
    "address",
    [
        "{{link}}",
        "{{ URL }}",
        "{{image||link}}",
        "{{feed_link}}/about",
        "{{link:400}}?ref=rss",
        "HTTPS://example.com/{{title}}",
        "http://example.com/?q={{title}}",
    ],
)
async def test_a_button_address_that_can_be_a_web_address_is_saved(
    db: Database, service: FeedService, feed: Feed, address: str
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="Read", url=address
    )
    assert stored(db, feed).buttons == (ButtonSpec("Read", address),)
    assert interaction.text.startswith("Added Button 1.\n")


async def test_a_button_address_with_an_unknown_placeholder_gets_that_error(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="Read", url="{{lnik}}"
    )
    assert stored(db, feed).buttons == ()
    assert '"lnik" in {{lnik}} is not a Placeholder name' in interaction.text


@pytest.mark.parametrize(("box", "what"), [("url", "The Embed link"), ("image", "The Embed image")])
async def test_an_embed_address_that_can_never_be_a_web_address_is_refused(
    db: Database, service: FeedService, feed: Feed, box: str, what: str
) -> None:
    parts = {"title": "T", "description": "", "url": "", "image": "", "footer": ""}
    parts[box] = "{{title}}"
    interaction = await submit(
        db, service, f"rss:m:tpl_embed:{feed.id}:{template.KEEP_COLOUR}", **parts
    )
    assert stored(db, feed).embed is None
    assert interaction.text.startswith(BAD_ADDRESS.format(what) + "\nNothing was saved.")
    assert "**Title**\n```\nT\n```" in interaction.text
    assert labels(interaction.last[1]["view"]) == ["Edit again", "Back to Feed"]


async def test_the_preview_says_which_buttons_are_left_out(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(
        db,
        buttons=(
            ButtonSpec("Read", "{{link}}"),
            ButtonSpec("Picture", "{{image}}"),
            ButtonSpec("{{author}}", "{{link}}"),
        ),
    )
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")
    assert interaction.text == (
        "Saved the message text of **News**.\n"
        "Buttons 2 and 3 are not shown for this Item: "
        "the label is empty or the address is not a web address.\n"
        "**Preview** of the newest Item as it would be posted:\n"
        "Title new"
    )
    rows = interaction.last[1]["view"].to_components()
    assert [component["label"] for component in rows[1]["components"]] == ["Read"]


async def test_the_preview_names_a_single_button_left_out(
    db: Database, service: FeedService, web: Web
) -> None:
    web.items = (item("new", 20, link=""),)
    feed = make_feed(db, buttons=(ButtonSpec("Read", "{{link}}"),))
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")
    assert (
        "\nButton 1 is not shown for this Item: "
        "the label is empty or the address is not a web address.\n" in interaction.text
    )


async def test_the_preview_says_nothing_when_every_button_is_shown(
    db: Database, service: FeedService
) -> None:
    feed = make_feed(db, buttons=(ButtonSpec("Read", "{{link}}"),))
    interaction = await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="{{title}}")
    assert "not shown" not in interaction.text


async def test_button_limit(db: Database, service: FeedService) -> None:
    buttons = tuple(ButtonSpec("b" * 80, "https://example.com/" + "u" * 490) for _ in range(5))
    assert len(buttons) == MAX_BUTTONS
    feed = make_feed(db, buttons=buttons)
    interaction = manager(db, service)
    await template.buttons_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert len(sent["content"]) <= 2000
    assert sent["content"].endswith("The limit of 5 Buttons is reached. Remove one to add another.")
    assert sent["view"].children[0].item.disabled is True

    click_it = manager(db, service, type=COMPONENT)
    await click(click_it, template.AddButton(feed.id))
    assert click_it.text == "A Feed can have at most 5 Buttons."
    sent_form = await submit(
        db, service, f"rss:m:tpl_button:{feed.id}", label="x", url="https://example.com/"
    )
    assert "A Feed can have at most 5 Buttons." in sent_form.text
    assert stored(db, feed) == feed


# -- reset --


async def test_reset_asks_then_resets(db: Database, service: FeedService) -> None:
    feed = make_feed(
        db, text_template="mine", embed=EMBED, buttons=(ButtonSpec("Read", "{{link}}"),)
    )
    interaction = manager(db, service)
    await template.reset_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    assert stored(db, feed) == feed
    private(interaction)
    assert interaction.text.startswith("Reset the Template of **News**?")
    view = interaction.last[1]["view"]
    assert component_ids(view) == [f"rss:c:tpl_reset:{feed.id}", "rss:c:cancel"]
    assert labels(view) == ["Reset", "Cancel"]

    confirm = manager(db, service, type=COMPONENT)
    await click(confirm, template.ResetTemplate(feed.id))
    after = stored(db, feed)
    assert (after.text_template, after.embed, after.buttons) == (DEFAULT_TEXT_TEMPLATE, None, ())
    assert confirm.last[0] == "edit_message"
    assert confirm.text.startswith("Reset the Template of **News**")
    assert component_ids(confirm.last[1]["view"]) == [f"rss:c:tpl_back:{feed.id}"]


async def test_reset_cancelled(db: Database, service: FeedService) -> None:
    feed = make_feed(db, text_template="mine", embed=EMBED)
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, ui.CancelButton())
    assert interaction.last[0] == "edit_message"
    assert interaction.text == "Cancelled."
    assert interaction.last[1]["view"] is None
    assert stored(db, feed) == feed


# -- placeholders --


async def test_placeholders_lists_every_value(
    db: Database, service: FeedService, feed: Feed
) -> None:
    interaction = manager(db, service)
    await template.placeholders_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    assert [name for name, _ in interaction.calls] == ["defer", "followup"]
    private(interaction)
    lines = interaction.text.split("\n")
    assert lines[1:13] == [
        "`{{title}}` Title new",
        "`{{link}}` https://example.com/new",
        "`{{description}}` Summary new",
        "`{{summary}}` Summary new",
        "`{{content}}` (empty)",
        "`{{author}}` (empty)",
        "`{{date}}` <t:20:f>",
        "`{{categories}}` (empty)",
        "`{{image}}` (empty)",
        "`{{feed_title}}` News",
        "`{{feed_link}}` (empty)",
        "`{{mentions}}` (empty)",
    ]
    assert lines[13:] == list(template.PLACEHOLDER_NOTES)
    assert "{{summary||description}}" in lines[13]
    assert "{{description:200}}" in lines[14]
    assert "{{url}}" in lines[15]


async def test_placeholders_fit_with_huge_values(
    db: Database, service: FeedService, web: Web
) -> None:
    huge = "word " * 2000
    web.items = (
        item(
            "big",
            5,
            title=huge,
            link="https://example.com/" + "p" * 1500,
            summary=huge,
            content=huge,
            author=huge,
            categories=(huge,),
            image="https://example.com/" + "i" * 1500,
        ),
    )
    feed = make_feed(db, source_title=huge, source_link="https://example.com/" + "s" * 1500)
    interaction = manager(db, service)
    await template.placeholders_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    text = interaction.text
    assert 1900 < len(text) <= 2000
    lines = text.split("\n")
    assert len(lines) == 16
    assert lines[7] == "`{{date}}` <t:5:f>"  # short values are not cut
    assert lines[12] == "`{{mentions}}` (empty)"
    assert lines[1].startswith("`{{title}}` word word") and lines[1].endswith("…")
    assert lines[13:] == list(template.PLACEHOLDER_NOTES)


async def test_placeholders_when_the_fetch_fails(
    db: Database, service: FeedService, web: Web
) -> None:
    web.error = FetchError("The address answered with error 500.")
    feed = make_feed(db, name="")
    interaction = manager(db, service)
    await template.placeholders_command.callback(interaction, feed=str(feed.id))  # type: ignore[arg-type]

    lines = interaction.text.split("\n")
    assert lines[1] == template.NO_ITEM_VALUES
    assert lines[2] == "`{{title}}` (empty)"
    assert len(lines) == 17
    assert all(line.endswith("(empty)") for line in lines[2:14])
    assert lines[14:] == list(template.PLACEHOLDER_NOTES)


# -- back to the Feed panel --


async def test_back_to_feed_hands_over_to_the_feed_panel(
    db: Database, service: FeedService, feed: Feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[Any, int, bool]] = []

    async def open_panel(interaction: Any, feed_id: int, *, edit: bool = False) -> None:
        opened.append((interaction, feed_id, edit))

    fake = SimpleNamespace(open_panel=open_panel)
    monkeypatch.setitem(sys.modules, "rssbot.commands.feed", fake)
    interaction = manager(db, service, type=COMPONENT)
    await click(interaction, template.BackToFeed(feed.id))

    assert opened == [(interaction, feed.id, True)]
    assert interaction.calls == []  # the panel does the responding


# -- Log entries --


async def test_template_changes_are_saved_by_the_member_who_made_them(
    db: Database, service: FeedService, feed: Feed
) -> None:
    def robin(**kwargs: Any) -> FakeInteraction:
        return manager(db, service, display_name="Robin", **kwargs)

    async def send(custom_id: str, **values: str | list[str]) -> None:
        form = robin(type=MODAL, data=submission(custom_id.format(feed.id), **values))
        await ui.handle_interaction(form)  # type: ignore[arg-type]

    await send("rss:m:tpl_text:{}", text="{{title}}")
    await send("rss:m:tpl_embed:{}:0", title="{{title}}")
    await click(robin(type=COMPONENT), template.ColourSelect(feed.id), "2ecc71")
    await send("rss:m:tpl_field:{}", name="By", value="{{author}}", inline=["no"])
    mark = option_values(stored(db, feed), "fields")[0]
    await click(robin(type=COMPONENT), template.RemoveField(feed.id), mark)
    await send("rss:m:tpl_button:{}", label="Read", url="{{link}}")
    mark = option_values(stored(db, feed), "buttons")[0]
    await click(robin(type=COMPONENT), template.RemoveButton(feed.id), mark)
    await click(robin(type=COMPONENT), template.RemoveEmbedConfirmed(feed.id))
    await click(robin(type=COMPONENT), template.ResetTemplate(feed.id))

    entries = db.list_log_entries(SERVER, limit=100)[::-1]
    assert [entry.detail for entry in entries] == [
        "changed the message text",
        "added the Embed",
        "changed the Embed",
        "added Field 1",
        "removed Field 1",
        "added Button 1",
        "removed Button 1",
        "removed the Embed",
        "reset the Template",
    ]
    for entry in entries:
        assert (entry.kind, entry.actor_id, entry.actor_name) == (
            LogKind.TEMPLATE_CHANGED,
            USER,
            "Robin",
        )
        assert (entry.feed_id, entry.feed_name) == (feed.id, feed.name)


async def test_a_form_that_was_not_saved_is_refused_in_the_container_log_and_not_saved(
    db: Database, service: FeedService, feed: Feed, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rssbot.commands")
    await submit(db, service, f"rss:m:tpl_text:{feed.id}", text="Breaking: {{titel}}")
    [line] = [r.getMessage() for r in caplog.records if r.name == "rssbot.commands"]
    assert line.startswith('command name="form tpl_text" server=100 ')
    assert " outcome=refused reason=" in line and "is not a Placeholder name" in line
    assert db.list_log_entries(SERVER, limit=5) == []
