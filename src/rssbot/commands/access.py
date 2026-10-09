"""/access: give and take away Admin and Manager access. Admins only."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import discord
from discord import app_commands

from ..models import Change, Grant, Level, LogKind, TargetKind
from . import _ui as ui

ALWAYS_ADMINS = (
    "The server owner and anyone with Discord's Administrator permission are always Admins."
)
LEVEL_NAMES = {Level.ADMIN: "Admin", Level.MANAGER: "Manager"}
LEVEL_CHOICES = [
    app_commands.Choice(name="Admin", value=Level.ADMIN.value),
    app_commands.Choice(name="Manager", value=Level.MANAGER.value),
]

SCAFFOLDING = 300  # room kept in /access list for headings, labels and the note
MORE_WORDS = 20  # room kept for "…and 12 more"


def _mention(target_id: int, kind: TargetKind) -> str:
    return ui.role_mention(target_id) if kind is TargetKind.ROLE else ui.member_mention(target_id)


def _listed(grant: Grant, guild: Any | None) -> str:
    """How /access list names a Grant, with the ID wherever revoking may need it."""
    if grant.target_kind is TargetKind.MEMBER:
        # Members are not cached, so one who left cannot be told apart: always give the ID.
        return f"{ui.member_mention(grant.target_id)} (`{grant.target_id}`)"
    if guild is not None and guild.get_role(grant.target_id) is None:
        return f"`{grant.target_id}` (deleted role)"
    return ui.role_mention(grant.target_id)


def _fit(mentions: Sequence[str], budget: int) -> str:
    """The mentions on one line, or as many as fit in `budget` followed by "…and N more"."""
    if sum(len(m) + 1 for m in mentions) <= budget:
        return " ".join(mentions)
    kept: list[str] = []
    used = 0
    for mention in mentions:
        if used + len(mention) + 1 > budget - MORE_WORDS:
            break
        kept.append(mention)
        used += len(mention) + 1
    return " ".join([*kept, f"…and {len(mentions) - len(kept)} more"])


def render_grants(grants: Sequence[Grant], guild: Any | None = None) -> str:
    """Admins and Managers, roles and members as mentions; under the message limit.

    `guild` is the Server's cache: a role it lacks was deleted and is shown by its ID.
    """
    if not grants:
        return f"No Grants yet.\n{ALWAYS_ADMINS}"
    budget = ui.MESSAGE_LIMIT - SCAFFOLDING
    groups = [
        (level, kind)
        for level in (Level.ADMIN, Level.MANAGER)
        for kind in (TargetKind.ROLE, TargetKind.MEMBER)
    ]
    wanted = [
        [_listed(g, guild) for g in grants if g.level is level and g.target_kind is kind]
        for level, kind in groups
    ]
    # The smallest group is fitted first, so that room it does not need goes to the others.
    sections = [""] * len(groups)
    by_size = sorted(range(len(groups)), key=lambda i: sum(len(m) + 1 for m in wanted[i]))
    for turn, index in enumerate(by_size):
        text = _fit(wanted[index], budget // (len(groups) - turn))
        budget -= len(text) + 1
        sections[index] = text
    lines: list[str] = []
    for level_index, level in enumerate((Level.ADMIN, Level.MANAGER)):
        roles, members = sections[level_index * 2 : level_index * 2 + 2]
        lines.append(f"**{LEVEL_NAMES[level]}s**")
        if roles:
            lines.append(f"Roles: {roles}")
        if members:
            lines.append(f"Members: {members}")
        if not roles and not members:
            lines.append("none")
    lines.append(ALWAYS_ADMINS)
    return "\n".join(lines)


def _grantable(interaction: discord.Interaction, target: object) -> tuple[int, TargetKind]:
    """The id and kind of a role or member that may be given a Grant, or UserError."""
    if isinstance(target, discord.Role):
        if target.id == interaction.guild_id:  # @everyone has the Server's id
            raise ui.UserError(
                "The bot cannot grant access to @everyone: that would give it to every member. "
                "Choose a role or a member instead."
            )
        return target.id, TargetKind.ROLE
    if isinstance(target, discord.Member):
        if target.bot:
            raise ui.UserError("Bots cannot be given access. Choose a role or a member instead.")
        return target.id, TargetKind.MEMBER
    raise ui.UserError("Choose a role or a member of this Server.")


access_group = app_commands.guild_only()(
    app_commands.Group(name="access", description="Give and take away access to the bot.")
)


@access_group.command(name="grant", description="Make a role or member an Admin or a Manager.")
@app_commands.describe(
    target="The role or member to give access to.",
    level="Managers manage Feeds. Admins also give and take away access.",
)
@app_commands.choices(level=LEVEL_CHOICES)
async def grant_command(
    interaction: discord.Interaction,
    target: discord.Role | discord.Member,
    level: app_commands.Choice[str],
) -> None:
    ui.require_admin(interaction)
    target_id, kind = _grantable(interaction, target)
    chosen = Level(level.value)
    db = ui.deps(interaction).db
    server_id = ui.server_id_of(interaction)
    before = next((g for g in db.list_grants(server_id) if g.target_id == target_id), None)
    db.set_grant(server_id, target_id, kind, chosen)
    if before is None or before.level is not chosen:
        was = "" if before is None else LEVEL_NAMES[before.level]
        ui.record(
            interaction,
            LogKind.GRANT_GIVEN,
            detail=_mention(target_id, kind),
            changes=[Change("Level", was, LEVEL_NAMES[chosen])],
        )
    text = f"Granted {LEVEL_NAMES[chosen]} access to {_mention(target_id, kind)}."
    if before is not None and before.level is not chosen:
        text += f" It was {LEVEL_NAMES[before.level]} before."
    elif before is not None:
        text += " It already had that Grant."
    await ui.reply(interaction, text)


@access_group.command(name="revoke", description="Take away the access given to a role or member.")
@app_commands.describe(
    target="The role or member to take access away from.",
    target_id="The ID of a deleted role or a member who left, as shown in /access list.",
)
async def revoke_command(
    interaction: discord.Interaction,
    target: discord.Role | discord.Member | None = None,
    target_id: str | None = None,
) -> None:
    ui.require_admin(interaction)
    if target is not None:
        found_id = target.id
        kind = TargetKind.ROLE if isinstance(target, discord.Role) else TargetKind.MEMBER
        shown = _mention(found_id, kind)
    elif target_id is not None and target_id.strip().strip("<@&!>").isdecimal():
        # A deleted role or departed member cannot be picked, only named by ID.
        found_id = int(target_id.strip().strip("<@&!>"))
        if found_id > ui.MAX_ID:
            raise ui.UserError("That is not a valid ID.")
        shown = f"`{found_id}`"
    else:
        raise ui.UserError("Choose a role or member, or give the ID of one that no longer exists.")
    db = ui.deps(interaction).db
    server_id = ui.server_id_of(interaction)
    before = next((g for g in db.list_grants(server_id) if g.target_id == found_id), None)
    removed = db.remove_grant(server_id, found_id)
    if removed:
        was = "" if before is None else LEVEL_NAMES[before.level]
        changes = [Change("Level", was, "")] if was else []
        ui.record(interaction, LogKind.GRANT_TAKEN, detail=shown, changes=changes)
        await ui.reply(interaction, f"Took away the Grant of {shown}.")
    else:
        await ui.reply(interaction, f"{shown} has no Grant to take away.")


@access_group.command(name="list", description="Show who has been given access.")
async def list_command(interaction: discord.Interaction) -> None:
    ui.require_admin(interaction)
    grants = ui.deps(interaction).db.list_grants(ui.server_id_of(interaction))
    await ui.reply(interaction, render_grants(grants, interaction.guild))


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(access_group)
