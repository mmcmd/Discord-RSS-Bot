from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands
from fakes_discord import (
    MEMBER,
    MEMBER_AVATAR,
    OWNER,
    SERVER,
    USER,
    FakeChannel,
    FakeInteraction,
    component_ids,
)

from rssbot.commands import _ui as ui
from rssbot.commands import add_all, setup
from rssbot.db import Database, FeedNotFound
from rssbot.models import Actor, Change, ChannelKind, Feed, Level, LogKind, TargetKind

OTHER_SERVER = 200
ROLE = 50


@pytest.fixture
def db() -> Database:
    return Database(":memory:")


def add_feed(
    db: Database, name: str, server_id: int = SERVER, channel_id: int = 7, url: str | None = None
) -> Feed:
    return db.create_feed(
        server_id=server_id,
        channel_id=channel_id,
        channel_kind=ChannelKind.MESSAGES,
        name=name,
        url=url or f"https://example.com/{name}.xml",
        now=0,
    )


def http_error(
    cls: type[discord.HTTPException] = discord.HTTPException, status: int = 400, code: int = 0
) -> discord.HTTPException:
    return cls(SimpleNamespace(status=status, reason="x"), {"code": code, "message": "x"})  # type: ignore[arg-type]


ALREADY_ACKNOWLEDGED = 40060


def fail_once(interaction: FakeInteraction, method: str, error: Exception) -> None:
    """Make one response method raise the first time, as when Discord refuses the call."""
    original = getattr(interaction.response, method)

    async def failing(**kwargs: Any) -> None:
        setattr(interaction.response, method, original)
        raise error

    setattr(interaction.response, method, failing)


# -- The command tree --


def _walk(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = [payload]
    for option in payload.get("options", []):
        found.extend(_walk(option))
    return found


def test_tree_serialises_within_discords_limits() -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    tree = app_commands.CommandTree(client)
    add_all(tree, client)

    commands = tree.get_commands()
    assert [c.name for c in commands] == [
        "setup",
        "access",
        "feed",
        "template",
        "filter",
        "log",
        "help",
    ]
    assert len(commands) <= 100
    assert tree.on_error is ui.on_tree_error
    for command in commands:
        payload = command.to_dict(tree)
        assert payload["contexts"] == [0]  # Servers only
        assert payload.get("default_member_permissions") is None  # visible to everyone
        for node in _walk(payload):
            assert 1 <= len(node["name"]) <= 32
            assert 1 <= len(node["description"]) <= 100
            assert len(node.get("options", [])) <= 25
            assert len(node.get("choices", [])) <= 25


def test_feed_option_registers_with_postponed_annotations() -> None:
    # This module uses `from __future__ import annotations`, as command modules do.
    @app_commands.command(name="example", description="An example.")
    @app_commands.guild_only()
    @app_commands.describe(feed="The Feed to act on.", times="How many times.")
    @app_commands.autocomplete(feed=ui.feed_autocomplete)
    async def example(interaction: discord.Interaction, feed: str, times: int = 1) -> None:
        pass

    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents(guilds=True)))
    tree.add_command(example)
    feed, times = example.to_dict(tree)["options"]
    assert (feed["type"], feed["autocomplete"], feed["required"]) == (3, True, True)
    assert (times["type"], times["required"]) == (4, False)


def test_registration_adds_every_action_and_chains_on_interaction() -> None:
    seen = []

    class Client:
        def __init__(self) -> None:
            self.added: tuple[Any, ...] = ()

        def add_dynamic_items(self, *items: Any) -> None:
            self.added = items

        async def on_interaction(self, interaction: Any) -> None:
            seen.append(interaction)

    client = Client()
    ui.register_dynamic_items(client)
    hooked = client.on_interaction
    ui.register_dynamic_items(client)

    assert ui.CancelButton in client.added
    assert client.on_interaction is hooked  # hooked once


async def test_hooked_on_interaction_still_calls_the_clients_own(db: Database) -> None:
    seen = []

    class Client:
        def add_dynamic_items(self, *items: Any) -> None:
            pass

        async def on_interaction(self, interaction: Any) -> None:
            seen.append(interaction)

    client = Client()
    ui.register_dynamic_items(client)
    interaction = FakeInteraction(db)
    await client.on_interaction(interaction)
    assert seen == [interaction]
    assert interaction.calls == []


# -- Custom ids --


def test_ids_round_trip() -> None:
    assert ui.component_id("feed_delete", 12) == "rss:c:feed_delete:12"
    assert ui.modal_id("feed_edit", 12, 0) == "rss:m:feed_edit:12:0"
    assert ui.parse_custom_id("rss:c:feed_delete:12") == ("c", "feed_delete", (12,))
    assert ui.parse_custom_id("rss:m:x") == ("m", "x", ())
    big = ui.component_id("a", ui.MAX_ID, ui.MAX_ID)
    assert ui.parse_custom_id(big) == ("c", "a", (ui.MAX_ID, ui.MAX_ID))


@pytest.mark.parametrize(
    "hostile",
    [
        "",
        "rss",
        "rss:c",
        "rss:c:",
        "rss:x:feed:1",
        "rss:c:Feed:1",
        "rss:c:feed:1 ",
        " rss:c:feed:1",
        "rss:c:feed:1\n",
        "rss:c:feed:-1",
        "rss:c:feed:+1",
        "rss:c:feed:01",
        "rss:c:feed:1.0",
        "rss:c:feed:1e3",
        "rss:c:feed:0x10",
        "rss:c:feed:",
        "rss:c:feed::1",
        "rss:c:feed:1:",
        "rss:c:feed:١٢",  # digits, but not ASCII
        "rss:c:feed:1;DROP TABLE feeds",
        "rss:c:feed:9223372036854775808",  # one past what SQLite stores
        "rss:c:feed:99999999999999999999999",
        "other:c:feed:1",
        "rss:c:" + "a" * 41,
        "rss:c:feed" + ":1" * 60,  # over 100 characters
        None,
        12,
    ],
)
def test_hostile_ids_are_rejected(hostile: object) -> None:
    assert ui.parse_custom_id(hostile) is None


