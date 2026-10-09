from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import OWNER, SERVER, USER, FakeInteraction, component_ids

from rssbot.commands import _ui as ui
from rssbot.commands import filter as flt
from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import (
    ChannelKind,
    Feed,
    FilterField,
    FilterList,
    Level,
    LogKind,
    TargetKind,
)
from rssbot.ports import FetchError
from rssbot.service import FeedService

OTHER_SERVER = 200
MUST = FilterList.MUST_HAVE
BLOCK = FilterList.BLOCK
COMPONENT = discord.InteractionType.component
MODAL = discord.InteractionType.modal_submit


class NoWeb:
    """The Filters never touch the network."""

    async def fetch(self, url: str, **kwargs: Any) -> Any:
        raise FetchError("offline")

    async def fetch_image(self, url: str, **kwargs: Any) -> Any:
        raise FetchError("offline")


class Clock:
    def now(self) -> int:
        return 1_700_000_000


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


@pytest.fixture
def service(db: Database) -> FeedService:
    return FeedService(db, NoWeb(), Clock(), Journal(db, Clock()))


def add_feed(db: Database, name: str = "News", server_id: int = SERVER) -> Feed:
    return db.create_feed(
        server_id=server_id,
        channel_id=7,
        channel_kind=ChannelKind.MESSAGES,
        name=name,
        url=f"https://example.com/{name}-{server_id}.xml",
        now=0,
    )


def manager(db: Database, service: FeedService, **kwargs: Any) -> FakeInteraction:
    """A Manager (by Grant) using the bot."""
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    return person(db, service, **kwargs)


def person(db: Database, service: FeedService, **kwargs: Any) -> FakeInteraction:
    interaction = FakeInteraction(db, **kwargs)
    interaction.client.deps.service = service
    if interaction.type is MODAL:
        # The form is only ever opened by a button, so it comes with that button's message.
        interaction.message = SimpleNamespace(id=1)
    return interaction


def words_of(db: Database, feed: Feed, flt_list: FilterList | None = None) -> list[tuple[str, str]]:
    return [
        (f.word, f.field.value)
        for f in db.list_filters(feed.id)
        if flt_list is None or f.list is flt_list
    ]


def submission(
    feed_id: int, list_index: int, words: str, field: str | None = "any"
) -> dict[str, Any]:
    components: list[dict[str, Any]] = [
        {"type": 18, "component": {"type": 4, "custom_id": "words", "value": words}}
    ]
    if field is not None:
        components.append(
            {"type": 18, "component": {"type": 3, "custom_id": "field", "values": [field]}}
        )
    return {"custom_id": f"rss:m:flt_add:{feed_id}:{list_index}", "components": components}


async def pick(select: flt.RemoveFilter, interaction: FakeInteraction, filter_id: int) -> None:
    select.item._values = [str(filter_id)]  # type: ignore[attr-defined]
    await select.callback(interaction)  # type: ignore[arg-type]


