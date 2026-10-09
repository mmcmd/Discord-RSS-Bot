from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands
from fakes_discord import MEMBER_AVATAR, OWNER, SERVER, USER, FakeInteraction

from rssbot.commands import _ui as ui
from rssbot.commands import access
from rssbot.db import Database
from rssbot.models import Actor, Change, Grant, Level, LogEntry, LogKind, TargetKind

ADMIN = app_commands.Choice(name="Admin", value="admin")
MANAGER = app_commands.Choice(name="Manager", value="manager")
ROLE_ID = 50_000_000_000_000_001
MEMBER_ID = 60_000_000_000_000_001


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


class ServerWithRoles:
    """The fake Server plus a role cache: `get_role` finds only the roles it was given."""

    def __init__(self, server: Any, roles: tuple[int, ...]) -> None:
        self._server, self._roles = server, roles

    def get_role(self, role_id: int) -> Any:
        return MagicMock(spec=discord.Role, id=role_id) if role_id in self._roles else None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)


def with_roles(interaction: FakeInteraction, *roles: int) -> FakeInteraction:
    interaction.guild = ServerWithRoles(interaction.guild, roles)  # type: ignore[assignment]
    return interaction


def admin(db: Database, *, roles: tuple[int, ...] = (), **kwargs: Any) -> FakeInteraction:
    return with_roles(FakeInteraction(db, user_id=OWNER, **kwargs), *roles)


def role(role_id: int = ROLE_ID) -> Any:
    target = MagicMock(spec=discord.Role)
    target.id = role_id
    return target


def member(user_id: int = MEMBER_ID, *, bot: bool = False) -> Any:
    target = MagicMock(spec=discord.Member)
    target.id = user_id
    target.bot = bot
    return target


async def grant(interaction: FakeInteraction, target: Any, level: Any) -> None:
    await access.grant_command.callback(interaction, target, level)  # type: ignore[arg-type]


async def revoke(
    interaction: FakeInteraction, target: Any = None, target_id: str | None = None
) -> None:
    await access.revoke_command.callback(interaction, target, target_id)  # type: ignore[arg-type]


async def list_grants(interaction: FakeInteraction) -> None:
    await access.list_command.callback(interaction)  # type: ignore[arg-type]


def sent_text(interaction: FakeInteraction) -> str:
    name, sent = interaction.last
    assert name == "send_message"
    assert sent["ephemeral"] is True
    assert sent["allowed_mentions"] is ui.NO_MENTIONS
    return sent["content"]


# -- The command tree --


def test_tree_serialises_within_discords_limits() -> None:
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents(guilds=True)))
    access.register(tree)
    (command,) = tree.get_commands()
    payload = command.to_dict(tree)
    assert payload["name"] == "access"
    assert payload["contexts"] == [0]
    assert payload.get("default_member_permissions") is None
    assert 1 <= len(payload["description"]) <= 100

    subcommands = {sub["name"]: sub for sub in payload["options"]}
    assert set(subcommands) == {"grant", "revoke", "list"}
    for sub in subcommands.values():
        assert sub["type"] == 1
        assert 1 <= len(sub["description"]) <= 100
        for option in sub.get("options", []):
            assert 1 <= len(option["description"]) <= 100

    target, level = subcommands["grant"]["options"]
    # Type 9 is Discord's mentionable option: the picker offers roles and members.
    assert (target["name"], target["type"], target["required"]) == ("target", 9, True)
    assert (level["name"], level["type"], level["required"]) == ("level", 3, True)
    assert [(c["name"], c["value"]) for c in level["choices"]] == [
        ("Admin", "admin"),
        ("Manager", "manager"),
    ]
    revoke_target, revoke_id = subcommands["revoke"]["options"]
    assert (revoke_id["name"], revoke_id["type"]) == ("target_id", 3)
    assert (revoke_target["name"], revoke_target["type"]) == ("target", 9)
    assert "/access list" in revoke_id["description"]
    assert subcommands["list"].get("options", []) == []


def test_access_registers_no_buttons_selects_or_forms() -> None:
    assert [n for n, cls in ui._ACTIONS.items() if cls.__module__ == access.__name__] == []
    assert [n for n, f in ui._FORMS.items() if f.handler.__module__ == access.__name__] == []
    assert all(name.startswith("acc_") for name in ui._ACTIONS if name.startswith("acc"))