@pytest.mark.parametrize("bad", [-1, ui.MAX_ID + 1, True, "1", 1.0])
def test_encoding_refuses_bad_ids(bad: Any) -> None:
    with pytest.raises(ValueError):
        ui.component_id("feed", bad)


def test_encoding_refuses_bad_actions_and_long_ids() -> None:
    with pytest.raises(ValueError):
        ui.component_id("Feed:1")
    with pytest.raises(ValueError):
        ui.component_id("a", *([ui.MAX_ID] * 6))


# -- Stateless buttons and selects --

handled: list[tuple[str, tuple[int, ...]]] = []


class ManagerButton(ui.ActionButton, action="t_manager", ids=1, requires=Level.MANAGER):
    label = "Go"

    async def handle(self, interaction: discord.Interaction) -> None:
        handled.append(("manager", self.ids))
        await ui.edit(interaction, "done")


class FailingButton(ui.ActionButton, action="t_fail", requires=None):
    async def handle(self, interaction: discord.Interaction) -> None:
        raise RuntimeError("boom")


class Pager(ui.PageButton, action="t_page", ids=2, requires=Level.MANAGER):
    async def handle(self, interaction: discord.Interaction) -> None:
        handled.append(("page", self.ids))


class Options(ui.ActionSelect, action="t_options", requires=None):
    def build(self, custom_id: str) -> discord.ui.Select[Any]:
        return discord.ui.Select(custom_id=custom_id)

    async def handle(self, interaction: discord.Interaction) -> None:
        pass


@pytest.fixture(autouse=True)
def _clear_handled() -> None:
    handled.clear()


def template(cls: type[ui.Action]) -> Any:
    return cls.__discord_ui_compiled_template__


def test_action_classes_declare_themselves_strictly() -> None:
    assert ManagerButton(5).custom_id == "rss:c:t_manager:5"
    assert template(ManagerButton).fullmatch("rss:c:t_manager:5")
    for suffix in ("", ":5:6", ":05", ":-5", ":5\n", "_x:5", ":5 "):
        assert template(ManagerButton).fullmatch("rss:c:t_manager" + suffix) is None
    with pytest.raises(ValueError):
        ManagerButton()
    with pytest.raises(TypeError):  # the Level must be stated

        class NoLevel(ui.ActionButton, action="t_no_level"):
            pass

    with pytest.raises(ValueError):  # one class per action

        class Duplicate(ui.ActionButton, action="t_manager", requires=None):
            pass

    with pytest.raises(ValueError):  # its longest custom id would not fit

        class TooLong(ui.ActionButton, action="t_long", ids=5, requires=None):
            pass


def test_templates_never_overlap() -> None:
    for cls in ui._ACTIONS.values():
        example = ui.component_id(cls.action, *range(cls.id_count))
        matching = [c for c in ui._ACTIONS.values() if template(c).fullmatch(example)]
        assert matching == [cls]