def denied(interaction: FakeInteraction) -> bool:
    return interaction.calls == [
        (
            "send_message",
            {"content": ui.NEED_MANAGER, "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]


def gone(interaction: FakeInteraction) -> bool:
    return interaction.calls == [
        (
            "send_message",
            {"content": ui.FEED_GONE, "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]


# -- The command tree --


def test_tree_serialises_within_discords_limits() -> None:
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents(guilds=True)))
    flt.register(tree)
    (command,) = tree.get_commands()
    payload = command.to_dict(tree)
    assert payload["name"] == "filter"
    assert payload["contexts"] == [0]
    assert payload.get("default_member_permissions") is None
    assert 1 <= len(payload["description"]) <= 100
    (option,) = payload["options"]
    assert (option["name"], option["type"], option["autocomplete"]) == ("feed", 3, True)
    assert 1 <= len(option["description"]) <= 100


def test_every_action_and_form_name_starts_with_flt() -> None:
    actions = {name for name, cls in ui._ACTIONS.items() if cls.__module__ == flt.__name__}
    forms = {name for name, form in ui._FORMS.items() if form.handler.__module__ == flt.__name__}
    assert actions == {"flt_must", "flt_block", "flt_back", "flt_remove"}
    assert forms == {"flt_add"}


# -- Rendering --


async def test_the_command_shows_an_empty_feed(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    interaction = manager(db, service)
    await flt.filter_command.callback(interaction, str(feed.id))  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    text = sent["content"]
    assert "**Filters for News**" in text
    assert flt.EXPLANATION in text
    assert "**Must-have words**\nnone" in text
    assert "**Block words**\nnone" in text
    assert component_ids(sent["view"]) == [
        f"rss:c:flt_must:{feed.id}",
        f"rss:c:flt_block:{feed.id}",
        f"rss:c:flt_back:{feed.id}",
    ]
    labels = [item.label for item in sent["view"].children]
    assert labels == ["Add must-have words", "Add block words", "Back to Feed"]
    assert sent["view"].timeout is None


async def test_a_few_filters_are_listed_with_their_field(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    db.add_filter(feed.id, MUST, FilterField.ANY, "python")
    db.add_filter(feed.id, MUST, FilterField.TITLE, "rumour")
    second = db.add_filter(feed.id, BLOCK, FilterField.AUTHOR, "bot")
    interaction = manager(db, service)
    await flt.open_filters(interaction, feed.id)  # type: ignore[arg-type]

    sent = interaction.last[1]
    must, block = sent["content"].split("**Block words**")
    assert "- python\n" in must + "\n" and "- rumour (title)" in must and "(any)" not in must
    assert block.strip() == "- bot (author)"
    select = sent["view"].children[-1].item
    assert select.custom_id == f"rss:c:flt_remove:{feed.id}"
    assert [(o.label, o.description) for o in select.options][-1] == ("bot", "Block, author")
    assert [o.label for o in select.options] == ["python", "rumour", "bot"]
    assert select.options[-1].value == str(second.id)
    assert select.options[1].description == "Must-have, title"
    assert select.options[0].description == "Must-have, title and description"


async def test_over_25_filters_show_the_first_25_in_the_menu(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    for number in range(30):
        db.add_filter(feed.id, MUST, FilterField.ANY, f"word{number:02}")
    interaction = manager(db, service)
    await flt.open_filters(interaction, feed.id)  # type: ignore[arg-type]

    sent = interaction.last[1]
    select = sent["view"].children[-1].item
    assert [o.label for o in select.options] == [f"word{n:02}" for n in range(25)]
    assert "first 25" in sent["content"]
    assert "Removing some will reveal the rest" in sent["content"]
    assert "word29" in sent["content"]  # the list itself still shows them all
    payload = sent["view"].to_components()
    assert len(payload) <= 5
    assert all(len(row["components"]) <= 25 or row["type"] == 1 for row in payload)


async def test_25_filters_or_fewer_say_nothing_about_a_limit(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    for number in range(25):
        db.add_filter(feed.id, BLOCK, FilterField.ANY, f"w{number}")
    interaction = manager(db, service)
    await flt.open_filters(interaction, feed.id)  # type: ignore[arg-type]
    assert "Removing some" not in interaction.text


def test_the_text_is_cut_to_fit_the_message_limit(db: Database) -> None:
    feed = add_feed(db, "N" * 100)
    for number in range(100):
        side = MUST if number % 2 else BLOCK
        db.add_filter(feed.id, side, FilterField.CATEGORY, f"{number:03}" + "w" * 97)
    filters = db.list_filters(feed.id)
    text = flt.render_filters(feed.name, filters, "x" * 500)

    assert len(text) <= ui.MESSAGE_LIMIT
    assert "…and " in text and " more" in text
    # Both lists get a share, however long the first one is.
    assert text.count("- ") >= 4
    must_part, block_part = text.split("**Block words**")
    assert "- " in must_part and "- " in block_part


def test_one_long_list_does_not_starve_the_other(db: Database) -> None:
    feed = add_feed(db)
    for number in range(60):
        db.add_filter(feed.id, MUST, FilterField.ANY, f"{number:02}" + "m" * 90)
    db.add_filter(feed.id, BLOCK, FilterField.ANY, "spam")
    text = flt.render_filters(feed.name, db.list_filters(feed.id))
    assert len(text) <= ui.MESSAGE_LIMIT
    assert "- spam" in text


def test_a_short_list_leaves_its_room_to_the_long_one(db: Database) -> None:
    feed = add_feed(db)
    db.add_filter(feed.id, MUST, FilterField.ANY, "rumour")
    for number in range(60):
        db.add_filter(feed.id, BLOCK, FilterField.ANY, f"{number:02}" + "b" * 18)
    text = flt.render_filters(feed.name, db.list_filters(feed.id))
    assert len(text) <= ui.MESSAGE_LIMIT
    assert text.count("- ") > 60  # all 60 fit, as the other list is short
    assert "…and" not in text


def test_words_are_shown_as_typed_without_mentions_or_formatting(db: Database) -> None:
    feed = add_feed(db)
    db.add_filter(feed.id, BLOCK, FilterField.ANY, "@everyone *big* <@123456789012345678>")
    text = flt.render_filters(feed.name, db.list_filters(feed.id))
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "<@123456789012345678>" not in text
    assert "\\*big\\*" in text


def test_the_explanation_says_what_users_get_wrong() -> None:
    text = flt.render_filters("News", [])
    assert (
        "Changes apply to new Items only: an Item that was filtered out is not posted later."
        in text
    )
    assert "Whole words, capitals ignored." in text
    assert "Accents and endings must match exactly" in text
    assert "“cafe” does not match “Café”" in text and "“bank” does not match “Banks”" in text
    assert len(flt.EXPLANATION) <= 400


# -- Adding words --


async def test_the_add_buttons_open_the_form(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    for button, expected_id, title in [
        (flt.AddMustHave(feed.id), f"rss:m:flt_add:{feed.id}:0", "Add must-have words"),
        (flt.AddBlock(feed.id), f"rss:m:flt_add:{feed.id}:1", "Add block words"),
    ]:
        interaction = manager(db, service, type=COMPONENT)
        await button.callback(interaction)  # type: ignore[arg-type]

        name, sent = interaction.last
        assert name == "send_modal"
        modal = sent["modal"]
        assert modal.is_finished()
        payload = modal.to_dict()
        assert payload["custom_id"] == expected_id
        assert payload["title"] == title
        words, look_in = payload["components"]
        assert (words["label"], words["component"]["style"]) == ("Words", 2)
        assert words["component"]["custom_id"] == "words"
        assert (look_in["label"], look_in["component"]["custom_id"]) == ("Look in", "field")
        assert [(o["value"], o["label"]) for o in look_in["component"]["options"]] == [
            ("any", "Title and description"),
            ("title", "Title"),
            ("description", "Description"),
            ("category", "Category"),
            ("author", "Author"),
        ]
        assert look_in["component"]["options"][0]["default"] is True


@pytest.mark.parametrize("list_index", [0, 1])
@pytest.mark.parametrize("field", [f.value for f in FilterField])
async def test_the_form_adds_words_to_the_chosen_list_and_field(
    db: Database, service: FeedService, list_index: int, field: str
) -> None:
    feed = add_feed(db)
    interaction = manager(
        db,
        service,
        type=MODAL,
        data=submission(feed.id, list_index, "  one \n\nTwo words\n   \nthree  \n", field),
    )
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]

    flt_list = (MUST, BLOCK)[list_index]
    assert words_of(db, feed, flt_list) == [(w, field) for w in ("one", "Two words", "three")]
    assert words_of(db, feed, (BLOCK, MUST)[list_index]) == []
    name, sent = interaction.last
    assert name == "edit_message"
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    noun = ("must-have", "block")[list_index]
    assert f"Added 3 {noun} words: “one”, “Two words”, “three”." in sent["content"]
    assert "Two words" in sent["content"]
    assert component_ids(sent["view"])[-1] == f"rss:c:flt_remove:{feed.id}"


async def test_adding_one_word_says_so_in_the_singular(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    interaction = manager(db, service, type=MODAL, data=submission(feed.id, 0, "solo"))
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert "Added 1 must-have word: “solo”." in interaction.text


async def test_a_line_with_commas_is_shown_back_as_the_one_phrase_it_became(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 0, "apple, banana, cherry")
    interaction = manager(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert words_of(db, feed) == [("apple, banana, cherry", "any")]
    assert "Added 1 must-have word: “apple, banana, cherry”." in interaction.text


async def test_the_notice_names_the_first_few_added_words_and_counts_the_rest(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 1, "one\ntwo\nthree\nfour\nfive")
    interaction = manager(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert "Added 5 block words: “one”, “two”, “three” and 2 more." in interaction.text


async def test_the_notice_names_only_what_was_added(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    db.add_filter(feed.id, MUST, FilterField.ANY, "old")
    interaction = manager(db, service, type=MODAL, data=submission(feed.id, 0, "OLD\nnew"))
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert "Added 1 must-have word: “new”." in interaction.text


async def test_the_notice_shows_added_words_without_mentions_or_formatting(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 0, "@everyone *big* <@123456789012345678>")
    interaction = manager(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    text = interaction.text
    assert (
        "Added 1 must-have word: “@\u200beveryone \\*big\\* <\u200b@\u200b123456789012345678\\>”."
        in text
    )
    assert "@everyone" not in text and "<@123456789012345678>" not in text
    assert interaction.last[1]["allowed_mentions"] is ui.NO_MENTIONS


async def test_the_notice_stays_whole_and_short_for_long_words(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    words = "\n".join(str(n) + "*" * 99 for n in range(5))
    interaction = manager(db, service, type=MODAL, data=submission(feed.id, 0, words))
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    (notice,) = [part for part in interaction.text.split("\n\n") if part.startswith("Added 5")]
    assert len(notice) <= 200
    assert notice.startswith("Added 5 must-have words: “0\\*")
    assert notice.endswith("more.") and "…”" in notice
    assert len(interaction.text) <= ui.MESSAGE_LIMIT


async def test_duplicates_and_blanks_add_nothing(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    db.add_filter(feed.id, MUST, FilterField.ANY, "Python")
    for words in ("python\n  PYTHON ", "   \n\n"):
        interaction = manager(db, service, type=MODAL, data=submission(feed.id, 0, words))
        await ui.handle_interaction(interaction)  # type: ignore[arg-type]
        assert interaction.last[0] == "edit_message"
        assert "Nothing was added" in interaction.text
    assert words_of(db, feed) == [("Python", "any")]


async def test_a_word_already_in_the_other_list_is_refused(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    for index in (0, 1):
        interaction = manager(db, service, type=MODAL, data=submission(feed.id, index, "news"))
        await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert interaction.last[0] == "send_message"
    assert "“news” is already a must-have word" in interaction.text
    assert words_of(db, feed) == [("news", "any")]
    assert words_of(db, feed, BLOCK) == []


async def test_a_service_error_is_shown_and_nothing_is_added(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 0, "fine\n" + "x" * 101)
    interaction = manager(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert interaction.last[0] == "send_message"
    assert "at most 100 characters" in interaction.text
    assert words_of(db, feed) == []


@pytest.mark.parametrize(
    ("custom_id", "field"), [("rss:m:flt_add:{}:2", "any"), ("rss:m:flt_add:{}:0", "colour")]
)
async def test_a_forged_form_is_refused(
    db: Database, service: FeedService, custom_id: str, field: str
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 0, "word", field)
    data["custom_id"] = custom_id.format(feed.id)
    interaction = manager(db, service, type=MODAL, data=data)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert interaction.last[0] == "send_message"
    assert words_of(db, feed) == []


async def test_a_form_without_a_field_choice_uses_any(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    data = submission(feed.id, 1, "word", None)
    await ui.handle_interaction(manager(db, service, type=MODAL, data=data))  # type: ignore[arg-type]
    assert words_of(db, feed) == [("word", "any")]


# -- Removing --


async def test_the_select_removes_one_filter(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    keep = db.add_filter(feed.id, MUST, FilterField.ANY, "keep")
    drop = db.add_filter(feed.id, BLOCK, FilterField.TITLE, "drop me")
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(feed.id), interaction, drop.id)

    assert [f.id for f in db.list_filters(feed.id)] == [keep.id]
    name, sent = interaction.last
    assert name == "edit_message"
    assert "Removed “drop me” from the block words." in sent["content"]
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    assert component_ids(sent["view"])[-1] == f"rss:c:flt_remove:{feed.id}"


async def test_removing_the_last_one_drops_the_menu(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    only = db.add_filter(feed.id, MUST, FilterField.ANY, "only")
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(feed.id), interaction, only.id)
    assert len(interaction.last[1]["view"].children) == 3


async def test_removing_reveals_the_rest(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    made = [db.add_filter(feed.id, MUST, FilterField.ANY, f"w{n:02}") for n in range(27)]
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(feed.id), interaction, made[0].id)
    select = interaction.last[1]["view"].children[-1].item
    labels = [o.label for o in select.options]
    assert (labels[0], labels[-1], len(labels)) == ("w01", "w25", 25)  # w26 is still hidden
    assert "first 25" in interaction.text  # 26 left: one more removal and the note goes


async def test_removing_something_already_gone_redraws_the_list_in_place(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    left = db.add_filter(feed.id, MUST, FilterField.ANY, "left")
    gone_one = db.add_filter(feed.id, BLOCK, FilterField.ANY, "taken")
    db.remove_filter(gone_one.id)  # someone else removed it; this screen still lists it
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(feed.id), interaction, gone_one.id)

    assert [name for name, _ in interaction.calls] == ["edit_message"]
    sent = interaction.last[1]
    assert sent["content"].endswith("That word was already removed.")
    assert "**Must-have words**\n- left" in sent["content"]
    assert "**Block words**\nnone" in sent["content"]
    assert "taken" not in sent["content"]
    select = sent["view"].children[-1].item
    assert [(o.label, o.value) for o in select.options] == [("left", str(left.id))]
    assert [f.id for f in db.list_filters(feed.id)] == [left.id]


async def test_removing_the_last_word_twice_redraws_an_empty_list(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(feed.id), interaction, 12345)
    assert interaction.last[0] == "edit_message"
    assert "That word was already removed." in interaction.text
    assert component_ids(interaction.last[1]["view"]) == [
        f"rss:c:flt_must:{feed.id}",
        f"rss:c:flt_block:{feed.id}",
        f"rss:c:flt_back:{feed.id}",
    ]


async def test_another_feeds_filter_cannot_be_removed(db: Database, service: FeedService) -> None:
    mine, theirs = add_feed(db, "Mine"), add_feed(db, "Theirs")
    other = db.add_filter(theirs.id, MUST, FilterField.ANY, "secret")
    interaction = manager(db, service, type=COMPONENT)
    await pick(flt.RemoveFilter(mine.id), interaction, other.id)
    assert "That word was already removed." in interaction.text
    assert "secret" not in interaction.text
    assert [f.word for f in db.list_filters(theirs.id)] == ["secret"]


async def test_an_empty_selection_is_refused(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    interaction = manager(db, service, type=COMPONENT)
    select = flt.RemoveFilter(feed.id)
    select.item._values = []  # type: ignore[attr-defined]
    await select.callback(interaction)  # type: ignore[arg-type]
    assert "Choose a word to remove." in interaction.text


# -- Back to feed --


@pytest.mark.parametrize("module_loaded", [True])
async def test_back_to_feed_opens_the_panel_in_place(
    db: Database, service: FeedService, monkeypatch: pytest.MonkeyPatch, module_loaded: bool
) -> None:
    feed = add_feed(db)
    calls: list[tuple[Any, ...]] = []

    async def open_panel(interaction: Any, feed_id: int, *, edit: bool = False) -> None:
        calls.append((interaction, feed_id, edit))

    fake = SimpleNamespace(open_panel=open_panel)
    monkeypatch.setitem(sys.modules, "rssbot.commands.feed", fake)
    if module_loaded:
        import rssbot.commands

        monkeypatch.setattr(rssbot.commands, "feed", fake, raising=False)
    interaction = manager(db, service, type=COMPONENT)
    await flt.BackToFeed(feed.id).callback(interaction)  # type: ignore[arg-type]
    assert calls == [(interaction, feed.id, True)]
    assert interaction.calls == []


# -- Where the message goes --


async def test_a_component_replaces_its_message_and_a_command_sends_a_new_one(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    clicked = manager(db, service, type=COMPONENT)
    await flt.open_filters(clicked, feed.id)  # type: ignore[arg-type]
    assert clicked.last[0] == "edit_message"

    typed = manager(db, service)
    await flt.open_filters(typed, feed.id)  # type: ignore[arg-type]
    assert typed.last[0] == "send_message"


# -- Access --


async def test_the_command_is_refused_for_a_non_manager(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    interaction = person(db, service)
    with pytest.raises(ui.UserError, match="Only Managers and Admins"):
        await flt.filter_command.callback(interaction, str(feed.id))  # type: ignore[arg-type]
    assert interaction.calls == []


async def test_open_filters_checks_access_itself(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    with pytest.raises(ui.UserError, match="Only Managers and Admins"):
        await flt.open_filters(person(db, service), feed.id)  # type: ignore[arg-type]


@pytest.mark.parametrize("who", ["owner", "administrator", "manager role"])
async def test_the_command_is_open_to_managers_and_admins(
    db: Database, service: FeedService, who: str
) -> None:
    feed = add_feed(db)
    if who == "owner":
        interaction = person(db, service, user_id=OWNER)
    elif who == "administrator":
        interaction = person(db, service, administrator=True)
    else:
        db.set_grant(SERVER, 55, TargetKind.ROLE, Level.MANAGER)
        interaction = person(db, service, role_ids=(55,))
    await flt.filter_command.callback(interaction, str(feed.id))  # type: ignore[arg-type]
    assert interaction.last[0] == "send_message"


async def test_the_command_needs_a_feed_picked_from_the_list(
    db: Database, service: FeedService
) -> None:
    with pytest.raises(ui.UserError, match="Choose a Feed"):
        await flt.filter_command.callback(manager(db, service), "not a number")  # type: ignore[arg-type]


@pytest.mark.parametrize("use", ["must", "block", "back", "select"])
async def test_components_recheck_manager_access(
    db: Database, service: FeedService, use: str
) -> None:
    feed = add_feed(db)
    made = db.add_filter(feed.id, MUST, FilterField.ANY, "keep")
    interaction = person(db, service, type=COMPONENT)
    if use == "select":
        await pick(flt.RemoveFilter(feed.id), interaction, made.id)
    else:
        button = {"must": flt.AddMustHave, "block": flt.AddBlock, "back": flt.BackToFeed}[use]
        await button(feed.id).callback(interaction)  # type: ignore[arg-type]
    assert denied(interaction)
    assert [f.word for f in db.list_filters(feed.id)] == ["keep"]


async def test_the_form_rechecks_manager_access(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    interaction = person(db, service, type=MODAL, data=submission(feed.id, 0, "sneaky"))
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert denied(interaction)
    assert words_of(db, feed) == []


async def test_an_admin_grant_is_enough_for_the_form(db: Database, service: FeedService) -> None:
    feed = add_feed(db)
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.ADMIN)
    interaction = person(db, service, type=MODAL, data=submission(feed.id, 0, "ok"))
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert words_of(db, feed) == [("ok", "any")]


# -- A Feed of another Server --


async def test_the_command_refuses_a_feed_of_another_server(
    db: Database, service: FeedService
) -> None:
    foreign = add_feed(db, "Foreign", OTHER_SERVER)
    interaction = manager(db, service)
    with pytest.raises(ui.UserError, match="no longer exists"):
        await flt.filter_command.callback(interaction, str(foreign.id))  # type: ignore[arg-type]
    with pytest.raises(ui.UserError, match="no longer exists"):
        await flt.open_filters(interaction, foreign.id)  # type: ignore[arg-type]
    assert interaction.calls == []


@pytest.mark.parametrize("use", ["must", "block", "back", "select", "form"])
async def test_controls_refuse_a_feed_of_another_server(
    db: Database, service: FeedService, use: str
) -> None:
    foreign = add_feed(db, "Foreign", OTHER_SERVER)
    theirs = db.add_filter(foreign.id, MUST, FilterField.ANY, "theirs")
    interaction = manager(db, service, type=MODAL if use == "form" else COMPONENT)
    if use == "select":
        await pick(flt.RemoveFilter(foreign.id), interaction, theirs.id)
    elif use == "form":
        interaction.data = submission(foreign.id, 0, "planted")
        await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    else:
        button = {"must": flt.AddMustHave, "block": flt.AddBlock, "back": flt.BackToFeed}[use]
        await button(foreign.id).callback(interaction)  # type: ignore[arg-type]
    if use == "form":  # it has the message of the button that opened it, which is replaced
        assert [name for name, _ in interaction.calls] == ["edit_message"]
        assert interaction.text == ui.FEED_GONE
    else:
        assert gone(interaction)
    assert [f.word for f in db.list_filters(foreign.id)] == ["theirs"]


async def test_a_missing_feed_is_refused_by_the_controls(
    db: Database, service: FeedService
) -> None:
    interaction = manager(db, service, type=COMPONENT)
    await flt.AddMustHave(999).callback(interaction)  # type: ignore[arg-type]
    assert gone(interaction)


# -- Log entries --


async def test_filter_changes_are_saved_by_the_member_who_made_them(
    db: Database, service: FeedService
) -> None:
    feed = add_feed(db)
    data = submission(feed.id, flt.FORM_LISTS.index(BLOCK), "sponsored\nadvert", "title")
    added = manager(db, service, type=MODAL, data=data, display_name="Robin")
    await ui.handle_interaction(added)  # type: ignore[arg-type]
    drop = db.list_filters(feed.id)[0]
    await pick(flt.RemoveFilter(feed.id), manager(db, service, type=COMPONENT), drop.id)
    nothing = submission(feed.id, flt.FORM_LISTS.index(BLOCK), "  \nadvert", "title")
    await ui.handle_interaction(manager(db, service, type=MODAL, data=nothing))  # type: ignore[arg-type]

    entries = db.list_log_entries(SERVER, limit=100)[::-1]
    assert [(e.kind, e.actor_id, e.actor_name, e.detail) for e in entries] == [
        (
            LogKind.FILTER_CHANGED,
            USER,
            "Robin",
            'added block Filters "sponsored", "advert" (title only)',
        ),
        (LogKind.FILTER_CHANGED, USER, "Alex", f'removed block Filter "{drop.word}" (title only)'),
    ]
    assert all((e.feed_id, e.feed_name) == (feed.id, feed.name) for e in entries)
