"""Small stand-ins for discord.py objects, so command code can be tested without Discord.

Run a command with `await some_command.callback(FakeInteraction(db, administrator=True))`,
then look at `interaction.calls`, `interaction.last` and `interaction.text`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord

from rssbot.commands._ui import Deps
from rssbot.db import Database
from rssbot.journal import Journal
from rssbot.models import Actor, LogEntry

SERVER = 100
OWNER = 1
USER = 2
BOT = 9
NOW = 1_700_000_000
MEMBER_NAME = "Alex"
MEMBER_AVATAR = "https://cdn.example/avatars/2.png"
# Who FakeInteraction's member is, as ui.actor_of() gives it.
MEMBER = Actor(id=USER, name=MEMBER_NAME, avatar_url=MEMBER_AVATAR)


class FixedClock:
    def now(self) -> int:
        return NOW

    async def sleep(self, seconds: float) -> None:
        pass


class Reports:
    """The fake notifier: what was reported, and the Logs channel the Server had right then."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.sent: list[tuple[int | None, list[LogEntry], Actor]] = []

    async def notify(self, server_id: int, text: str) -> None:
        pass

    async def announce(self, server_id: int, entries: Any, actor: Actor) -> None:
        server = self._db.get_server(server_id)
        self.sent.append((server.logs_channel_id if server else None, list(entries), actor))


class FakeChannel:
    def __init__(
        self,
        id: int,
        name: str = "general",
        type: discord.ChannelType = discord.ChannelType.text,
        *,
        bot_can_post: bool = True,
        lacking: tuple[str, ...] = (),
    ) -> None:
        self.id, self.name, self.type = id, name, type
        self._bot_can_post = bot_can_post
        self._lacking = lacking  # permissions the bot lacks although it can see and post

    def permissions_for(self, member: Any) -> discord.Permissions:
        allowed = self._bot_can_post
        permissions = discord.Permissions(
            view_channel=allowed,
            send_messages=allowed,
            send_messages_in_threads=allowed,
            embed_links=allowed,
        )
        for name in self._lacking:
            setattr(permissions, name, False)
        return permissions


class FakeGuild:
    """`members` are the ids of the members the Server has; None means everyone is one.

    Like the real bot, which has no member cache, get_member finds only `cached` ids.
    fetch_member finds the members and raises NotFound for anyone else, or `fetch_error`
    for the ids in `fetch_fails`; every id it was asked for is in `fetched`.
    """

    def __init__(
        self,
        id: int,
        owner_id: int,
        channels: tuple[FakeChannel, ...],
        members: tuple[int, ...] | None = None,
        cached: tuple[int, ...] = (),
        fetch_fails: tuple[int, ...] = (),
    ) -> None:
        self.id, self.owner_id = id, owner_id
        self.me = SimpleNamespace(id=BOT)
        self._channels = {channel.id: channel for channel in channels}
        self._members, self._cached, self._fetch_fails = members, cached, fetch_fails
        self.fetched: list[int] = []

    def get_member(self, user_id: int) -> Any:
        return SimpleNamespace(id=user_id) if user_id in self._cached else None

    async def fetch_member(self, user_id: int) -> Any:
        self.fetched.append(user_id)
        response = SimpleNamespace(status=404, reason="Not Found")
        if user_id in self._fetch_fails:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="Oops"), "Oops")
        if self._members is not None and user_id not in self._members:
            raise discord.NotFound(response, "Unknown Member")  # type: ignore[arg-type]
        return SimpleNamespace(id=user_id)

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self._channels.get(channel_id)

    get_channel_or_thread = get_channel


class _Response:
    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        if self._done:
            raise AssertionError(f"{name} after the interaction was already responded to")
        self._done = True
        self._interaction.calls.append((name, kwargs))

    async def send_message(self, **kwargs: Any) -> None:
        self._record("send_message", kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        self._record("edit_message", kwargs)

    async def defer(self, **kwargs: Any) -> None:
        self._record("defer", kwargs)

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        self._record("send_modal", {"modal": modal})


class FakeInteraction:
    """Records every response in `calls` as (method name, keyword arguments)."""

    def __init__(
        self,
        db: Database,
        *,
        guild_id: int | None = SERVER,
        user_id: int = USER,
        role_ids: tuple[int, ...] = (),
        owner_id: int = OWNER,
        display_name: str = MEMBER_NAME,
        administrator: bool = False,
        type: discord.InteractionType = discord.InteractionType.application_command,
        data: dict[str, Any] | None = None,
        channels: tuple[FakeChannel, ...] = (),
        members: tuple[int, ...] | None = None,
        cached: tuple[int, ...] = (),
        fetch_fails: tuple[int, ...] = (),
    ) -> None:
        self.reports = Reports(db)
        self.journal = Journal(db, FixedClock(), self.reports)
        self.client = SimpleNamespace(deps=Deps(db=db, journal=self.journal))
        self.guild_id = guild_id
        self.channel_id = 55
        self.guild = (
            None
            if guild_id is None
            else FakeGuild(guild_id, owner_id, channels, members, cached, fetch_fails)
        )
        self.user = SimpleNamespace(
            id=user_id,
            roles=[SimpleNamespace(id=r) for r in role_ids],
            _roles=list(role_ids),
            display_name=display_name,
            display_avatar=SimpleNamespace(url=MEMBER_AVATAR),
        )
        self.permissions = discord.Permissions(administrator=administrator)
        self.type = type
        self.data = data or {}
        self.extras: dict[str, Any] = {}
        self.message: Any = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = _Response(self)
        self.followup = SimpleNamespace(send=self._followup_send)

    async def _followup_send(self, **kwargs: Any) -> None:
        self.calls.append(("followup", kwargs))

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.calls.append(("edit_original_response", kwargs))

    @property
    def last(self) -> tuple[str, dict[str, Any]]:
        return self.calls[-1]

    @property
    def text(self) -> str:
        """The content of the most recent message sent or edited."""
        return self.last[1].get("content") or ""


def component_ids(view: discord.ui.View) -> list[str]:
    return [item.custom_id for item in view.children]  # type: ignore[attr-defined]
