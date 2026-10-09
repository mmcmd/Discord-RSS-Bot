"""/log: everything members and the bot did in this Server, newest first. Admins only."""

from __future__ import annotations

from typing import NamedTuple

import discord
from discord import app_commands

from ..models import Level, LogKind
from . import _history as history
from . import _ui as ui

NO_LOG = "This Server has no Log entries yet."
NO_MATCH = "No Log entries match that."


class KindGroup(NamedTuple):
    name: str
    kinds: tuple[LogKind, ...]


# The `kind` choices. A group's number (its position here, from 1) is what the page buttons
# carry, so add new groups at the end. Every LogKind is in exactly one group.
GROUPS = (
    KindGroup("Feeds added and removed", (LogKind.FEED_ADDED, LogKind.FEED_REMOVED)),
    KindGroup("Pauses and resumes", (LogKind.FEED_PAUSED, LogKind.FEED_RESUMED)),
    KindGroup(
        "Edits",
        (
            LogKind.FEED_EDITED,
            LogKind.TEMPLATE_CHANGED,
            LogKind.FILTER_CHANGED,
            LogKind.POST_AS_CHANGED,
            LogKind.MENTIONS_CHANGED,
            LogKind.FORUM_TAGS_CHANGED,
        ),
    ),
    KindGroup(
        "Access",
        (LogKind.GRANT_GIVEN, LogKind.GRANT_TAKEN, LogKind.LOGS_CHANNEL_CHANGED),
    ),
    KindGroup(
        "The bot's own reports",
        (LogKind.FEED_AUTO_PAUSED, LogKind.FEED_BROKEN, LogKind.FEED_WORKING_AGAIN),
    ),
)
KIND_CHOICES = [
    app_commands.Choice(name=group.name, value=number) for number, group in enumerate(GROUPS, 1)
]


async def _log(
    interaction: discord.Interaction, member_id: int, group: int, page_number: int
) -> tuple[str, discord.ui.View | None]:
    """One page of the Server's Log entries, newest first. 0 for no member or no group."""
    if group > len(GROUPS):
        raise ui.UserError(ui.STALE)
    db = ui.deps(interaction).db
    server_id = ui.server_id_of(interaction)
    actor_id = member_id or None
    kinds = GROUPS[group - 1].kinds if group else None
    total = db.count_log_entries(server_id, actor_id=actor_id, kinds=kinds)
    if total == 0:
        return (NO_MATCH if actor_id or kinds else NO_LOG), None
    number, pages = history.page_of(total, page_number)
    entries = db.list_log_entries(
        server_id,
        actor_id=actor_id,
        kinds=kinds,
        limit=history.PAGE_SIZE,
        offset=number * history.PAGE_SIZE,
    )
    filters = [
        *([f"by {ui.member_mention(member_id)}"] if actor_id else []),
        *([GROUPS[group - 1].name] if kinds else []),
    ]
    named = f" ({' · '.join(filters)})" if filters else ""
    content = await history.render(
        interaction,
        entries,
        header=f"**Log entries**{named}: {total}",
        footer=ui.Page(entries, number, pages).footer if pages > 1 else "",
        show_feed=True,
    )
    buttons = ui.page_buttons(LogPage, member_id, group, page=number, pages=pages)
    return content, ui.view_of(*buttons) if buttons else None


class LogPage(ui.PageButton, action="log_page", ids=3, requires=Level.ADMIN):
    """Carries the filters: the member's id and the group's number, 0 for none, then the page."""

    async def handle(self, interaction: discord.Interaction) -> None:
        content, view = await _log(interaction, self.ids[0], self.ids[1], self.page)
        await ui.edit(interaction, content, view=view)


@app_commands.command(name="log", description="Show what members and the bot did in this Server.")
@app_commands.guild_only()
@app_commands.describe(
    member="Only what this member did.",
    kind="Only this kind of Log entry.",
)
@app_commands.choices(kind=KIND_CHOICES)
async def log_command(
    interaction: discord.Interaction,
    member: discord.Member | discord.User | None = None,
    kind: int | None = None,
) -> None:
    ui.require_admin(interaction)
    content, view = await _log(interaction, member.id if member else 0, kind or 0, 0)
    await ui.reply(interaction, content, view=view)


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(log_command)