# -- grant --


LEVELS = [(ADMIN, Level.ADMIN, "Admin"), (MANAGER, Level.MANAGER, "Manager")]


@pytest.mark.parametrize(("choice", "level", "word"), LEVELS)
async def test_grant_to_a_role(db: Database, choice: Any, level: Level, word: str) -> None:
    interaction = admin(db)
    await grant(interaction, role(), choice)

    assert db.list_grants(SERVER) == [Grant(SERVER, ROLE_ID, TargetKind.ROLE, level)]
    assert sent_text(interaction) == f"Granted {word} access to <@&{ROLE_ID}>."


@pytest.mark.parametrize(("choice", "level", "word"), LEVELS)
async def test_grant_to_a_member(db: Database, choice: Any, level: Level, word: str) -> None:
    interaction = admin(db)
    await grant(interaction, member(), choice)

    assert db.list_grants(SERVER) == [Grant(SERVER, MEMBER_ID, TargetKind.MEMBER, level)]
    assert sent_text(interaction) == f"Granted {word} access to <@{MEMBER_ID}>."


async def test_a_grant_to_a_member_really_gives_access(db: Database) -> None:
    await grant(admin(db), member(USER), MANAGER)
    ui.require_manager(FakeInteraction(db))  # the default invoker is USER
    with pytest.raises(ui.UserError, match="Only Admins"):
        ui.require_admin(FakeInteraction(db))


async def test_granting_again_replaces_the_level(db: Database) -> None:
    await grant(admin(db), role(), MANAGER)
    interaction = admin(db)
    await grant(interaction, role(), ADMIN)

    assert db.list_grants(SERVER) == [Grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.ADMIN)]
    assert "It was Manager before." in sent_text(interaction)

    interaction = admin(db)
    await grant(interaction, role(), MANAGER)
    assert db.list_grants(SERVER)[0].level is Level.MANAGER
    assert "It was Admin before." in sent_text(interaction)


async def test_granting_the_same_level_again_says_so(db: Database) -> None:
    await grant(admin(db), member(), MANAGER)
    interaction = admin(db)
    await grant(interaction, member(), MANAGER)
    assert len(db.list_grants(SERVER)) == 1
    assert "It already had that Grant." in sent_text(interaction)


async def test_everyone_is_refused(db: Database) -> None:
    everyone = role(SERVER)  # the @everyone role has the Server's id
    for choice in (MANAGER, ADMIN):
        interaction = admin(db)
        with pytest.raises(ui.UserError, match="@everyone"):
            await grant(interaction, everyone, choice)
        assert interaction.calls == []
    assert db.list_grants(SERVER) == []


async def test_the_refusal_of_everyone_is_a_clear_sentence(db: Database) -> None:
    with pytest.raises(ui.UserError) as caught:
        await grant(admin(db), role(SERVER), MANAGER)
    assert "every member" in caught.value.user_message


async def test_a_role_with_the_id_of_another_server_is_not_everyone(db: Database) -> None:
    await grant(admin(db), role(SERVER + 1), MANAGER)
    assert len(db.list_grants(SERVER)) == 1


async def test_bots_are_refused(db: Database) -> None:
    interaction = admin(db)
    with pytest.raises(ui.UserError, match="Bots cannot"):
        await grant(interaction, member(bot=True), ADMIN)
    assert interaction.calls == []
    assert db.list_grants(SERVER) == []


@pytest.mark.parametrize(
    "target", [discord.Object(id=MEMBER_ID), MagicMock(spec=discord.User), None]
)
async def test_something_that_is_not_a_role_or_member_of_the_server_is_refused(
    db: Database, target: Any
) -> None:
    with pytest.raises(ui.UserError, match="role or a member"):
        await grant(admin(db), target, MANAGER)
    assert db.list_grants(SERVER) == []


async def test_grants_are_kept_per_server(db: Database) -> None:
    await grant(admin(db), role(), MANAGER)
    assert db.list_grants(200) == []


# -- revoke --


