"""/setup: choose the Server's Logs channel. Admins only. The worked example for PATTERNS.md."""

from __future__ import annotations

import discord
from discord import app_commands

from ..models import Change, Level, LogKind
from . import _ui as ui

LOGS_CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.news)


def _logs_channel(interaction: discord.Interaction) -> int | None:
    server = ui.deps(interaction).db.get_server(ui.server_id_of(interaction))
    return server.logs_channel_id if server else None


def _change(before: int | None, after: int | None) -> Change:
    old, new = (ui.channel_mention(c) if c is not None else "" for c in (before, after))
    return Change("Logs channel", old, new)


def _panel(interaction: discord.Interaction) -> tuple[str, discord.ui.View]:
    """The /setup message, rebuilt from the database each time it is shown."""
    current = _logs_channel(interaction)
    lines = [
        f"**Logs channel**: {ui.channel_mention(current) if current else 'none set'}",
        "The bot reports what members did to Feeds and access, and Feed problems, "
        "in the Logs channel.",
    ]
    # The Server's cache holds every channel, so one it lacks was deleted.
    deleted = (
        current is not None
        and interaction.guild is not None
        and ui.cached_channel(interaction, current) is None
    )
    if deleted:
        lines.append("**Warning**: that channel no longer exists. Choose another one or clear it.")
    elif current is not None and ui.bot_can_post(interaction, current) is False:
        lines.append(
            f"**Warning**: the bot cannot see or post in {ui.channel_mention(current)}. "
            "Nothing will be reported until the bot is given access to it."
        )
    view = ui.view_of(
        LogsChannelSelect(current=None if deleted else current),
        ClearLogsChannel(disabled=current is None),
    )
    return "\n".join(lines), view


class LogsChannelSelect(ui.ActionSelect, action="setup_logs", requires=Level.ADMIN):
    def build(
        self, custom_id: str, *, current: int | None = None
    ) -> discord.ui.ChannelSelect[discord.ui.View]:
        return discord.ui.ChannelSelect(
            custom_id=custom_id,
            channel_types=list(LOGS_CHANNEL_TYPES),
            placeholder="Choose the Logs channel",
            default_values=[] if current is None else [discord.Object(current)],
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        picked = self.picked_ids
        if not picked:
            raise ui.UserError("Choose a channel to use as the Logs channel.")
        # The Server's cache holds every channel, so one it lacks is not this Server's.
        channel = ui.cached_channel(interaction, picked[0])
        if channel is None:
            raise ui.UserError("The bot cannot find that channel in this Server.")
        if channel.type not in LOGS_CHANNEL_TYPES:
            raise ui.UserError("The Logs channel must be a text or announcement channel.")
        before = _logs_channel(interaction)
        ui.deps(interaction).db.set_logs_channel(ui.server_id_of(interaction), picked[0])
        if picked[0] != before:
            # Started once the channel is stored, so the report goes to the new one.
            ui.record(
                interaction, LogKind.LOGS_CHANNEL_CHANGED, changes=[_change(before, picked[0])]
            )
        content, view = _panel(interaction)
        await ui.edit(interaction, content, view=view)


class ClearLogsChannel(ui.ActionButton, action="setup_clear", requires=Level.ADMIN):
    label = "Clear"

    async def handle(self, interaction: discord.Interaction) -> None:
        before = _logs_channel(interaction)
        if before is not None:
            # The last report goes to the channel being cleared, so it is saved and sent
            # before the channel is forgotten: afterwards there is nowhere to send it.
            await ui.defer(interaction, update=True)
            entry = ui.record(
                interaction,
                LogKind.LOGS_CHANNEL_CHANGED,
                changes=[_change(before, None)],
                announce=False,
            )
            if entry is not None:
                await ui.deps(interaction).journal.announce(entry, ui.actor_of(interaction))
            ui.deps(interaction).db.set_logs_channel(ui.server_id_of(interaction), None)
        content, view = _panel(interaction)
        await ui.edit(interaction, content, view=view)


@app_commands.command(name="setup", description="Choose the Logs channel for this Server.")
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction) -> None:
    ui.require_admin(interaction)
    content, view = _panel(interaction)
    await ui.reply(interaction, content, view=view)


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(setup_command)
