"""The toolkit every command module builds on. Read PATTERNS.md next to this file first.

Nothing here keeps per-message state: buttons, selects and pop-up forms carry what they
need in their custom id, which is untrusted input and is parsed strictly on every use.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, NamedTuple, Self
from urllib.parse import urlsplit

import discord
from discord import app_commands

from ..access import can_admin, can_manage, level_of
from ..db import Database
from ..journal import quote
from ..logembed import safe
from ..models import Actor, Change, Feed, Level, LogEntry, LogKind

log = logging.getLogger(__name__)
use_log = logging.getLogger("rssbot.commands")  # one line per command, click and form

NO_MENTIONS = discord.AllowedMentions.none()

MESSAGE_LIMIT = 2000
EMBED_DESCRIPTION_LIMIT = 4096
CUSTOM_ID_LIMIT = 100
CHOICE_LIMIT = 25  # autocomplete choices, select options
CHOICE_NAME_LIMIT = 100
BUTTON_LABEL_LIMIT = 80
MODAL_TITLE_LIMIT = 45
MODAL_LABEL_LIMIT = 45
MODAL_DESCRIPTION_LIMIT = 100
MODAL_FIELD_LIMIT = 5
TEXT_INPUT_LIMIT = 4000
PAGE_SIZE = 10

MAX_ID = 2**63 - 1  # what SQLite can store; Discord ids are smaller

# Discord shows "This interaction failed" when a click or form has no answer after 3 seconds.
ANSWER_WITHIN_S = 2.0
_STARTED = "rssbot_started"
_REFUSED = "rssbot_refused"

UNEXPECTED = "Something went wrong. It has been logged."
STALE = "That control is out of date. Run the command again."
NOT_HANDLED = "That click did not go through. Try again."
SERVER_ONLY = "This only works in a Server."
NEED_ADMIN = "Only Admins of this Server can do that."
NEED_MANAGER = "Only Managers and Admins of this Server can do that."
FEED_GONE = "That Feed no longer exists."
MISSING_PERMISSION = (
    "The bot is missing a permission for that. Check what it is allowed to do in that channel."
)
TARGET_GONE = "That message or channel no longer exists."
PICK_FEED = "Choose a Feed from the list that appears as you type."


# -- Errors --


class UserError(Exception):
    """An expected failure. Its message is one plain sentence shown to the member.

    `log_reason` is what the container log says instead, for a sentence that repeats
    something the member typed, such as a Feed address.
    """

    def __init__(self, user_message: str, *, log_reason: str | None = None) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.log_reason = log_reason


_EXPECTED_NAMES = frozenset({"ServiceError", "TemplateError"})
_ALREADY_ACKNOWLEDGED = 40060  # Discord's error code for a second first response


def _already_answered(exc: BaseException) -> bool:
    """Whether a first response failed only because the interaction already has one.

    discord.py marks a response as done only once Discord has accepted it, so a response
    sent while another (the watchdog's defer) is in flight is refused by Discord instead.
    """
    return isinstance(exc, discord.InteractionResponded) or (
        isinstance(exc, discord.HTTPException) and exc.code == _ALREADY_ACKNOWLEDGED
    )


def user_message(exc: BaseException) -> str | None:
    """The sentence to show for an expected error, or None if the error is unexpected."""
    if isinstance(exc, app_commands.CommandInvokeError):
        exc = exc.original
    if isinstance(exc, UserError):
        return exc.user_message
    # Other layers' expected errors are recognised by name, so this file need not import them.
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _EXPECTED_NAMES:
        return str(getattr(exc, "user_message", "") or exc) or None
    if "FeedNotFound" in names:
        return FEED_GONE
    if isinstance(exc, discord.Forbidden):
        return MISSING_PERMISSION
    if isinstance(exc, discord.NotFound):
        return TARGET_GONE
    if _already_answered(exc):  # reply() and edit() recover from this; a form cannot open late
        return NOT_HANDLED
    if isinstance(exc, app_commands.CommandNotFound | app_commands.CommandSignatureMismatch):
        return "That command has changed. Wait a minute and try again."
    if isinstance(exc, app_commands.TransformerError):
        return "One of the options was not understood. Choose a value from the list."
    if isinstance(exc, app_commands.CheckFailure):
        return "You cannot use that command here."
    return None


async def report_error(interaction: discord.Interaction, exc: BaseException) -> None:
    """Tell the member what went wrong, logging anything unexpected. Never raises."""
    if isinstance(exc, app_commands.CommandInvokeError):
        exc = exc.original  # the error the command itself raised, with its traceback
    message = user_message(exc)
    if message is None:
        log_use(interaction, "error")
    else:
        log_use(interaction, "refused", getattr(exc, "log_reason", None) or message)
    if message is None:
        log.error(
            "Unexpected error (Server %s, member %s, interaction %s)",
            interaction.guild_id,
            getattr(interaction.user, "id", None),
            (interaction.data or {}).get("custom_id") or (interaction.data or {}).get("name"),
            exc_info=exc,
        )
        message = UNEXPECTED
    try:
        if message == FEED_GONE and getattr(interaction, "message", None) is not None:
            # The panel that was clicked belongs to a deleted Feed: take its controls away.
            try:
                await edit(interaction, message)
                return
            except Exception:
                log.debug("Could not replace the panel of a deleted Feed", exc_info=True)
        await reply(interaction, message)
    except Exception:
        log.warning("Could not tell the member about an error", exc_info=True)


async def on_tree_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """The one error handler for slash commands; add_all() installs it on the tree."""
    await report_error(interaction, error)


async def guarded(interaction: discord.Interaction, work: Callable[[], Awaitable[None]]) -> None:
    """Run a component or form callback so that a failure is reported instead of lost.

    discord.py only logs exceptions from these callbacks, which leaves the member with
    "This interaction failed".
    """
    try:
        await work()
    except Exception as exc:
        await report_error(interaction, exc)
    else:
        log_done(interaction)


# -- The container log: one line for every command, click and form --


def _use_name(interaction: discord.Interaction) -> str:
    """ "/feed pause", "button feed_pause", "select feed_post_as_set" or "form feed_edit"."""
    data: Mapping[str, Any] = interaction.data or {}
    if interaction.type in (
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    ):
        parsed = parse_custom_id(data.get("custom_id"))
        if interaction.type is discord.InteractionType.modal_submit:
            kind = "form"
        elif data.get("component_type") == discord.ComponentType.button.value:
            kind = "button"
        else:
            kind = "select"
        return f"{kind} {parsed.action if parsed else 'unknown'}"
    command = getattr(interaction, "command", None)
    return "/" + str(getattr(command, "qualified_name", None) or data.get("name") or "unknown")


def use_line(interaction: discord.Interaction, outcome: str, reason: str = "") -> str:
    """The container log line of one command, click or form.

    For example: `command name="/feed pause" server=123 channel=456 by="alex" by_id=987
    outcome=ok`, in the style of a Log entry's line. It names what was used and never what
    was typed into it: a Feed address can carry a private key.
    """
    parts = ["command", f"name={quote(_use_name(interaction))}"]
    if interaction.guild_id is not None:
        parts.append(f"server={interaction.guild_id}")
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id is not None:
        parts.append(f"channel={channel_id}")
    member = interaction.user
    parts.append(f"by={quote(str(getattr(member, 'display_name', '')))}")
    parts.append(f"by_id={member.id}")
    parts.append(f"outcome={outcome}")
    if reason:
        parts.append(f"reason={quote(reason)}")
    return " ".join(parts)


def log_use(interaction: discord.Interaction, outcome: str, reason: str = "") -> None:
    """Write the line. `outcome` is ok, refused (with the reason) or error. Never raises."""
    try:
        use_log.info("%s", use_line(interaction, outcome, reason))
    except Exception:
        log.debug("Could not write a command's line to the log", exc_info=True)


def refused(interaction: discord.Interaction, reason: str) -> None:
    """Note that a handler turned the member down itself, without raising: its line then
    says refused instead of ok."""
    interaction.extras[_REFUSED] = reason


def log_done(interaction: discord.Interaction) -> None:
    """Write the line of a command, click or form whose handler ran to its end."""
    reason = interaction.extras.get(_REFUSED)
    if reason is None:
        log_use(interaction, "ok")
    else:
        log_use(interaction, "refused", str(reason))


# -- Shared objects --


@dataclass(slots=True)
class Deps:
    """What command modules share. The start-up file sets `client.deps` to one of these."""

    db: Database
    service: Any = None
    deliverer: Any = None
    scheduler: Any = None
    journal: Any = None


def deps(interaction: discord.Interaction) -> Deps:
    return interaction.client.deps  # type: ignore[attr-defined]


# -- Who did it --


def actor_of(interaction: discord.Interaction) -> Actor:
    """The member who ran the command or pressed the button, for the Log entry of a change."""
    member = interaction.user
    return Actor(id=member.id, name=member.display_name, avatar_url=member.display_avatar.url)


def record(
    interaction: discord.Interaction,
    kind: LogKind,
    *,
    changes: Iterable[Change] = (),
    detail: str = "",
    announce: bool = True,
) -> LogEntry | None:
    """Save the Log entry of a change a command made straight through `db`, by the member.

    Call it once the change is made. Its report in the Logs channel is started unless
    `announce` is False. If the entry cannot be saved that is logged and None is returned:
    the member is not shown an error for something that was done.
    """
    journal = deps(interaction).journal
    actor = actor_of(interaction)
    try:
        entry: LogEntry = journal.record(
            server_id_of(interaction), actor, kind, changes=changes, detail=detail
        )
    except sqlite3.Error:
        log.exception("A change was made, but its Log entry could not be saved")
        return None
    if announce:
        journal.announce_soon(entry, actor)
    return entry


# -- Access --


def _role_ids(member: Any) -> set[int]:
    # Member.roles drops roles missing from the cache; _roles is the list Discord sent.
    ids = {role.id for role in getattr(member, "roles", ())}
    ids.update(getattr(member, "_roles", ()))
    return ids


def access_level(interaction: discord.Interaction) -> Level | None:
    """The invoking member's Level in the interaction's Server, from the payload and Grants."""
    server_id = interaction.guild_id
    if server_id is None:
        return None
    member = interaction.user
    guild = interaction.guild
    return level_of(
        user_id=member.id,
        role_ids=_role_ids(member),
        is_owner=guild is not None and guild.owner_id == member.id,
        is_administrator=interaction.permissions.administrator,
        grants=deps(interaction).db.list_grants(server_id),
        server_id=server_id,
    )


def require(interaction: discord.Interaction, needed: Level | None) -> None:
    """Raise UserError unless the member is in a Server and holds `needed` (None: anyone)."""
    if interaction.guild_id is None:
        raise UserError(SERVER_ONLY)
    if needed is None:
        return
    level = access_level(interaction)
    if needed is Level.ADMIN:
        if not can_admin(level):
            raise UserError(NEED_ADMIN)
    elif not can_manage(level):
        raise UserError(NEED_MANAGER)


def require_admin(interaction: discord.Interaction) -> None:
    require(interaction, Level.ADMIN)


def require_manager(interaction: discord.Interaction) -> None:
    require(interaction, Level.MANAGER)


def server_id_of(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise UserError(SERVER_ONLY)
    return interaction.guild_id


def feed_of(interaction: discord.Interaction, feed_id: int) -> Feed:
    """The Feed, only if it belongs to the interaction's Server. Use for every id received."""
    server_id = server_id_of(interaction)
    feed = deps(interaction).db.get_feed(feed_id) if 0 <= feed_id <= MAX_ID else None
    if feed is None or feed.server_id != server_id:
        raise UserError(FEED_GONE)
    return feed


# -- Text --


def cut(text: str, limit: int, ellipsis: str = "…") -> str:
    """Shorten text to at most `limit` characters."""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    return text[: limit - len(ellipsis)].rstrip() + ellipsis


def channel_mention(channel_id: int) -> str:
    return f"<#{channel_id}>"


def role_mention(role_id: int) -> str:
    return f"<@&{role_id}>"


def member_mention(user_id: int) -> str:
    return f"<@{user_id}>"


def cached_channel(interaction: discord.Interaction, channel_id: int) -> Any | None:
    """The channel or thread from the Server's cache, or None."""
    guild = interaction.guild
    return guild.get_channel_or_thread(channel_id) if guild is not None else None


THREAD_TYPES = (
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
    discord.ChannelType.news_thread,
)


def missing_post_permissions(
    interaction: discord.Interaction, channel_id: int, *, embed: bool = False
) -> list[str] | None:
    """The permissions the bot lacks to post in a channel, by name; None when the cache cannot tell.

    A thread also needs Send Messages in Threads, and an Embed needs Embed Links.
    """
    guild = interaction.guild
    channel = cached_channel(interaction, channel_id)
    if guild is None or channel is None or guild.me is None:
        return None
    permissions = channel.permissions_for(guild.me)
    needed = [
        ("View Channel", permissions.view_channel),
        ("Send Messages", permissions.send_messages),
    ]
    if getattr(channel, "type", None) in THREAD_TYPES:
        needed.append(("Send Messages in Threads", permissions.send_messages_in_threads))
    if embed:
        needed.append(("Embed Links", permissions.embed_links))
    return [name for name, granted in needed if not granted]


def bot_can_post(
    interaction: discord.Interaction, channel_id: int, *, embed: bool = False
) -> bool | None:
    """Whether the bot can see and post in a channel; None when the cache cannot tell."""
    missing = missing_post_permissions(interaction, channel_id, embed=embed)
    return None if missing is None else not missing


# -- Members named in Log entries --

LEFT_THE_SERVER = " (left the Server)"
ACTOR_NAME_CHARS = 20  # of a saved name, before it is escaped
MAX_LOOKUPS = 10  # distinct members looked up for one message; more are shown as mentions
# The longest way actors_of shows a member: every character of a name escaped.
LONGEST_ACTOR = "\\_" * ACTOR_NAME_CHARS + LEFT_THE_SERVER


async def _has_left(guild: Any, user_id: int) -> bool:
    try:
        await guild.fetch_member(user_id)
    except discord.NotFound:
        return True
    except Exception:
        # Not knowing is not a reason to fail the command: the mention is shown.
        log.warning("Could not look up member %s", user_id, exc_info=True)
    return False


async def actors_of(
    interaction: discord.Interaction, entries: Iterable[LogEntry]
) -> dict[int, str]:
    """How to show each member behind these Log entries, by member id.

    A member of the Server is a mention. One who left is "Name (left the Server)", with
    the name saved in the Log entry, because a mention of them shows as a bare number.
    The bot has no member cache, so each member not in the cache is fetched, once, and
    all together; that defers the interaction first. With more than MAX_LOOKUPS distinct
    members, or a lookup that fails for any reason but "no such member", the mention is shown.
    Pass only the entries being shown. The bot's own entries have no member: see actor_words.
    """
    names: dict[int, str] = {}
    for entry in entries:
        if entry.actor_id is not None:
            names.setdefault(entry.actor_id, entry.actor_name)
    shown = {actor_id: member_mention(actor_id) for actor_id in names}
    guild = interaction.guild
    if guild is None or len(names) > MAX_LOOKUPS:
        return shown
    unknown = [
        actor_id
        for actor_id in names
        if actor_id != interaction.user.id and guild.get_member(actor_id) is None
    ]
    if not unknown:
        return shown
    await defer(interaction, update=True)
    gone = await asyncio.gather(*(_has_left(guild, actor_id) for actor_id in unknown))
    for actor_id, has_left in zip(unknown, gone, strict=True):
        if has_left:
            name = safe(cut(names[actor_id], ACTOR_NAME_CHARS)) or "A member"
            shown[actor_id] = name + LEFT_THE_SERVER
    return shown


def actor_words(entry: LogEntry, shown: Mapping[int, str]) -> str:
    """Who made a Log entry: "the bot", or the member as actors_of shows them."""
    if entry.actor_id is None:
        return "the bot"
    return shown.get(entry.actor_id) or member_mention(entry.actor_id)


# -- Replies: always private, never pinging --


def _message_kwargs(
    content: str | None, embed: discord.Embed | None, view: discord.ui.View | None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"allowed_mentions": NO_MENTIONS}
    if content is not None:
        kwargs["content"] = cut(content, MESSAGE_LIMIT)
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    return kwargs


async def reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Send a private message: the first response, or a follow-up if one was already sent."""
    kwargs = _message_kwargs(content, embed, view)
    if not interaction.response.is_done():
        try:
            await interaction.response.send_message(ephemeral=True, **kwargs)
            return
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            if not _already_answered(exc):
                raise
    await interaction.followup.send(ephemeral=True, **kwargs)


async def edit(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Replace the message a button or select sits on. What is left out is removed.

    For interactions that come from a message: a component, or a form opened by one. A form
    opened by a slash command has no message to replace, so it gets a reply instead.
    """
    if (
        interaction.type is discord.InteractionType.modal_submit
        and getattr(interaction, "message", None) is None
    ):
        await reply(interaction, content, embed=embed, view=view)
        return
    kwargs: dict[str, Any] = {
        "content": None if content is None else cut(content, MESSAGE_LIMIT),
        "embed": embed,
        "view": view,
        "allowed_mentions": NO_MENTIONS,
    }
    if not interaction.response.is_done():
        try:
            await interaction.response.edit_message(**kwargs)
            return
        except (discord.InteractionResponded, discord.HTTPException) as exc:
            if not _already_answered(exc):
                raise
    await interaction.edit_original_response(**kwargs)


async def defer(interaction: discord.Interaction, *, update: bool = False) -> None:
    """Acknowledge within 3 seconds before slow work.

    By default shows a private "thinking" message that the next reply() fills in. With
    update=True (components and forms opened by them) nothing is shown and the next edit()
    replaces the message the component sits on.
    """
    if interaction.response.is_done():
        return
    # A form opened by a slash command has no message to update, so it gets a reply.
    from_message = interaction.type is discord.InteractionType.component or (
        interaction.type is discord.InteractionType.modal_submit
        and getattr(interaction, "message", None) is not None
    )
    try:
        if update and from_message:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
    except (discord.InteractionResponded, discord.HTTPException) as exc:
        if not _already_answered(exc):
            raise


# -- Custom ids: rss:<kind>:<action>[:<id>...] --

PREFIX = "rss"
_COMPONENT = "c"
_MODAL = "m"
_ACTION = r"[a-z][a-z0-9_]{0,39}"
_ID = r"(?:0|[1-9][0-9]{0,18})"  # ASCII digits only, no sign, no leading zeros
_CUSTOM_ID = re.compile(rf"{PREFIX}:([{_COMPONENT}{_MODAL}]):({_ACTION})((?::{_ID})*)")
_NEVER = re.compile(r"(?!)")


class ParsedId(NamedTuple):
    kind: str
    action: str
    ids: tuple[int, ...]


def _encode(kind: str, action: str, ids: Sequence[int]) -> str:
    if not re.fullmatch(_ACTION, action):
        raise ValueError(f"Bad action name: {action!r}")
    for value in ids:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ID:
            raise ValueError(f"Bad id for {action!r}: {value!r}")
    custom_id = ":".join((PREFIX, kind, action, *map(str, ids)))
    if len(custom_id) > CUSTOM_ID_LIMIT:
        raise ValueError(f"Custom id is over {CUSTOM_ID_LIMIT} characters: {custom_id!r}")
    return custom_id


def component_id(action: str, *ids: int) -> str:
    return _encode(_COMPONENT, action, ids)


def modal_id(action: str, *ids: int) -> str:
    return _encode(_MODAL, action, ids)


def parse_custom_id(custom_id: object) -> ParsedId | None:
    """Strictly parse one of our custom ids; None for anything else."""
    if not isinstance(custom_id, str) or len(custom_id) > CUSTOM_ID_LIMIT:
        return None
    match = _CUSTOM_ID.fullmatch(custom_id)
    if match is None:
        return None
    ids = tuple(int(part) for part in match[3].split(":")[1:])
    if any(value > MAX_ID for value in ids):
        return None
    return ParsedId(match[1], match[2], ids)


# -- Stateless buttons and selects --

_UNSET: Any = object()
_ACTIONS: dict[str, type[Action]] = {}


class Action(discord.ui.DynamicItem[discord.ui.Item[Any]], template=_NEVER):
    """Base for a stateless button or select. Subclass ActionButton or ActionSelect.

    A concrete class names its action, how many ids its custom id carries, and the Level
    it requires (None: anyone in the Server). Access is re-checked on every use, then
    handle() runs with self.ids holding the parsed ids.
    """

    action: ClassVar[str] = ""
    id_count: ClassVar[int] = 0
    requires: ClassVar[Level | None] = Level.ADMIN
    _stale: bool = False

    def __init_subclass__(
        cls, *, action: str | None = None, ids: int = 0, requires: Level | None = _UNSET
    ) -> None:
        if action is None:  # an abstract base
            super().__init_subclass__(template=_NEVER)
            return
        if requires is _UNSET:
            raise TypeError(f"{cls.__name__} must say which Level it requires")
        if action in _ACTIONS:
            raise ValueError(f"Action {action!r} is already used by {_ACTIONS[action].__name__}")
        component_id(action, *([MAX_ID] * ids))  # the longest id it can produce must fit
        cls.action, cls.id_count, cls.requires = action, ids, requires
        super().__init_subclass__(template=component_id(action) + rf":({_ID})" * ids)
        _ACTIONS[action] = cls

    def __init__(self, *ids: int, **look: Any) -> None:
        if len(ids) != self.id_count:
            raise ValueError(f"{self.action!r} takes {self.id_count} id(s), got {len(ids)}")
        self.ids: tuple[int, ...] = ids
        super().__init__(self.build(component_id(self.action, *ids), **look))

    def build(self, custom_id: str, **look: Any) -> discord.ui.Item[Any]:
        """Make the underlying component. Must also work with no `look` arguments."""
        raise NotImplementedError

    async def handle(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        # discord.py drops the click if this raises, so a bad id is only noted here and
        # refused in callback(), where the member can be told.
        ids = tuple(int(group) for group in match.groups())
        valid = all(value <= MAX_ID for value in ids)
        self = cls(*(ids if valid else (0,) * len(ids)))
        self._stale = not valid
        return self

    async def callback(self, interaction: discord.Interaction) -> None:
        await guarded(interaction, lambda: self._run(interaction))

    async def _run(self, interaction: discord.Interaction) -> None:
        interaction.extras[_STARTED] = True
        if self._stale:
            raise UserError(STALE)
        require(interaction, self.requires)
        await self.handle(interaction)


class ActionButton(Action):
    """A stateless button. Set `label` and `style`, or pass them per instance."""

    label: ClassVar[str] = ""
    style: ClassVar[discord.ButtonStyle] = discord.ButtonStyle.secondary

    def build(
        self,
        custom_id: str,
        *,
        label: str | None = None,
        style: discord.ButtonStyle | None = None,
        disabled: bool = False,
        emoji: str | None = None,
        row: int | None = None,
    ) -> discord.ui.Button[Any]:
        return discord.ui.Button(
            custom_id=custom_id,
            label=cut(label or self.label, BUTTON_LABEL_LIMIT),
            style=style or self.style,
            disabled=disabled,
            emoji=emoji,
            row=row,
        )


class ActionSelect(Action):
    """A stateless select. Override build() to return any discord.ui select."""

    @property
    def picked(self) -> list[str]:
        """What was chosen, as strings: option values, or ids for channel and role selects."""
        return [str(getattr(value, "id", value)) for value in self.item.values]  # type: ignore[attr-defined]

    @property
    def picked_ids(self) -> list[int]:
        return _to_ids(self.picked)


def _to_ids(values: Sequence[Any]) -> list[int]:
    ids = []
    for value in values:
        text = str(value)
        if re.fullmatch(_ID, text) and int(text) <= MAX_ID:
            ids.append(int(text))
    return ids


def view_of(*items: discord.ui.Item[Any] | None) -> discord.ui.View:
    """A view of stateless items. It has no timeout and the bot remembers nothing about it."""
    view = discord.ui.View(timeout=None)
    for item in items:
        if item is not None:
            view.add_item(item)
    return view


def register_dynamic_items(client: discord.Client) -> None:
    """Call once at start-up, after every command module has been imported.

    Registers every Action subclass and hooks on_interaction, which is where pop-up form
    submissions are handled. An on_interaction the client already has keeps running; one
    assigned later would replace ours and must call handle_interaction() itself. Also
    hooks on_app_command_completion, which writes the log line of a slash command that
    ran to its end; one that failed gets its line from on_tree_error.
    """
    client.add_dynamic_items(*_ACTIONS.values())
    if getattr(client, "_rssbot_hooked", False):
        return
    previous = getattr(client, "on_interaction", None)
    previous_completion = getattr(client, "on_app_command_completion", None)

    async def on_interaction(interaction: discord.Interaction) -> None:
        await handle_interaction(interaction)
        if previous is not None:
            await previous(interaction)

    async def on_app_command_completion(interaction: discord.Interaction, command: Any) -> None:
        log_done(interaction)
        if previous_completion is not None:
            await previous_completion(interaction, command)

    client.on_interaction = on_interaction  # type: ignore[attr-defined]
    client.on_app_command_completion = on_app_command_completion  # type: ignore[attr-defined]
    client._rssbot_hooked = True  # type: ignore[attr-defined]


async def handle_interaction(interaction: discord.Interaction) -> None:
    """Dispatch form submissions, and answer clicks on controls that no longer exist."""
    custom_id = (interaction.data or {}).get("custom_id")
    if not isinstance(custom_id, str) or not custom_id.startswith(f"{PREFIX}:"):
        return
    if interaction.type is discord.InteractionType.modal_submit:
        interaction.extras[_STARTED] = True
        watchdog = asyncio.create_task(_answer_in_time(interaction, custom_id))
        try:
            await guarded(interaction, lambda: _submit_form(interaction, custom_id))
        finally:
            watchdog.cancel()
    elif interaction.type is discord.InteractionType.component:
        if any(
            cls.__discord_ui_compiled_template__.fullmatch(custom_id) for cls in _ACTIONS.values()
        ):
            await _answer_in_time(interaction, custom_id)  # the library runs the Action itself
        else:
            await report_error(interaction, UserError(STALE))


async def _answer_in_time(interaction: discord.Interaction, custom_id: str) -> None:
    """Answer a click or form that is about to run out of time, and log that it happened.

    A handler that is still working gets a defer, after which its own edit() or reply()
    still works. A click whose handler never ran gets a message instead of silence.
    """
    await asyncio.sleep(ANSWER_WITHIN_S)
    if interaction.response.is_done():
        return
    started = bool(interaction.extras.get(_STARTED))
    log.warning(
        "No answer to %s after %.1f s (Server %s): %s",
        custom_id,
        ANSWER_WITHIN_S,
        interaction.guild_id,
        "its handler is still running"
        if started
        else f"its handler never ran (on a message: {interaction.message is not None})",
    )
    try:
        if started:
            await defer(interaction, update=True)
        else:
            await reply(interaction, NOT_HANDLED)
    except Exception:
        log.warning("Could not answer %s in time", custom_id, exc_info=True)


# -- Confirm --


class CancelButton(ActionButton, action="cancel", requires=None):
    label = "Cancel"

    async def handle(self, interaction: discord.Interaction) -> None:
        await edit(interaction, "Cancelled.")


def confirm_view(confirm: Action, *, cancel_label: str = "Cancel") -> discord.ui.View:
    """Confirm and Cancel. `confirm` is the stateless button that does the destructive thing."""
    return view_of(confirm, CancelButton(label=cancel_label))


# -- Pagination --


class Page[T](NamedTuple):
    items: Sequence[T]
    page: int  # zero-based, clamped into range
    pages: int  # at least 1

    @property
    def footer(self) -> str:
        return f"Page {self.page + 1} of {self.pages}"


def paginate[T](items: Sequence[T], page: int, per_page: int = PAGE_SIZE) -> Page[T]:
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(max(page, 0), pages - 1)
    return Page(items[page * per_page : (page + 1) * per_page], page, pages)


class PageButton(ActionButton):
    """A stateless Previous/Next button. Its last id is the zero-based page to show."""

    @property
    def page(self) -> int:
        return self.ids[-1]


def page_buttons(
    button: type[PageButton],
    *ids: int,
    page: int,
    pages: int,
    labels: tuple[str, str] = ("Previous", "Next"),
    row: int | None = None,
) -> list[PageButton]:
    """Previous and Next for `page` of `pages`; none when there is only one page."""
    if pages <= 1:
        return []
    last = pages - 1
    page = min(max(page, 0), last)
    # A disabled button points at the current page so the two custom ids never collide.
    return [
        button(*ids, max(page - 1, 0), label=labels[0], disabled=page == 0, row=row),
        button(*ids, min(page + 1, last), label=labels[1], disabled=page == last, row=row),
    ]


# -- Feed option for slash commands --


def _feed_label(interaction: discord.Interaction, feed: Feed, detail: str = "") -> str:
    tail = f" · {cut(detail, 30)}" if detail else ""
    channel = cached_channel(interaction, feed.channel_id)
    if channel is not None:
        tail += f" (#{cut(channel.name, 30)})"
    # The name gives way to the tail, which is what tells two Feeds apart.
    name = cut(feed.name or feed.url or f"Feed {feed.id}", min(60, CHOICE_NAME_LIMIT - len(tail)))
    return name + tail


def _feed_details(feed: Feed) -> tuple[str, str, str]:
    """What to add to a Feed's label when another Feed's reads the same, mildest first."""
    url = urlsplit(feed.url)
    host = url.hostname or ""
    return host, host + url.path + (f"?{url.query}" if url.query else ""), f"Feed {feed.id}"


def feed_details(interaction: discord.Interaction, feeds: Sequence[Feed]) -> list[str]:
    """For each Feed what tells it apart from the others listed, "" where its name and channel do.

    The site's host, else host and path, else the Feed's id; cut to 30 characters it still differs.
    """
    details = ["" for _ in feeds]
    labels = [_feed_label(interaction, feed) for feed in feeds]
    for step in range(3):
        shared = {label for label in labels if labels.count(label) > 1}
        if not shared:
            break
        details = [
            _feed_details(feed)[step] if label in shared else detail
            for feed, label, detail in zip(feeds, labels, details, strict=True)
        ]
        labels = [
            _feed_label(interaction, feed, detail)
            for feed, detail in zip(feeds, details, strict=True)
        ]
    return details


def _feed_choices(
    interaction: discord.Interaction, feeds: Sequence[Feed]
) -> list[app_commands.Choice[str]]:
    details = feed_details(interaction, feeds)
    return [
        app_commands.Choice(name=_feed_label(interaction, feed, detail), value=feed_value(feed.id))
        for feed, detail in zip(feeds, details, strict=True)
    ]


async def feed_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """The Server's Feeds whose name or URL contains what was typed. Nothing for non-Managers."""
    try:
        if interaction.guild_id is None or not can_manage(access_level(interaction)):
            return []
        needle = current.strip().casefold()
        feeds = deps(interaction).db.list_feeds(interaction.guild_id)
        matches = [
            feed
            for feed in feeds
            if needle in feed.name.casefold() or needle in feed.url.casefold()
        ]
        return _feed_choices(interaction, matches[:CHOICE_LIMIT])
    except Exception:
        log.exception("Feed autocomplete failed")
        return []


FEED_VALUE_PREFIX = "id:"


def feed_value(feed_id: int) -> str:
    """The value of a Feed option as autocomplete offers it. A typed number is not one."""
    return f"{FEED_VALUE_PREFIX}{feed_id}"


def parse_feed_option(value: str) -> int:
    """Turn the value of a Feed option back into a Feed id.

    Only a value picked from the list counts: a number typed by hand would act on whichever
    Feed has that id, which is not what a member typing a name or a number means.
    """
    text = value.strip()
    ids = _to_ids([text[len(FEED_VALUE_PREFIX) :]]) if text.startswith(FEED_VALUE_PREFIX) else []
    if not ids:
        raise UserError(PICK_FEED)
    return ids[0]


def feed_from_option(interaction: discord.Interaction, value: str) -> Feed:
    return feed_of(interaction, parse_feed_option(value))


# -- Pop-up forms (modals), handled statelessly from handle_interaction() --


class PickedChannel(NamedTuple):
    id: int
    type: discord.ChannelType | None  # None if Discord did not describe the channel
    name: str


class FormValues:
    """What a member submitted, read from the raw payload by each field's key."""

    def __init__(self, data: Mapping[str, Any] | None) -> None:
        data = data or {}
        self._values: dict[str, Any] = {}
        self._collect(data.get("components") or ())
        self._resolved: Mapping[str, Any] = data.get("resolved") or {}

    def _collect(self, components: Sequence[Mapping[str, Any]]) -> None:
        for component in components:
            if component.get("type") == discord.ComponentType.action_row.value:
                self._collect(component.get("components") or ())
            elif component.get("type") == discord.ComponentType.label.value:
                self._collect([component.get("component") or {}])
            elif isinstance(component.get("custom_id"), str):
                value = component["values"] if "values" in component else component.get("value")
                self._values[component["custom_id"]] = value

    def text(self, key: str) -> str:
        """A text field's value with outer whitespace removed; "" if empty or missing."""
        value = self._values.get(key)
        return value.strip() if isinstance(value, str) else ""

    def choices(self, key: str) -> list[str]:
        value = self._values.get(key)
        return [str(v) for v in value] if isinstance(value, list) else []

    def choice(self, key: str) -> str | None:
        return next(iter(self.choices(key)), None)

    def ids(self, key: str) -> list[int]:
        """The ids picked in a channel or role field."""
        return _to_ids(self.choices(key))

    def channel(self, key: str) -> PickedChannel | None:
        """The first channel picked in a channel field, with its type as Discord sent it."""
        ids = self.ids(key)
        if not ids:
            return None
        raw = (self._resolved.get("channels") or {}).get(str(ids[0])) or {}
        kind = raw.get("type")
        return PickedChannel(
            ids[0],
            discord.enums.try_enum(discord.ChannelType, kind) if isinstance(kind, int) else None,
            str(raw.get("name") or ""),
        )


FormHandler = Callable[[discord.Interaction, tuple[int, ...], FormValues], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Form:
    id_count: int
    requires: Level | None
    handler: FormHandler


_FORMS: dict[str, _Form] = {}


def form_handler(
    action: str, *, ids: int = 0, requires: Level | None
) -> Callable[[FormHandler], FormHandler]:
    """Register the function that handles submissions of the form named `action`."""

    def decorator(handler: FormHandler) -> FormHandler:
        if action in _FORMS:
            raise ValueError(f"Form {action!r} already has a handler")
        modal_id(action, *([MAX_ID] * ids))
        _FORMS[action] = _Form(ids, requires, handler)
        return handler

    return decorator


async def _submit_form(interaction: discord.Interaction, custom_id: str) -> None:
    parsed = parse_custom_id(custom_id)
    form = _FORMS.get(parsed.action) if parsed and parsed.kind == _MODAL else None
    if parsed is None or form is None or len(parsed.ids) != form.id_count:
        raise UserError(STALE)
    require(interaction, form.requires)
    await form.handler(interaction, parsed.ids, FormValues(interaction.data))


def _label(text: str, description: str | None, component: discord.ui.Item[Any]) -> Any:
    return discord.ui.Label(
        text=cut(text, MODAL_LABEL_LIMIT),
        description=cut(description, MODAL_DESCRIPTION_LIMIT) if description else None,
        component=component,
    )


def text_field(
    key: str,
    label: str,
    *,
    default: str = "",
    required: bool = True,
    long: bool = False,
    max_length: int = TEXT_INPUT_LIMIT,
    placeholder: str | None = None,
    description: str | None = None,
) -> Any:
    """A text box, pre-filled with `default`."""
    text_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
        custom_id=key,
        style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
        default=default[:max_length] or None,
        required=required,
        max_length=max_length,
        placeholder=cut(placeholder, 100) if placeholder else None,
    )
    return _label(label, description, text_input)


def choice_field(
    key: str,
    label: str,
    options: Sequence[tuple[str, str]],
    *,
    default: str | None = None,
    required: bool = True,
    description: str | None = None,
) -> Any:
    """A short list to pick one of; `options` are (value, label) pairs, at most 25."""
    select: discord.ui.Select[Any] = discord.ui.Select(
        custom_id=key,
        options=[
            discord.SelectOption(label=cut(text, 100), value=value, default=value == default)
            for value, text in options
        ],
        required=required,
        min_values=1 if required else 0,
    )
    return _label(label, description, select)


def channel_field(
    key: str,
    label: str,
    *,
    channel_types: Sequence[discord.ChannelType],
    default_id: int | None = None,
    required: bool = True,
    description: str | None = None,
) -> Any:
    """A channel picker limited to `channel_types`, pre-set to `default_id`."""
    select: discord.ui.ChannelSelect[Any] = discord.ui.ChannelSelect(
        custom_id=key,
        channel_types=list(channel_types),
        default_values=[] if default_id is None else [discord.Object(default_id)],
        required=required,
        min_values=1 if required else 0,
    )
    return _label(label, description, select)


def role_field(
    key: str,
    label: str,
    *,
    default_ids: Sequence[int] = (),
    max_values: int = 1,
    required: bool = False,
    description: str | None = None,
) -> Any:
    """A role picker, pre-set to `default_ids`."""
    select: discord.ui.RoleSelect[Any] = discord.ui.RoleSelect(
        custom_id=key,
        default_values=[discord.Object(role_id) for role_id in default_ids],
        max_values=max_values,
        required=required,
        min_values=1 if required else 0,
    )
    return _label(label, description, select)


def build_form(action: str, *ids: int, title: str, fields: Sequence[Any]) -> discord.ui.Modal:
    form = _FORMS.get(action)
    if form is None or form.id_count != len(ids):
        raise ValueError(f"No form handler takes {action!r} with {len(ids)} id(s)")
    if not 1 <= len(fields) <= MODAL_FIELD_LIMIT:
        raise ValueError(f"A form takes 1 to {MODAL_FIELD_LIMIT} fields")
    modal = discord.ui.Modal(
        title=cut(title, MODAL_TITLE_LIMIT), custom_id=modal_id(action, *ids), timeout=None
    )
    for field in fields:
        modal.add_item(field)
    # send_modal() does not remember a finished modal, so the bot holds nothing between
    # showing the form and its submission; handle_interaction() takes it from there.
    modal.stop()
    return modal


async def show_form(
    interaction: discord.Interaction, action: str, *ids: int, title: str, fields: Sequence[Any]
) -> None:
    """Open a pop-up form. Must be the first response: read what pre-fills it beforehand."""
    await interaction.response.send_modal(build_form(action, *ids, title=title, fields=fields))