async def test_revoke_a_role(db: Database) -> None:
    db.set_grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.MANAGER)
    interaction = admin(db)
    await revoke(interaction, role())
    assert db.list_grants(SERVER) == []
    assert sent_text(interaction) == f"Took away the Grant of <@&{ROLE_ID}>."


async def test_revoke_a_member(db: Database) -> None:
    db.set_grant(SERVER, MEMBER_ID, TargetKind.MEMBER, Level.ADMIN)
    db.set_grant(SERVER, MEMBER_ID + 1, TargetKind.MEMBER, Level.ADMIN)
    interaction = admin(db)
    await revoke(interaction, member())
    assert [g.target_id for g in db.list_grants(SERVER)] == [MEMBER_ID + 1]
    assert sent_text(interaction) == f"Took away the Grant of <@{MEMBER_ID}>."


@pytest.mark.parametrize("target", [role(), member()])
async def test_revoke_when_there_is_no_grant(db: Database, target: Any) -> None:
    db.set_grant(SERVER, 77, TargetKind.ROLE, Level.MANAGER)
    interaction = admin(db)
    await revoke(interaction, target)
    assert "has no Grant to take away." in sent_text(interaction)
    assert len(db.list_grants(SERVER)) == 1


async def test_revoke_leaves_other_servers_alone(db: Database) -> None:
    db.set_grant(200, MEMBER_ID, TargetKind.MEMBER, Level.ADMIN)
    await revoke(admin(db), member())
    assert len(db.list_grants(200)) == 1


async def test_revoke_by_id(db: Database) -> None:
    db.set_grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.MANAGER)
    interaction = admin(db)
    await revoke(interaction, target_id=str(ROLE_ID))
    assert db.list_grants(SERVER) == []
    assert sent_text(interaction) == f"Took away the Grant of `{ROLE_ID}`."


@pytest.mark.parametrize("target_id", ["99999999999999999999", str(ui.MAX_ID + 1)])
async def test_revoke_refuses_an_id_too_large_to_be_one(db: Database, target_id: str) -> None:
    db.set_grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.MANAGER)
    interaction = admin(db)
    with pytest.raises(ui.UserError) as caught:
        await revoke(interaction, target_id=target_id)
    assert caught.value.user_message == "That is not a valid ID."
    assert interaction.calls == []
    assert len(db.list_grants(SERVER)) == 1


async def test_revoke_accepts_the_largest_id(db: Database) -> None:
    interaction = admin(db)
    await revoke(interaction, target_id=str(ui.MAX_ID))
    assert sent_text(interaction) == f"`{ui.MAX_ID}` has no Grant to take away."


# -- list --


async def test_list_with_no_grants(db: Database) -> None:
    interaction = admin(db)
    await list_grants(interaction)
    text = sent_text(interaction)
    assert text.startswith("No Grants yet.")
    assert "server owner" in text and "Administrator permission" in text


async def test_list_groups_admins_and_managers_with_roles_and_members(db: Database) -> None:
    db.set_grant(SERVER, 11, TargetKind.ROLE, Level.ADMIN)
    db.set_grant(SERVER, 12, TargetKind.MEMBER, Level.ADMIN)
    db.set_grant(SERVER, 13, TargetKind.MEMBER, Level.ADMIN)
    db.set_grant(SERVER, 21, TargetKind.ROLE, Level.MANAGER)
    db.set_grant(SERVER, 22, TargetKind.ROLE, Level.MANAGER)
    db.set_grant(200, 99, TargetKind.ROLE, Level.MANAGER)  # another Server
    interaction = admin(db, roles=(11, 21, 22))
    await list_grants(interaction)

    assert sent_text(interaction) == "\n".join(
        [
            "**Admins**",
            "Roles: <@&11>",
            "Members: <@12> (`12`) <@13> (`13`)",
            "**Managers**",
            "Roles: <@&21> <@&22>",
            access.ALWAYS_ADMINS,
        ]
    )


async def test_list_with_only_managers_says_there_are_no_admins(db: Database) -> None:
    db.set_grant(SERVER, 12, TargetKind.MEMBER, Level.MANAGER)
    interaction = admin(db)
    await list_grants(interaction)
    assert sent_text(interaction).startswith(
        "**Admins**\nnone\n**Managers**\nMembers: <@12> (`12`)\n"
    )


