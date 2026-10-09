"""/help: what each command does, a category at a time. Open to everyone in a Server.

The text is written by hand. Every command needs an Entry below; tests/test_cmd_help.py
fails when one is missing, left over, or lists the wrong options.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands

from ..access import can_admin, can_manage
from ..models import Level
from . import _ui as ui

PER_PAGE = 6
NO_ACCESS = "You need to be given access by an Admin of this Server to use this bot."


@dataclass(frozen=True, slots=True)
class Entry:
    command: str  # as typed, without the slash: "feed refresh"
    text: str
    options: str = ""  # "<required> [optional]", in the command's own order
    example: str = ""


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    blurb: str
    requires: Level
    entries: tuple[Entry, ...]


# A category's position is what its buttons carry, so add new categories at the end or
# accept that help messages already on screen open a different one.
CATEGORIES = (
    Category(
        "Feeds",
        "Add Feeds, look after them and choose which Items they post.",
        Level.MANAGER,
        (
            Entry(
                "feed add",
                "Opens a form for a new Feed: its address, the channel it posts in, how often "
                "to check it and who to post as. Items the source already lists are not posted.",
            ),
            Entry(
                "feed list",
                "Lists this Server's Feeds with their channel, when each one was last checked "
                "and whether it is working, rate limited, failing or paused, and who paused a "
                "Paused feed. A menu opens any Feed's panel.",
            ),
            Entry(
                "feed history",
                "Shows a Feed's Log entries, newest first, 10 to a page: who added, edited, paused "
                "or resumed it and what they changed, and what the bot reported about it.",
                "<feed>",
                "/feed history feed:Ars Technica",
            ),
            Entry(
                "feed edit",
                "Opens a Feed's panel, where you can change anything about it.",
                "<feed>",
                "/feed edit feed:Ars Technica",
            ),
            Entry(
                "feed remove",
                "Removes a Feed along with its Template and Filters, after asking you to confirm.",
                "<feed>",
                "/feed remove feed:Ars Technica",
            ),
            Entry(
                "feed pause",
                "Stops checking a Feed until you resume it. Nothing is posted while it is paused.",
                "<feed>",
                "/feed pause feed:Ars Technica",
            ),
            Entry(
                "feed resume",
                "Checks a Paused feed again, starting now.",
                "<feed>",
                "/feed resume feed:Ars Technica",
            ),
            Entry(
                "feed refresh",
                "Checks a Feed right now instead of waiting for its next Check, and posts any "
                "new Items. Leave `feed` out to check every Feed that is not paused.",
                "[feed]",
                "/feed refresh feed:Ars Technica",
            ),
            Entry(
                "feed test",
                "Shows you privately what a Feed would post for its newest Item. Nothing goes "
                "to the channel unless you press **Post to channel**.",
                "<feed>",
                "/feed test feed:Ars Technica",
            ),
            Entry(
                "feed import",
                "Adds a Feed for every address in an OPML file, the format feed readers export. "
                "Leave `channel` out to have them post in the channel you are in.",
                "<file> [channel]",
                "/feed import file:feeds.opml channel:#news",
            ),
            Entry(
                "feed export",
                "Sends you this Server's Feeds as an OPML file, to keep as a copy or import "
                "elsewhere.",
            ),
            Entry(
                "filter",
                "Shows a Feed's Filters and lets you add or remove words: must-have words an "
                "Item needs, and block words that stop an Item from being posted.",
                "<feed>",
                "/filter feed:Ars Technica",
            ),
        ),
    ),
    Category(
        "Templates",
        "Change what a Feed posts for each Item.",
        Level.MANAGER,
        (
            Entry(
                "template text",
                "Edits the message text a Feed posts. Placeholders such as `{{title}}` are "
                "replaced by the Item's own details.",
                "<feed>",
                "/template text feed:Ars Technica",
            ),
            Entry(
                "template embed",
                "Edits the Embed posted under the message text: its title, description, link, "
                "image and footer. `colour` is a hex code or `none`; leave it out to keep the "
                "colour.",
                "<feed> [colour]",
                "/template embed feed:Ars Technica colour:#ff8800",
            ),
            Entry(
                "template fields",
                "Adds or removes the Fields of a Feed's Embed.",
                "<feed>",
                "/template fields feed:Ars Technica",
            ),
            Entry(
                "template buttons",
                "Adds or removes the link Buttons under a Feed's message.",
                "<feed>",
                "/template buttons feed:Ars Technica",
            ),
            Entry(
                "template reset",
                "Puts a Feed's Template back to the default after asking you to confirm: the "
                "default message text, no Embed and no Buttons.",
                "<feed>",
                "/template reset feed:Ars Technica",
            ),
            Entry(
                "template placeholders",
                "Lists every Placeholder with the value it has for the Feed's newest Item, so "
                "you can see what each one gives before using it.",
                "<feed>",
                "/template placeholders feed:Ars Technica",
            ),
        ),
    ),
    Category(
        "Admin",
        "Set up the bot and decide who may use it.",
        Level.ADMIN,
        (
            Entry(
                "setup",
                "Chooses the Logs channel, where the bot reports what members did to Feeds and "
                "access, and Feed problems such as Broken feeds and Paused feeds. It is optional, "
                "but it is the only place the bot tells you something is wrong.",
            ),
            Entry(
                "access grant",
                "Makes a role or a member a Manager, who can manage Feeds, or an Admin, who can "
                "also give and take away access. A new Grant replaces the old one.",
                "<target> <level>",
                "/access grant target:@feed-managers level:Manager",
            ),
            Entry(
                "access revoke",
                "Takes away a Grant. Choose the role or member, or give the ID shown in "
                "`/access list` for a role that was deleted or a member who left.",
                "[target] [target_id]",
                "/access revoke target:@feed-managers",
            ),
            Entry(
                "access list",
                "Shows who has been given access, grouped into Admins and Managers.",
            ),
            Entry(
                "log",
                "Shows the Log entries of the whole Server, newest first, 10 to a page: what "
                "members and the bot did to Feeds and access, including Feeds since removed. "
                "Choose a `member` to see only what they did, or a `kind` to see one sort of "
                "Log entry.",
                "[member] [kind]",
                "/log member:@Alex kind:Edits",
            ),
        ),
    ),
)

ENTRIES = {entry.command: entry for category in CATEGORIES for entry in category.entries}


def _visible(interaction: discord.Interaction) -> list[int]:
    """The positions of the categories this member can use, in the order they are shown."""
    level = ui.access_level(interaction)
    return [
        index
        for index, category in enumerate(CATEGORIES)
        if (can_admin(level) if category.requires is Level.ADMIN else can_manage(level))
    ]


def _entry_text(entry: Entry) -> str:
    heading = f"**/{entry.command}**" + (f" `{entry.options}`" if entry.options else "")
    lines = [heading, entry.text]
    if entry.example:
        lines.append(f"Example: `{entry.example}`")
    return "\n".join(lines)


def _panel(
    interaction: discord.Interaction, wanted: int | None, page_number: int
) -> tuple[discord.Embed, discord.ui.View] | None:
    """The help message for one category and page, or None for a member with no access.

    `wanted` is a category's position; one the member cannot see opens their first instead.
    """
    visible = _visible(interaction)
    if not visible:
        return None
    index = wanted if wanted in visible else visible[0]
    category = CATEGORIES[index]
    page = ui.paginate(category.entries, page_number, PER_PAGE)
    body = "\n\n".join([category.blurb, *map(_entry_text, page.items)])
    embed = discord.Embed(
        title=category.name,
        description=ui.cut(body, ui.EMBED_DESCRIPTION_LIMIT),
        colour=discord.Colour.blurple(),
    )
    if page.pages > 1:
        embed.set_footer(text=page.footer)
    view = ui.view_of(
        *(
            HelpCategory(
                other,
                label=CATEGORIES[other].name,
                style=discord.ButtonStyle.primary if other == index else None,
                disabled=other == index,
                row=0,
            )
            for other in visible
        ),
        *ui.page_buttons(
            HelpPage, index, page=page.page, pages=page.pages, labels=("◀", "▶"), row=1
        ),
    )
    return embed, view


async def _show(interaction: discord.Interaction, category: int, page_number: int) -> None:
    if category >= len(CATEGORIES):
        raise ui.UserError(ui.STALE)
    panel = _panel(interaction, category, page_number)
    if panel is None:  # access was taken away while the message was on screen
        await ui.edit(interaction, NO_ACCESS)
        return
    embed, view = panel
    await ui.edit(interaction, embed=embed, view=view)


class HelpCategory(ui.ActionButton, action="help_category", ids=1, requires=None):
    async def handle(self, interaction: discord.Interaction) -> None:
        await _show(interaction, self.ids[0], 0)


class HelpPage(ui.PageButton, action="help_page", ids=2, requires=None):
    async def handle(self, interaction: discord.Interaction) -> None:
        await _show(interaction, self.ids[0], self.page)


@app_commands.command(name="help", description="Show what each command does.")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction) -> None:
    ui.require(interaction, None)  # anyone in a Server; what they see depends on their Level
    panel = _panel(interaction, None, 0)
    if panel is None:
        await ui.reply(interaction, NO_ACCESS)
        return
    embed, view = panel
    await ui.reply(interaction, embed=embed, view=view)


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(help_command)