async def test_click_runs_for_a_manager_and_rebuilds_from_the_id(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = FakeInteraction(db, type=discord.InteractionType.component)
    match = template(ManagerButton).fullmatch("rss:c:t_manager:42")
    item = await ManagerButton.from_custom_id(interaction, None, match)
    await item.callback(interaction)
    assert handled == [("manager", (42,))]
    assert interaction.last == (
        "edit_message",
        {"content": "done", "embed": None, "view": None, "allowed_mentions": ui.NO_MENTIONS},
    )


async def test_click_is_refused_without_access(db: Database) -> None:
    interaction = FakeInteraction(db)
    await ManagerButton(42).callback(interaction)
    assert handled == []
    assert interaction.text == ui.NEED_MANAGER
    assert interaction.last[1]["ephemeral"] is True


async def test_click_is_refused_outside_a_server(db: Database) -> None:
    interaction = FakeInteraction(db, guild_id=None)
    await FailingButton().callback(interaction)
    assert interaction.text == ui.SERVER_ONLY


async def test_oversized_id_is_refused_not_dropped(db: Database) -> None:
    interaction = FakeInteraction(db, administrator=True)
    match = template(ManagerButton).fullmatch("rss:c:t_manager:9999999999999999999")
    item = await ManagerButton.from_custom_id(interaction, None, match)
    await item.callback(interaction)
    assert handled == []
    assert interaction.text == ui.STALE


async def test_failing_click_is_logged_and_answered(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = FakeInteraction(db)
    with caplog.at_level(logging.ERROR):
        await FailingButton().callback(interaction)
    assert interaction.text == ui.UNEXPECTED
    assert "boom" in caplog.text


async def test_select_reports_what_was_picked() -> None:
    select = Options()
    select.item._values = ["a", "12"]  # type: ignore[attr-defined]
    assert select.picked == ["a", "12"]
    assert select.picked_ids == [12]
    select.item._values = [discord.Object(34)]  # type: ignore[attr-defined]
    assert select.picked_ids == [34]


async def test_unknown_control_is_answered(db: Database) -> None:
    interaction = FakeInteraction(
        db, type=discord.InteractionType.component, data={"custom_id": "rss:c:removed_action:1"}
    )
    await ui.handle_interaction(interaction)
    assert interaction.text == ui.STALE


async def test_foreign_controls_are_left_alone(db: Database) -> None:
    for custom_id in ("someone_elses_button", None):
        interaction = FakeInteraction(
            db, type=discord.InteractionType.component, data={"custom_id": custom_id}
        )
        await ui.handle_interaction(interaction)
        assert interaction.calls == []


# -- Access --


def test_owner_and_administrator_are_admins(db: Database) -> None:
    assert ui.access_level(FakeInteraction(db, user_id=OWNER)) is Level.ADMIN
    assert ui.access_level(FakeInteraction(db, administrator=True)) is Level.ADMIN


def test_granted_role_and_member(db: Database) -> None:
    db.set_grant(SERVER, ROLE, TargetKind.ROLE, Level.MANAGER)
    assert ui.access_level(FakeInteraction(db, role_ids=(ROLE,))) is Level.MANAGER
    assert ui.access_level(FakeInteraction(db, role_ids=(ROLE + 1,))) is None
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.ADMIN)
    assert ui.access_level(FakeInteraction(db)) is Level.ADMIN


def test_nobody_and_direct_messages_have_no_access(db: Database) -> None:
    assert ui.access_level(FakeInteraction(db)) is None
    assert ui.access_level(FakeInteraction(db, guild_id=None, administrator=True)) is None


def test_another_servers_grants_do_not_count(db: Database) -> None:
    db.set_grant(OTHER_SERVER, USER, TargetKind.MEMBER, Level.ADMIN)
    db.set_grant(OTHER_SERVER, ROLE, TargetKind.ROLE, Level.ADMIN)
    assert ui.access_level(FakeInteraction(db, role_ids=(ROLE,))) is None
    # The owner of one Server is nobody in another.
    assert ui.access_level(FakeInteraction(db, guild_id=OTHER_SERVER, owner_id=999)) is Level.ADMIN
    assert ui.access_level(FakeInteraction(db, user_id=OWNER, owner_id=999)) is None


def test_guards(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    manager = FakeInteraction(db)
    ui.require_manager(manager)
    with pytest.raises(ui.UserError, match="Only Admins"):
        ui.require_admin(manager)
    ui.require_admin(FakeInteraction(db, user_id=OWNER))
    with pytest.raises(ui.UserError, match="Managers and Admins"):
        ui.require_manager(FakeInteraction(db, user_id=3))
    with pytest.raises(ui.UserError, match="only works in a Server"):
        ui.require(FakeInteraction(db, guild_id=None), None)


def test_feed_of_only_returns_this_servers_feeds(db: Database) -> None:
    mine = add_feed(db, "mine")
    theirs = add_feed(db, "theirs", server_id=OTHER_SERVER)
    interaction = FakeInteraction(db)
    assert ui.feed_of(interaction, mine.id) == mine
    for feed_id in (theirs.id, 9999, -1, ui.MAX_ID + 1):
        with pytest.raises(ui.UserError, match="no longer exists"):
            ui.feed_of(interaction, feed_id)


# -- Errors --


class ServiceError(Exception):
    """Stands in for the application layer's error, recognised by its name."""


class FeedLimitReached(ServiceError):
    user_message = "This Server has reached its Feed limit."


def test_expected_errors_are_recognised() -> None:
    assert ui.user_message(ui.UserError("Nope.")) == "Nope."
    assert ui.user_message(ServiceError("That URL is not a feed.")) == "That URL is not a feed."
    assert ui.user_message(FeedLimitReached()) == "This Server has reached its Feed limit."
    wrapped = app_commands.CommandInvokeError(setup.setup_command, ui.UserError("Nope."))
    assert ui.user_message(wrapped) == "Nope."
    assert ui.user_message(ValueError("internal detail")) is None
    assert ui.user_message(ServiceError()) is None


async def test_expected_error_is_shown_and_not_logged(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = FakeInteraction(db)
    error = app_commands.CommandInvokeError(setup.setup_command, ui.UserError("Nope @everyone."))
    with caplog.at_level(logging.DEBUG):
        await ui.on_tree_error(interaction, error)
    assert interaction.calls == [
        (
            "send_message",
            {"content": "Nope @everyone.", "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]
    # Nothing but the command's own line: an expected error is no warning and has no traceback.
    [record] = caplog.records
    assert (record.name, record.levelno, record.exc_info) == ("rssbot.commands", logging.INFO, None)


async def test_unexpected_error_is_logged_with_its_traceback(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = FakeInteraction(db)
    await interaction.response.defer()
    try:
        raise KeyError("secret detail")
    except KeyError as exc:
        error = app_commands.CommandInvokeError(setup.setup_command, exc)
    with caplog.at_level(logging.ERROR):
        await ui.on_tree_error(interaction, error)
    assert interaction.last[0] == "followup"  # already responded: follows up instead
    assert interaction.text == ui.UNEXPECTED
    assert "secret detail" in caplog.text and "Traceback" in caplog.text


async def test_reporting_survives_a_dead_interaction(db: Database) -> None:
    interaction = FakeInteraction(db)

    async def broken(**kwargs: Any) -> None:
        raise RuntimeError("Unknown interaction")

    interaction.response.send_message = broken  # type: ignore[method-assign]
    await ui.report_error(interaction, ui.UserError("Nope."))


def test_more_expected_errors_get_a_plain_sentence() -> None:
    assert ui.user_message(FeedNotFound(12)) == ui.FEED_GONE
    wrapped = app_commands.CommandInvokeError(setup.setup_command, FeedNotFound(12))
    assert ui.user_message(wrapped) == ui.FEED_GONE
    assert ui.user_message(http_error(discord.Forbidden, 403, 50013)) == ui.MISSING_PERMISSION
    assert ui.user_message(http_error(discord.NotFound, 404, 10008)) == ui.TARGET_GONE
    assert ui.user_message(http_error(code=ALREADY_ACKNOWLEDGED)) == ui.NOT_HANDLED
    assert ui.user_message(KeyError("internal detail")) is None
    assert ui.user_message(http_error(status=500)) is None


class GoneFeedButton(ui.ActionButton, action="t_gone_feed", requires=None):
    async def handle(self, interaction: discord.Interaction) -> None:
        ui.feed_of(interaction, 9999)


def panel_click(db: Database) -> FakeInteraction:
    interaction = FakeInteraction(db, type=discord.InteractionType.component)
    interaction.message = SimpleNamespace(id=1)
    return interaction


async def test_click_on_a_deleted_feeds_panel_replaces_the_panel(db: Database) -> None:
    interaction = panel_click(db)
    await GoneFeedButton().callback(interaction)
    assert interaction.calls == [
        (
            "edit_message",
            {
                "content": ui.FEED_GONE,
                "embed": None,
                "view": None,  # no controls left to click
                "allowed_mentions": ui.NO_MENTIONS,
            },
        )
    ]


async def test_deleted_feed_is_a_reply_when_there_is_no_panel_to_replace(db: Database) -> None:
    command = FakeInteraction(db)
    await ui.report_error(command, ui.UserError(ui.FEED_GONE))
    assert command.last[0] == "send_message"
    assert command.text == ui.FEED_GONE

    refused = panel_click(db)
    fail_once(refused, "edit_message", http_error(discord.NotFound, 404, 10008))
    await ui.report_error(refused, ui.UserError(ui.FEED_GONE))
    assert refused.last[0] == "send_message"
    assert refused.text == ui.FEED_GONE
    assert refused.last[1]["ephemeral"] is True


async def test_other_errors_leave_the_panel_alone(db: Database) -> None:
    interaction = panel_click(db)
    await ui.report_error(interaction, ui.UserError("Nope."))
    assert [name for name, _ in interaction.calls] == ["send_message"]
    assert interaction.text == "Nope."


# -- Replies --


async def test_reply_edit_and_defer(db: Database) -> None:
    command = FakeInteraction(db)
    await ui.defer(command, update=True)  # a command cannot "update": it thinks privately
    assert command.last == ("defer", {"ephemeral": True, "thinking": True})
    await ui.defer(command)  # already deferred: nothing more
    await ui.reply(command, "x" * 3000)
    assert command.last[0] == "followup"
    assert len(command.text) == ui.MESSAGE_LIMIT
    assert len(command.calls) == 2

    click = FakeInteraction(db, type=discord.InteractionType.component)
    await ui.defer(click, update=True)
    assert click.last == ("defer", {})
    await ui.edit(click, "new")
    assert click.last[0] == "edit_original_response"
    assert click.last[1]["allowed_mentions"] is ui.NO_MENTIONS


async def test_reply_follows_up_when_discord_says_already_acknowledged(db: Database) -> None:
    # The watchdog's defer was in flight, so is_done() was still False.
    interaction = FakeInteraction(db)
    fail_once(interaction, "send_message", http_error(code=ALREADY_ACKNOWLEDGED))
    await ui.reply(interaction, "Saved.")
    assert interaction.calls == [
        ("followup", {"content": "Saved.", "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS})
    ]


async def test_click_that_races_the_watchdog_still_shows_its_result(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = panel_click(db)
    fail_once(interaction, "edit_message", http_error(code=ALREADY_ACKNOWLEDGED))
    with caplog.at_level(logging.WARNING):
        await ManagerButton(42).callback(interaction)
    assert [name for name, _ in interaction.calls] == ["edit_original_response"]
    assert interaction.text == "done"
    assert caplog.records == []


async def test_defer_that_races_the_watchdog_is_not_an_error(db: Database) -> None:
    interaction = FakeInteraction(db)
    fail_once(interaction, "defer", http_error(code=ALREADY_ACKNOWLEDGED))
    await ui.defer(interaction)
    assert interaction.calls == []


async def test_other_refusals_from_discord_are_not_swallowed(db: Database) -> None:
    for method, call in (("send_message", ui.reply), ("edit_message", ui.edit)):
        interaction = FakeInteraction(db, type=discord.InteractionType.component)
        fail_once(interaction, method, http_error(code=50035))
        with pytest.raises(discord.HTTPException):
            await call(interaction, "x")
        assert interaction.calls == []


async def test_edit_in_a_form_opened_by_a_slash_command_replies(db: Database) -> None:
    form = FakeInteraction(db, type=discord.InteractionType.modal_submit)
    await ui.edit(form, "Saved.")
    assert form.calls == [
        (
            "send_message",
            {"content": "Saved.", "ephemeral": True, "allowed_mentions": ui.NO_MENTIONS},
        )
    ]

    deferred = FakeInteraction(db, type=discord.InteractionType.modal_submit)
    await ui.defer(deferred, update=True)
    await ui.edit(deferred, "Saved.")
    assert [name for name, _ in deferred.calls] == ["defer", "followup"]
    assert deferred.text == "Saved."


async def test_edit_in_a_form_opened_by_a_button_replaces_the_message(db: Database) -> None:
    form = FakeInteraction(db, type=discord.InteractionType.modal_submit)
    form.message = SimpleNamespace(id=1)
    await ui.edit(form, "Saved.")
    assert form.last[0] == "edit_message"
    await ui.edit(form, "Saved again.")
    assert form.last[0] == "edit_original_response"
    assert form.text == "Saved again."


# -- Feed option --


async def test_autocomplete_lists_this_servers_feeds_by_substring(db: Database) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    news = add_feed(db, "Daily News", channel_id=7)
    add_feed(db, "Comics")
    add_feed(db, "Other news", server_id=OTHER_SERVER)
    interaction = FakeInteraction(db, channels=(FakeChannel(7, "feeds"),))

    choices = await ui.feed_autocomplete(interaction, " NEWS ")
    assert [(c.name, c.value) for c in choices] == [("Daily News (#feeds)", ui.feed_value(news.id))]
    assert len(await ui.feed_autocomplete(interaction, "")) == 2
    assert await ui.feed_autocomplete(interaction, "zzz") == []


async def test_autocomplete_also_matches_the_address(db: Database) -> None:
    feed = add_feed(db, "Daily News", url="https://blog.example.org/rss")
    add_feed(db, "Comics")
    interaction = FakeInteraction(db, administrator=True)
    choices = await ui.feed_autocomplete(interaction, "BLOG.example")
    assert [(c.name, c.value) for c in choices] == [("Daily News", ui.feed_value(feed.id))]


async def test_autocomplete_tells_same_named_feeds_apart(db: Database) -> None:
    long = "https://c.example/" + "p" * 60
    feeds = [
        add_feed(db, "News", url="https://a.example/feed.xml"),
        add_feed(db, "News", url="https://b.example/one.xml"),
        add_feed(db, "News", url="https://b.example/two.xml"),
        add_feed(db, "News", url=long + "/1"),
        add_feed(db, "News", url=long + "/2"),
        add_feed(db, "News", channel_id=8, url="https://a.example/feed.xml"),
        add_feed(db, "Comics"),
    ]
    channels = (FakeChannel(7, "feeds"), FakeChannel(8, "other"))
    interaction = FakeInteraction(db, administrator=True, channels=channels)

    choices = await ui.feed_autocomplete(interaction, "")
    assert [c.value for c in choices] == [ui.feed_value(feed.id) for feed in [feeds[6], *feeds[:6]]]
    names = [c.name for c in choices]
    assert names == [
        "Comics (#feeds)",
        "News · a.example (#feeds)",
        "News · b.example/one.xml (#feeds)",
        "News · b.example/two.xml (#feeds)",
        f"News · Feed {feeds[3].id} (#feeds)",
        f"News · Feed {feeds[4].id} (#feeds)",
        "News (#other)",  # already told apart by its channel
    ]


async def test_autocomplete_tells_long_same_named_feeds_apart_within_the_limit(
    db: Database,
) -> None:
    for host in ("a.example", "b.example"):
        add_feed(db, "n" * 200, url=f"https://{host}/feed.xml")
    interaction = FakeInteraction(db, administrator=True, channels=(FakeChannel(7, "c" * 100),))
    names = [c.name for c in await ui.feed_autocomplete(interaction, "")]
    assert len(set(names)) == 2
    assert all(len(name) <= 100 for name in names)
    assert "a.example" in names[0] and "b.example" in names[1]


async def test_autocomplete_caps_at_25_and_keeps_names_short(db: Database) -> None:
    for number in range(30):
        add_feed(db, f"{number:02d} " + "n" * 200)
    choices = await ui.feed_autocomplete(FakeInteraction(db, administrator=True), "")
    assert len(choices) == 25
    assert all(1 <= len(c.name) <= 100 for c in choices)


async def test_autocomplete_shows_nothing_without_access_or_on_failure(db: Database) -> None:
    add_feed(db, "News")
    assert await ui.feed_autocomplete(FakeInteraction(db), "") == []
    assert await ui.feed_autocomplete(FakeInteraction(db, guild_id=None), "") == []
    broken = FakeInteraction(db, administrator=True)
    broken.client.deps = None
    assert await ui.feed_autocomplete(broken, "") == []


def test_feed_option_parsing(db: Database) -> None:
    feed = add_feed(db, "News")
    assert ui.parse_feed_option(f" {ui.feed_value(feed.id)} ") == feed.id
    assert ui.feed_from_option(FakeInteraction(db), ui.feed_value(feed.id)) == feed
    # A number typed by hand is not a picked Feed.
    for typed in (str(feed.id), "News", "", "id:", "id:-1", "id:1.5", "id:0x1", "id:" + "9" * 20):
        with pytest.raises(ui.UserError, match="Choose a Feed"):
            ui.parse_feed_option(typed)


# -- Pagination --


def test_paginate_clamps() -> None:
    items = list(range(25))
    assert ui.paginate(items, 0) == (list(range(10)), 0, 3)
    assert ui.paginate(items, 2).items == [20, 21, 22, 23, 24]
    assert ui.paginate(items, 99).page == 2
    assert ui.paginate(items, -5).page == 0
    assert ui.paginate([], 3) == ([], 0, 1)
    assert ui.paginate(items, 1).footer == "Page 2 of 3"


def test_page_buttons_carry_the_target_page() -> None:
    def state(page: int, pages: int) -> list[tuple[str, bool]]:
        buttons = ui.page_buttons(Pager, 8, page=page, pages=pages)
        return [(b.custom_id, b.item.disabled) for b in buttons]  # type: ignore[attr-defined]

    assert state(0, 1) == []
    assert state(0, 3) == [("rss:c:t_page:8:0", True), ("rss:c:t_page:8:1", False)]
    assert state(1, 3) == [("rss:c:t_page:8:0", False), ("rss:c:t_page:8:2", False)]
    assert state(2, 3) == [("rss:c:t_page:8:1", False), ("rss:c:t_page:8:2", True)]
    assert state(99, 2) == [("rss:c:t_page:8:0", False), ("rss:c:t_page:8:1", True)]
    assert Pager(8, 2).page == 2


# -- Confirm --


async def test_confirm_view_and_cancel(db: Database) -> None:
    view = ui.confirm_view(ManagerButton(5, label="Delete", style=discord.ButtonStyle.danger))
    assert component_ids(view) == ["rss:c:t_manager:5", "rss:c:cancel"]
    assert view.timeout is None
    assert len(view.to_components()) == 1

    interaction = FakeInteraction(db, type=discord.InteractionType.component)
    await ui.CancelButton().callback(interaction)
    assert interaction.last[0] == "edit_message"
    assert interaction.last[1]["content"] == "Cancelled."
    assert interaction.last[1]["view"] is None


# -- Text --


def test_text_helpers() -> None:
    assert ui.cut("short", 10) == "short"
    assert ui.cut("a" * 20, 10) == "a" * 9 + "…"
    assert ui.cut("word " * 5, 6) == "word…"
    assert ui.cut("abc", 1) == "a"
    assert ui.channel_mention(5) == "<#5>"
    assert ui.role_mention(5) == "<@&5>"
    assert ui.member_mention(5) == "<@5>"


def test_bot_can_post(db: Database) -> None:
    channels = (FakeChannel(1), FakeChannel(2, bot_can_post=False))
    interaction = FakeInteraction(db, channels=channels)
    assert ui.bot_can_post(interaction, 1) is True
    assert ui.bot_can_post(interaction, 2) is False
    assert ui.bot_can_post(interaction, 3) is None


def test_bot_can_post_needs_the_permissions_the_channel_and_message_ask_for(db: Database) -> None:
    channels = (
        FakeChannel(1, lacking=("embed_links",)),
        FakeChannel(
            2, type=discord.ChannelType.public_thread, lacking=("send_messages_in_threads",)
        ),
        FakeChannel(3, lacking=("send_messages_in_threads",)),
    )
    interaction = FakeInteraction(db, channels=channels)
    assert ui.bot_can_post(interaction, 1) is True  # no Embed is posted
    assert ui.bot_can_post(interaction, 1, embed=True) is False
    assert ui.missing_post_permissions(interaction, 1, embed=True) == ["Embed Links"]
    assert ui.bot_can_post(interaction, 2) is False
    assert ui.missing_post_permissions(interaction, 2) == ["Send Messages in Threads"]
    assert ui.bot_can_post(interaction, 3) is True  # only threads need that permission
    assert ui.missing_post_permissions(interaction, 4) is None


# -- Pop-up forms --

submitted: list[tuple[tuple[int, ...], ui.FormValues]] = []


@ui.form_handler("t_edit", ids=1, requires=Level.MANAGER)
async def on_edit(interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues):
    submitted.append((ids, values))
    await ui.defer(interaction)
    await ui.reply(interaction, "Saved.")


TEXT_ONLY = [discord.ChannelType.text]
INTERVALS = [("600", "10 minutes"), ("3600", "1 hour")]


def fields() -> list[Any]:
    return [
        ui.text_field("name", "Name", default="News", max_length=80),
        ui.text_field("text", "Message text", long=True, required=False, description="Markdown"),
        ui.channel_field("channel", "Channel", channel_types=TEXT_ONLY, default_id=7),
        ui.choice_field("interval", "Check every", INTERVALS, default="600"),
        ui.role_field("roles", "Roles to mention", default_ids=[50], max_values=3),
    ]


SUBMISSION = {
    "custom_id": "rss:m:t_edit:12",
    "components": [
        {"type": 18, "component": {"type": 4, "custom_id": "name", "value": "  Tech  "}},
        {"type": 1, "components": [{"type": 4, "custom_id": "text", "value": ""}]},
        {"type": 18, "component": {"type": 8, "custom_id": "channel", "values": ["77"]}},
        {"type": 18, "component": {"type": 3, "custom_id": "interval", "values": ["3600"]}},
        {"type": 18, "component": {"type": 6, "custom_id": "roles", "values": ["50", "51", "x"]}},
    ],
    "resolved": {"channels": {"77": {"id": "77", "type": 15, "name": "articles"}}},
}


async def test_form_serialises_and_is_not_remembered() -> None:
    modal = ui.build_form("t_edit", 12, title="Edit Feed " + "x" * 80, fields=fields())
    assert modal.is_finished()  # so send_modal() stores nothing
    payload = modal.to_dict()
    assert payload["custom_id"] == "rss:m:t_edit:12"
    assert len(payload["title"]) == 45
    kinds = [(c["type"], c["component"]["type"]) for c in payload["components"]]
    assert kinds == [(18, 4), (18, 4), (18, 8), (18, 3), (18, 6)]
    name, text, channel, interval, roles = (c["component"] for c in payload["components"])
    assert name["value"] == "News" and name["custom_id"] == "name" and name["max_length"] == 80
    assert text["style"] == 2 and text["required"] is False
    assert channel["channel_types"] == [0]
    assert channel["default_values"] == [{"id": 7, "type": "channel"}]
    assert [o["default"] for o in interval["options"]] == [True, False]
    assert roles["default_values"] == [{"id": 50, "type": "role"}] and roles["max_values"] == 3


async def test_form_limits() -> None:
    with pytest.raises(ValueError):
        ui.build_form("t_edit", 12, title="Edit", fields=fields() + fields())
    with pytest.raises(ValueError):
        ui.build_form("t_edit", title="Edit", fields=fields())  # wrong number of ids
    with pytest.raises(ValueError):
        ui.build_form("t_unknown", title="Edit", fields=fields())


async def test_show_form_is_the_first_response(db: Database) -> None:
    interaction = FakeInteraction(db)
    await ui.show_form(interaction, "t_edit", 12, title="Edit", fields=fields())
    assert interaction.last[0] == "send_modal"


def test_form_values_are_read_from_the_payload() -> None:
    values = ui.FormValues(SUBMISSION)
    assert values.text("name") == "Tech"
    assert values.text("text") == "" and values.text("missing") == ""
    assert values.choice("interval") == "3600"
    assert values.choice("missing") is None
    assert values.ids("roles") == [50, 51]
    assert values.channel("channel") == (77, discord.ChannelType.forum, "articles")
    assert values.channel("missing") is None
    assert ui.FormValues(None).text("name") == ""


async def test_submission_is_dispatched_statelessly(db: Database) -> None:
    submitted.clear()
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    interaction = FakeInteraction(db, type=discord.InteractionType.modal_submit, data=SUBMISSION)
    await ui.handle_interaction(interaction)
    assert [ids for ids, _ in submitted] == [(12,)]
    assert [name for name, _ in interaction.calls] == ["defer", "followup"]
    assert interaction.text == "Saved."


@pytest.mark.parametrize(
    ("custom_id", "administrator", "expected"),
    [
        ("rss:m:t_edit:12", False, ui.NEED_MANAGER),
        ("rss:m:t_edit", True, ui.STALE),  # an id is missing
        ("rss:m:t_edit:12:13", True, ui.STALE),
        ("rss:m:t_edit:-12", True, ui.STALE),
        ("rss:m:t_gone:12", True, ui.STALE),
        ("rss:c:t_manager:12", True, ui.STALE),  # a button's id is not a form's
    ],
)
async def test_bad_submissions_are_refused(
    db: Database, custom_id: str, administrator: bool, expected: str
) -> None:
    submitted.clear()
    interaction = FakeInteraction(
        db,
        administrator=administrator,
        type=discord.InteractionType.modal_submit,
        data={**SUBMISSION, "custom_id": custom_id},
    )
    await ui.handle_interaction(interaction)
    assert submitted == []
    assert interaction.text == expected


async def test_someone_elses_modal_is_ignored(db: Database) -> None:
    interaction = FakeInteraction(
        db,
        type=discord.InteractionType.modal_submit,
        data={"custom_id": "abcdef", "components": []},
    )
    await ui.handle_interaction(interaction)
    assert interaction.calls == []


# -- Clicks and forms that are about to run out of time --


class _Slow(ui.ActionButton, action="test_slow", requires=None):
    async def handle(self, interaction: Any) -> None:
        await asyncio.sleep(0.05)
        await ui.edit(interaction, "done")


async def test_a_slow_click_is_deferred_and_still_answered(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ui, "ANSWER_WITHIN_S", 0.01)
    interaction = FakeInteraction(
        db, type=discord.InteractionType.component, data={"custom_id": "rss:c:test_slow"}
    )
    await asyncio.gather(
        _Slow().callback(interaction),  # type: ignore[arg-type]
        ui.handle_interaction(interaction),  # type: ignore[arg-type]
    )
    assert [name for name, _ in interaction.calls] == ["defer", "edit_original_response"]
    assert "its handler is still running" in caplog.text


async def test_a_click_nothing_handled_is_told_to_try_again(
    db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ui, "ANSWER_WITHIN_S", 0.01)
    interaction = FakeInteraction(
        db, type=discord.InteractionType.component, data={"custom_id": "rss:c:test_slow"}
    )
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert interaction.text == ui.NOT_HANDLED
    assert "its handler never ran" in caplog.text


async def test_an_answered_click_is_left_alone(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui, "ANSWER_WITHIN_S", 0.01)
    interaction = FakeInteraction(
        db, type=discord.InteractionType.component, data={"custom_id": "rss:c:test_slow"}
    )
    await ui.edit(interaction, "done")  # type: ignore[arg-type]
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert [name for name, _ in interaction.calls] == ["edit_message"]


# -- Who did it, and the container log --


def test_actor_of_is_the_member_behind_the_interaction(db: Database) -> None:
    interaction = FakeInteraction(db, user_id=77, display_name="Sam the Second")
    assert ui.actor_of(interaction) == Actor(77, "Sam the Second", MEMBER_AVATAR)
    assert ui.actor_of(FakeInteraction(db)) == MEMBER


async def test_record_saves_a_log_entry_by_the_member_and_starts_its_report(db: Database) -> None:
    interaction = FakeInteraction(db)
    entry = ui.record(
        interaction, LogKind.GRANT_GIVEN, detail="<@&5>", changes=[Change("Level", "", "Admin")]
    )
    assert entry is not None and db.list_log_entries(SERVER, limit=5) == [entry]
    assert (entry.actor_id, entry.actor_name, entry.kind) == (USER, "Alex", LogKind.GRANT_GIVEN)
    assert (entry.detail, entry.changes) == ("<@&5>", (Change("Level", "", "Admin"),))
    await interaction.journal.drain()
    assert interaction.reports.sent == [(None, [entry], MEMBER)]

    quiet = ui.record(interaction, LogKind.GRANT_TAKEN, detail="<@&5>", announce=False)
    await interaction.journal.drain()
    assert quiet is not None and len(interaction.reports.sent) == 1


async def test_record_does_not_raise_when_the_entry_cannot_be_saved(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = FakeInteraction(db)
    db._conn.execute("DROP TABLE log_entries")
    assert ui.record(interaction, LogKind.GRANT_GIVEN, detail="<@&5>") is None
    await interaction.journal.drain()
    assert interaction.reports.sent == []
    assert "its Log entry could not be saved" in caplog.text


@pytest.fixture
def uses(caplog: pytest.LogCaptureFixture) -> Any:
    """The lines written to the container log for commands, clicks and forms."""
    caplog.set_level(logging.INFO, logger="rssbot.commands")
    return lambda: [r.getMessage() for r in caplog.records if r.name == "rssbot.commands"]


def slash(db: Database, name: str, **kwargs: Any) -> FakeInteraction:
    interaction = FakeInteraction(db, **kwargs)
    interaction.command = SimpleNamespace(qualified_name=name)  # type: ignore[attr-defined]
    return interaction


async def test_a_command_that_ran_gets_one_line(db: Database, uses: Any) -> None:
    client = discord.Client(intents=discord.Intents(guilds=True))
    add_all(app_commands.CommandTree(client), client)
    interaction = slash(db, "feed pause", data={"name": "feed", "options": [{"value": "secret"}]})
    await client.on_app_command_completion(interaction, interaction.command)  # type: ignore[attr-defined]
    assert uses() == [
        'command name="/feed pause" server=100 channel=55 by="Alex" by_id=2 outcome=ok'
    ]


async def test_a_refused_command_says_why(db: Database, uses: Any) -> None:
    interaction = slash(db, "access grant")
    try:
        ui.require_admin(interaction)  # type: ignore[arg-type]
    except ui.UserError as exc:
        error = app_commands.CommandInvokeError(setup.setup_command, exc)
    await ui.on_tree_error(interaction, error)  # type: ignore[arg-type]
    assert uses() == [
        'command name="/access grant" server=100 channel=55 by="Alex" by_id=2 '
        f'outcome=refused reason="{ui.NEED_ADMIN}"'
    ]


async def test_a_command_that_broke_gets_its_line_and_one_traceback(
    db: Database, uses: Any, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = slash(db, "feed list")
    try:
        raise KeyError("secret detail")
    except KeyError as exc:
        error = app_commands.CommandInvokeError(setup.setup_command, exc)
    await ui.on_tree_error(interaction, error)  # type: ignore[arg-type]
    assert uses() == [
        'command name="/feed list" server=100 channel=55 by="Alex" by_id=2 outcome=error'
    ]
    assert "secret detail" not in uses()[0]
    assert caplog.text.count("Traceback") == 1  # logged where it always was, not twice


async def test_clicks_selects_and_forms_are_named_by_their_action(db: Database, uses: Any) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    click = {"custom_id": "rss:c:t_manager:42", "component_type": 2}
    interaction = FakeInteraction(db, type=discord.InteractionType.component, data=click)
    await ManagerButton(42).callback(interaction)  # type: ignore[arg-type]
    picked = {"custom_id": "rss:c:t_options", "component_type": 3, "values": ["secret"]}
    interaction = FakeInteraction(db, type=discord.InteractionType.component, data=picked)
    await Options().callback(interaction)  # type: ignore[arg-type]
    interaction = FakeInteraction(db, type=discord.InteractionType.modal_submit, data=SUBMISSION)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    start, end = "command name=", ' server=100 channel=55 by="Alex" by_id=2 outcome=ok'
    assert uses() == [
        f'{start}"button t_manager"{end}',
        f'{start}"select t_options"{end}',
        f'{start}"form t_edit"{end}',
    ]
    assert not any(word in line for line in uses() for word in ("secret", "Tech", "42"))


async def test_refused_and_broken_clicks_and_forms_get_their_line(db: Database, uses: Any) -> None:
    click = {"custom_id": "rss:c:t_manager:42", "component_type": 2}
    interaction = FakeInteraction(db, type=discord.InteractionType.component, data=click)
    await ManagerButton(42).callback(interaction)  # type: ignore[arg-type]
    broken = {"custom_id": "rss:c:t_fail", "component_type": 2}
    interaction = FakeInteraction(db, type=discord.InteractionType.component, data=broken)
    await FailingButton().callback(interaction)  # type: ignore[arg-type]
    gone = {"custom_id": "rss:c:long_gone:1", "component_type": 2}
    interaction = FakeInteraction(db, type=discord.InteractionType.component, data=gone)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    interaction = FakeInteraction(db, type=discord.InteractionType.modal_submit, data=SUBMISSION)
    await ui.handle_interaction(interaction)  # type: ignore[arg-type]
    assert [line.split(' server=100 channel=55 by="Alex" by_id=2 ') for line in uses()] == [
        ['command name="button t_manager"', f'outcome=refused reason="{ui.NEED_MANAGER}"'],
        ['command name="button t_fail"', "outcome=error"],
        ['command name="button long_gone"', f'outcome=refused reason="{ui.STALE}"'],
        ['command name="form t_edit"', f'outcome=refused reason="{ui.NEED_MANAGER}"'],
    ]


async def test_a_handler_that_turns_the_member_down_itself_is_refused(
    db: Database, uses: Any
) -> None:
    interaction = FakeInteraction(db, type=discord.InteractionType.component)

    async def work() -> None:
        ui.refused(interaction, "The message text is too long.")  # type: ignore[arg-type]

    await ui.guarded(interaction, work)  # type: ignore[arg-type]
    assert uses()[0].endswith('outcome=refused reason="The message text is too long."')


async def test_a_line_cannot_be_broken_by_a_name_or_a_reason(db: Database, uses: Any) -> None:
    interaction = slash(db, "feed add", display_name='x" outcome=ok\nfake line')
    await ui.report_error(interaction, ui.UserError('No.\n"quoted" outcome=ok'))  # type: ignore[arg-type]
    [line] = uses()
    assert "\n" not in line
    assert line.count("outcome=") == 3 and line.count(" outcome=refused ") == 1
    assert 'by="x\\" outcome=ok\\u000afake line"' in line


async def test_a_reason_that_repeats_what_was_typed_is_replaced(db: Database, uses: Any) -> None:
    interaction = slash(db, "feed add")
    error = ui.UserError("Not found. (`https://example.com/?key=s3cret`)", log_reason="Not found.")
    await ui.report_error(interaction, error)  # type: ignore[arg-type]
    assert interaction.text == "Not found. (`https://example.com/?key=s3cret`)"
    assert uses()[0].endswith('outcome=refused reason="Not found."')


async def test_autocomplete_and_direct_messages(db: Database, uses: Any) -> None:
    db.set_grant(SERVER, USER, TargetKind.MEMBER, Level.MANAGER)
    add_feed(db, "News")
    assert await ui.feed_autocomplete(FakeInteraction(db), "ne")  # type: ignore[arg-type]
    assert uses() == []  # typing is not logged
    await ui.report_error(slash(db, "help", guild_id=None), ui.UserError(ui.SERVER_ONLY))  # type: ignore[arg-type]
    assert uses() == [
        f'command name="/help" channel=55 by="Alex" by_id=2 outcome=refused '
        f'reason="{ui.SERVER_ONLY}"'
    ]