async def test_list_shows_the_id_of_a_deleted_role_and_it_can_be_revoked(db: Database) -> None:
    db.set_grant(SERVER, 11, TargetKind.ROLE, Level.ADMIN)
    db.set_grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.ADMIN)
    interaction = admin(db, roles=(11,))  # ROLE_ID is no longer in the Server
    await list_grants(interaction)
    text = sent_text(interaction)
    assert f"Roles: <@&11> `{ROLE_ID}` (deleted role)\n" in text
    assert f"<@&{ROLE_ID}>" not in text

    shown_id = text.split("`")[1]
    await revoke(admin(db), target_id=shown_id)
    assert [g.target_id for g in db.list_grants(SERVER)] == [11]


async def test_list_shows_a_members_id_so_one_who_left_can_be_revoked(db: Database) -> None:
    db.set_grant(SERVER, MEMBER_ID, TargetKind.MEMBER, Level.MANAGER)
    interaction = admin(db)
    await list_grants(interaction)
    text = sent_text(interaction)
    assert f"Members: <@{MEMBER_ID}> (`{MEMBER_ID}`)\n" in text

    shown_id = text.split("`")[1]
    await revoke(admin(db), target_id=shown_id)
    assert db.list_grants(SERVER) == []


def test_a_long_list_of_deleted_roles_is_cut_to_fit_the_message_limit() -> None:
    grants = [
        Grant(SERVER, 100_000_000_000_000_000 + n, kind, level)
        for n in range(200)
        for kind in (TargetKind.ROLE, TargetKind.MEMBER)
        for level in (Level.ADMIN, Level.MANAGER)
    ]
    text = access.render_grants(grants, ServerWithRoles(None, ()))
    assert len(text) <= ui.MESSAGE_LIMIT
    assert text.count("…and ") == 4
    assert "(deleted role)" in text
    assert text.endswith(access.ALWAYS_ADMINS)


def test_a_long_list_is_cut_to_fit_the_message_limit() -> None:
    grants = [
        Grant(SERVER, 100_000_000_000_000_000 + n, kind, level)
        for n in range(200)
        for kind in (TargetKind.ROLE, TargetKind.MEMBER)
        for level in (Level.ADMIN, Level.MANAGER)
    ]
    text = access.render_grants(grants)
    assert len(text) <= ui.MESSAGE_LIMIT
    assert text.count("…and ") == 4  # every group says it was cut
    assert text.endswith(access.ALWAYS_ADMINS)
    assert all(label in text for label in ("**Admins**", "**Managers**"))


def test_a_list_that_fits_is_not_cut() -> None:
    grants = [
        Grant(SERVER, 100_000_000_000_000_000 + n, TargetKind.ROLE, Level.ADMIN) for n in range(40)
    ]
    text = access.render_grants(grants)
    assert "…and" not in text
    assert text.count("<@&") == 40


# -- Access --


@pytest.mark.parametrize("level", [None, Level.MANAGER])
async def test_every_subcommand_is_refused_for_anyone_who_is_not_an_admin(
    db: Database, level: Level | None
) -> None:
    if level is not None:
        db.set_grant(SERVER, USER, TargetKind.MEMBER, level)
    db.set_grant(SERVER, MEMBER_ID, TargetKind.MEMBER, Level.MANAGER)
    for run in (
        lambda i: grant(i, member(USER), ADMIN),
        lambda i: revoke(i, member()),
        lambda i: list_grants(i),
    ):
        interaction = FakeInteraction(db)
        with pytest.raises(ui.UserError, match="Only Admins"):
            await run(interaction)
        assert interaction.calls == []
    expected = {(MEMBER_ID, Level.MANAGER)} | ({(USER, level)} if level is not None else set())
    assert {(g.target_id, g.level) for g in db.list_grants(SERVER)} == expected


async def test_a_refused_grant_changes_nothing(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    with pytest.raises(ui.UserError):
        await grant(FakeInteraction(db), member(USER), ADMIN)
    assert db.list_grants(SERVER) == [Grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)]


