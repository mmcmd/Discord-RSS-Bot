from __future__ import annotations

from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import OWNER, SERVER, USER, FakeInteraction, component_ids

from rssbot.commands import _ui as ui
from rssbot.commands import add_all, help
from rssbot.db import Database
from rssbot.models import Level, TargetKind

TEMPLATES, ADMIN = 1, 2  # positions in help.CATEGORIES


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def admin(db: Database, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(db, user_id=OWNER, **kwargs)


def manager(db: Database, **kwargs: Any) -> FakeInteraction:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    return FakeInteraction(db, **kwargs)


def buttons(view: discord.ui.View) -> list[list[dict[str, Any]]]:
    return [row["components"] for row in view.to_components()]


async def click(interaction: FakeInteraction, cls: type[ui.Action], custom_id: str) -> None:
    match = cls.__discord_ui_compiled_template__.fullmatch(custom_id)
    item = await cls.from_custom_id(interaction, None, match)  # type: ignore[arg-type]
    await item.callback(interaction)  # type: ignore[arg-type]


# -- The text keeps up with the commands --


def _leaves(command: Any, prefix: str = "") -> dict[str, str]:
    """Every runnable command as {"feed refresh": "[feed]"}, options in Discord's order."""
    name = f"{prefix}{command.name}"
    if isinstance(command, app_commands.Group):
        found: dict[str, str] = {}
        for child in command.commands:
            found.update(_leaves(child, f"{name} "))
        return found
    options = [
        f"<{p.display_name}>" if p.required else f"[{p.display_name}]" for p in command.parameters
    ]
    return {name: " ".join(options)}


def test_every_command_has_a_help_entry_with_its_options() -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = app_commands.CommandTree(client)
    add_all(tree, client)
    registered: dict[str, str] = {}
    for command in tree.get_commands():
        registered.update(_leaves(command))
    del registered["help"]

    written = {command: entry.options for command, entry in help.ENTRIES.items()}
    missing = sorted(f"/{name}" for name in registered.keys() - written.keys())
    left_over = sorted(f"/{name}" for name in written.keys() - registered.keys())
    assert not missing, f"No help entry in commands/help.py for: {', '.join(missing)}"
    assert not left_over, f"Help entry in commands/help.py for a missing command: {left_over}"
    assert written == registered  # the options each entry lists are the command's own


def test_entries_are_unique_and_examples_show_their_command() -> None:
    entries = [entry for category in help.CATEGORIES for entry in category.entries]
    assert len(entries) == len(help.ENTRIES)
    for entry in entries:
        assert entry.text
        assert bool(entry.example) == bool(entry.options), entry.command
        if entry.example:
            assert entry.example.startswith(f"/{entry.command} ")


def test_every_page_fits_an_embed_and_a_button_row() -> None:
    assert len(help.CATEGORIES) <= 5
    for category in help.CATEGORIES:
        for start in range(0, len(category.entries), help.PER_PAGE):
            shown = category.entries[start : start + help.PER_PAGE]
            body = "\n\n".join([category.blurb, *map(help._entry_text, shown)])
            assert len(body) <= ui.EMBED_DESCRIPTION_LIMIT


# -- /help --


async def test_help_opens_on_feeds_for_an_admin(db: Database) -> None:
    interaction = admin(db)
    await help.help_command.callback(interaction)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True
    assert "content" not in sent
    embed = sent["embed"]
    assert embed.title == "Feeds"
    assert embed.colour == discord.Colour.blurple()
    assert embed.footer.text == "Page 1 of 2"
    assert embed.description.count("**/") == help.PER_PAGE
    assert "**/feed add**\n" in embed.description
    assert "**/feed edit** `<feed>`" in embed.description
    assert "Example: `/feed edit feed:Ars Technica`" in embed.description
    assert "/feed refresh" not in embed.description

    categories, arrows = buttons(sent["view"])
    assert [b["label"] for b in categories] == ["Feeds", "Templates", "Admin"]
    assert [b["disabled"] for b in categories] == [True, False, False]
    assert categories[0]["style"] == discord.ButtonStyle.primary.value
    assert categories[1]["style"] == discord.ButtonStyle.secondary.value
    assert [(b["label"], b["disabled"]) for b in arrows] == [("◀", True), ("▶", False)]
    assert component_ids(sent["view"]) == [
        "rss:c:help_category:0",
        "rss:c:help_category:1",
        "rss:c:help_category:2",
        "rss:c:help_page:0:0",
        "rss:c:help_page:0:1",
    ]
    assert sent["view"].timeout is None


async def test_help_hides_the_admin_category_from_a_manager(db: Database) -> None:
    interaction = manager(db)
    await help.help_command.callback(interaction)  # type: ignore[arg-type]

    sent = interaction.last[1]
    assert sent["embed"].title == "Feeds"
    assert [b["label"] for b in buttons(sent["view"])[0]] == ["Feeds", "Templates"]


async def test_help_tells_a_member_without_access_what_to_do(db: Database) -> None:
    interaction = FakeInteraction(db)
    await help.help_command.callback(interaction)  # type: ignore[arg-type]

    name, sent = interaction.last
    assert name == "send_message"
    assert sent["content"] == help.NO_ACCESS
    assert sent["ephemeral"] is True
    assert "embed" not in sent and "view" not in sent


async def test_help_only_works_in_a_server(db: Database) -> None:
    interaction = FakeInteraction(db, guild_id=None)
    with pytest.raises(ui.UserError, match=ui.SERVER_ONLY):
        await help.help_command.callback(interaction)  # type: ignore[arg-type]


# -- Clicks --


async def test_next_shows_the_second_page_of_feeds(db: Database) -> None:
    interaction = manager(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpPage, "rss:c:help_page:0:1")

    name, sent = interaction.last
    assert name == "edit_message"
    assert sent["content"] is None
    embed = sent["embed"]
    assert embed.title == "Feeds"
    assert embed.footer.text == "Page 2 of 2"
    assert "**/feed refresh** `[feed]`" in embed.description
    assert "**/filter** `<feed>`" in embed.description
    assert "/feed add" not in embed.description
    arrows = buttons(sent["view"])[1]
    assert [(b["label"], b["disabled"]) for b in arrows] == [("◀", False), ("▶", True)]
    assert component_ids(sent["view"])[-2:] == ["rss:c:help_page:0:0", "rss:c:help_page:0:1"]


async def test_a_page_past_the_end_shows_the_last_page(db: Database) -> None:
    interaction = manager(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpPage, "rss:c:help_page:0:99")
    assert interaction.last[1]["embed"].footer.text == "Page 2 of 2"


async def test_a_category_of_one_page_has_no_arrows(db: Database) -> None:
    interaction = manager(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpCategory, "rss:c:help_category:1")

    sent = interaction.last[1]
    assert sent["embed"].title == "Templates"
    assert sent["embed"].footer.text is None
    assert sent["embed"].description.count("**/template ") == 6
    (categories,) = buttons(sent["view"])
    assert [(b["label"], b["disabled"]) for b in categories] == [
        ("Feeds", False),
        ("Templates", True),
    ]


async def test_admin_category_lists_setup_and_access(db: Database) -> None:
    interaction = admin(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpCategory, "rss:c:help_category:2")

    description = interaction.last[1]["embed"].description
    assert interaction.last[1]["embed"].title == "Admin"
    for command in ("setup", "access grant", "access revoke", "access list"):
        assert f"**/{command}**" in description


async def test_a_manager_cannot_open_the_admin_category(db: Database) -> None:
    interaction = manager(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpCategory, f"rss:c:help_category:{ADMIN}")

    sent = interaction.last[1]
    assert sent["embed"].title == "Feeds"
    assert "/access" not in sent["embed"].description
    assert [b["label"] for b in buttons(sent["view"])[0]] == ["Feeds", "Templates"]


async def test_a_click_after_access_was_taken_away_removes_the_menu(db: Database) -> None:
    interaction = FakeInteraction(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpCategory, f"rss:c:help_category:{TEMPLATES}")
    name, sent = interaction.last
    assert name == "edit_message"
    assert sent["content"] == help.NO_ACCESS
    assert sent["embed"] is None and sent["view"] is None


async def test_an_unknown_category_is_out_of_date(db: Database) -> None:
    interaction = admin(db, type=discord.InteractionType.component)
    await click(interaction, help.HelpCategory, "rss:c:help_category:7")
    assert interaction.text == ui.STALE