@pytest.mark.parametrize("who", ["owner", "administrator", "admin grant", "admin role grant"])
async def test_admins_may_use_the_commands(db: Database, who: str) -> None:
    if who == "owner":
        interaction = admin(db)
    elif who == "administrator":
        interaction = FakeInteraction(db, administrator=True)
    elif who == "admin grant":
        db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.ADMIN)
        interaction = FakeInteraction(db)
    else:
        db.set_grant(SERVER, 55, TargetKind.ROLE, Level.ADMIN)
        interaction = FakeInteraction(db, role_ids=(55,))
    await list_grants(with_roles(interaction, 55))
    assert interaction.last[0] == "send_message"


async def test_outside_a_server_is_refused(db: Database) -> None:
    with pytest.raises(ui.UserError, match="only works in a Server"):
        await list_grants(FakeInteraction(db, guild_id=None))


async def test_list_without_a_cached_server_shows_mentions(db: Database) -> None:
    db.set_grant(SERVER, 11, TargetKind.ROLE, Level.ADMIN)
    interaction = FakeInteraction(db, administrator=True)
    interaction.guild = None  # nothing to tell a deleted role by
    await list_grants(interaction)
    assert "Roles: <@&11>\n" in sent_text(interaction)


# -- Log entries --


def logged(db: Database) -> list[LogEntry]:
    """The Server's Log entries, oldest first."""
    return db.list_log_entries(SERVER, limit=100)[::-1]


async def test_a_grant_is_saved_and_reported(db: Database) -> None:
    interaction = admin(db)
    await grant(interaction, role(), MANAGER)
    [entry] = logged(db)
    assert (entry.kind, entry.actor_id, entry.actor_name) == (LogKind.GRANT_GIVEN, OWNER, "Alex")
    assert entry.detail == f"<@&{ROLE_ID}>"
    assert entry.changes == (Change("Level", "", "Manager"),)
    assert (entry.feed_id, entry.feed_name, entry.channel_id) == (None, "", None)
    await interaction.journal.drain()
    assert interaction.reports.sent == [(None, [entry], Actor(OWNER, "Alex", MEMBER_AVATAR))]


async def test_a_changed_level_is_saved_and_the_same_level_again_is_not(db: Database) -> None:
    await grant(admin(db), member(), MANAGER)
    await grant(admin(db, display_name="Robin"), member(), ADMIN)
    await grant(admin(db), member(), ADMIN)
    first, second = logged(db)
    assert first.detail == second.detail == f"<@{MEMBER_ID}>"
    assert second.changes == (Change("Level", "Manager", "Admin"),)
    assert (second.kind, second.actor_name) == (LogKind.GRANT_GIVEN, "Robin")


async def test_a_revoke_is_saved_and_reported(db: Database) -> None:
    db.set_grant(SERVER, ROLE_ID, TargetKind.ROLE, Level.ADMIN)
    db.set_grant(SERVER, MEMBER_ID, TargetKind.MEMBER, Level.MANAGER)
    interaction = admin(db)
    await revoke(interaction, role())
    await revoke(admin(db), None, str(MEMBER_ID))  # by the id of one who left
    by_role, by_id = logged(db)
    assert (by_role.kind, by_role.actor_id) == (LogKind.GRANT_TAKEN, OWNER)
    assert (by_role.detail, by_role.changes) == (f"<@&{ROLE_ID}>", (Change("Level", "Admin", ""),))
    assert (by_id.kind, by_id.detail) == (LogKind.GRANT_TAKEN, f"`{MEMBER_ID}`")
    assert by_id.changes == (Change("Level", "Manager", ""),)
    await interaction.journal.drain()
    assert [entries for _, entries, _ in interaction.reports.sent] == [[by_role]]


async def test_what_changed_no_grant_is_not_saved(db: Database) -> None:
    await revoke(admin(db), role())  # there is no Grant to take away
    await revoke(admin(db), None, "123")
    with pytest.raises(ui.UserError):
        await grant(admin(db), member(bot=True), MANAGER)
    with pytest.raises(ui.UserError):
        await grant(FakeInteraction(db), role(), MANAGER)  # not an Admin
    await list_grants(admin(db))
    assert logged(db) == []
