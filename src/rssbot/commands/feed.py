"""/feed: add, list, edit, remove, pause, resume, test, import and export Feeds.

Also the Feed panel: one private message for one Feed, from which everything about the
Feed can be changed. Other command modules show it with `open_panel`.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import discord
from discord import app_commands

from ..deliver import DEFAULT_THREAD_TITLE, MAX_THREAD_TITLE, build_embed, build_view
from ..models import (
    DEFAULT_INTERVAL_S,
    MAX_FORUM_TAGS,
    ChannelKind,
    Feed,
    Item,
    Level,
    LogEntry,
    OutgoingMessage,
    PauseReason,
    PostAs,
)
from ..ports import DeliveryOutcome
from ..render import MAX_EMBED_URL, MAX_USERNAME
from ..service import (
    MAX_FORUM_TITLE_TEMPLATE_CHARS,
    MAX_IMPORT_FEEDS,
    MAX_MENTION_ROLES,
    MAX_NAME_CHARS,
    MAX_URL_CHARS,
    POST_AS_WORDS,
    FeedStatus,
    OpmlImport,
    ServiceError,
    duration_words,
    pause_cause,
    status_line,
    status_of,
)
from . import _history as history
from . import _ui as ui
from .template import PREVIEW_REFUSED

MESSAGE_CHANNEL_TYPES = (
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
    discord.ChannelType.news_thread,
)
FEED_CHANNEL_TYPES = (*MESSAGE_CHANNEL_TYPES, discord.ChannelType.forum)

INTERVALS = (300, 600, 900, 1800, 3600, 10800, 21600, 43200, 86400)

MAX_IMPORT_BYTES = 1024 * 1024
IMPORT_NAMES_SHOWN = 15
IMPORT_FAILURES_SHOWN = 10
EXPORT_FILENAME = "feeds.opml"
LIST_PAGE_FEEDS = ui.CHOICE_LIMIT  # the select under the list holds no more

WRONG_CHANNEL = (
    "A Feed can only post in a text channel, an announcement channel, a forum channel or a thread."
)
NO_CHANNEL = "The bot cannot find that channel in this Server."
NOT_A_FORUM = "That Feed does not post in a forum channel."
NO_FEEDS = "This Server has no Feeds yet. Add one with `/feed add`."
NO_HISTORY = "This Feed has no Log entries yet."
TAG_NEEDED_AGAIN = (
    "This forum requires a tag. Choose one under Forum options or the Feed will be paused again."
)
PAUSED_NOT_REFRESHED = "That Feed is paused. Resume it with `/feed resume` to refresh it."
EVERYONE_LEFT_OUT = "`@everyone` cannot be a mention role, so it was left out."
ADDRESS_SHOWN = 200
CHOICE_TITLE = 100  # characters of an Item's title in a test's list
HELD_BACK = 5  # how many held-back Items a test names
HELD_BACK_TITLE = 60

POST_GONE = (
    "Nothing was posted: the Feed no longer lists that Item, or its Filters now hold it back."
)

OUTCOME_WORDS = {
    DeliveryOutcome.DELIVERED: "Posted the Item in {channel}.",
    DeliveryOutcome.RETRY: (
        "Discord did not take the message just now. Nothing was posted in {channel}; "
        "try again in a minute."
    ),
    DeliveryOutcome.UNKNOWN: (
        "Discord did not answer, so the bot cannot tell whether the message was posted in "
        "{channel}. Check the channel before trying again."
    ),
    DeliveryOutcome.REJECTED: (
        "Discord refused the message, so nothing was posted in {channel}. "
        "Something in the Feed's Template is not accepted; change it and test again."
    ),
    DeliveryOutcome.LOST_CHANNEL: (
        "The bot cannot post in {channel}. Check that the channel still exists and that "
        "the bot may see it and post in it."
    ),
    DeliveryOutcome.NEEDS_TAG: (
        "{channel} requires a tag on every Forum post and this Feed has none. "
        "Choose one under Forum options on the Feed's panel."
    ),
}


# -- Small helpers --


def _service(interaction: discord.Interaction) -> Any:
    return ui.deps(interaction).service


def _name(feed: Feed) -> str:
    return discord.utils.escape_markdown(ui.cut(feed.name or f"Feed {feed.id}", 80))


def _one_line(text: str, limit: int) -> str:
    return ui.cut(" ".join(text.split()), limit)


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def _is_are(number: int, word: str) -> str:
    return f"is 1 {word}" if number == 1 else f"are {number} {word}s"


def interval_words(seconds: int) -> str:
    """ "every 10 minutes", "every hour", "every 6 hours"."""
    return "every " + duration_words(seconds).removeprefix("1 ")


def _interval_choices(current: int | None = None) -> list[tuple[str, str]]:
    """The listed intervals, plus the Feed's own if it is not one of them."""
    seconds = set(INTERVALS)
    if current is not None:
        seconds.add(current)
    return [(str(value), duration_words(value)) for value in sorted(seconds)]


def _picked_interval(values: ui.FormValues) -> int:
    raw = values.choice("interval") or ""
    if not (raw.isascii() and raw.isdigit() and len(raw) <= 9):
        raise ui.UserError("Choose how often the Feed is checked from the list.")
    return int(raw)


def _from_message(interaction: discord.Interaction) -> bool:
    """Whether the interaction came from a message: a click, or a form opened by one."""
    return getattr(interaction, "message", None) is not None


def _channel_type(interaction: discord.Interaction, channel: Any) -> discord.ChannelType | None:
    """The channel's type as Discord described it, else as the Server's cache has it."""
    kind = getattr(channel, "type", None)
    if kind is None:
        kind = getattr(ui.cached_channel(interaction, channel.id), "type", None)
    return kind


def _kind_of(interaction: discord.Interaction, channel: Any) -> ChannelKind:
    kind = _channel_type(interaction, channel)
    if kind is None:
        raise ui.UserError(NO_CHANNEL)
    if kind is discord.ChannelType.forum:
        return ChannelKind.FORUM
    if kind in MESSAGE_CHANNEL_TYPES:
        return ChannelKind.MESSAGES
    raise ui.UserError(WRONG_CHANNEL)


def _picked_channel(interaction: discord.Interaction, values: ui.FormValues) -> ui.PickedChannel:
    picked = values.channel("channel")
    if picked is None:
        raise ui.UserError("Choose the channel the Feed posts in.")
    return picked


def _current_channel(interaction: discord.Interaction) -> Any | None:
    """The channel the command was used in, if a Feed could post there."""
    channel = getattr(interaction, "channel", None)
    if channel is None or _channel_type(interaction, channel) not in FEED_CHANNEL_TYPES:
        return None
    return channel


def _access_warning(
    interaction: discord.Interaction, channel_id: int, *, embed: bool = False
) -> str:
    """A warning when the bot cannot post in the channel; nothing when it can or cannot tell."""
    missing = ui.missing_post_permissions(interaction, channel_id, embed=embed)
    if not missing:
        return ""
    mention = ui.channel_mention(channel_id)
    if {"View Channel", "Send Messages", "Send Messages in Threads"} & set(missing):
        return (
            f"**Warning**: the bot cannot see or post in {mention}. "
            "Nothing will be posted until the bot is given access to it."
        )
    return (
        f"**Warning**: the bot lacks the {' and '.join(missing)} permission in {mention}. "
        "Posts there may fail until the bot is given it."
    )


async def _cleanup_webhook_if_unused(interaction: discord.Interaction, channel_id: int) -> None:
    shared = ui.deps(interaction)
    if not shared.service.channel_uses_webhook(channel_id):
        await shared.deliverer.cleanup_webhook(channel_id)


# -- The Feed panel --


def _next_check_words(feed: Feed, now: int) -> str:
    return "due now" if feed.next_check_at <= now else f"<t:{feed.next_check_at}:R>"


def _times(feed: Feed, now: int, *, always_next: bool) -> list[tuple[str, str]]:
    """The Feed's times as (label, words), fitted to its status.

    A Paused feed is not checked, so only when it last worked says anything. The next
    Check is unusual, and so worth a place in the list, only when the Feed is not Working.
    """
    status = status_of(feed)
    times = []
    if status is not FeedStatus.PAUSED:
        if feed.last_checked_at is not None:
            times.append(("Last checked", f"<t:{feed.last_checked_at}:R>"))
        if always_next or status is not FeedStatus.WORKING:
            times.append(("Next Check", _next_check_words(feed, now)))
    if status is not FeedStatus.WORKING and feed.last_success_at is not None:
        times.append(("Last worked", f"<t:{feed.last_success_at}:R>"))
    return times


def _post_as_words(feed: Feed) -> str:
    words = POST_AS_WORDS[feed.post_as]
    shown = {PostAs.SITE: feed.site_name, PostAs.CUSTOM: feed.custom_name}.get(feed.post_as, "")
    if shown:
        words += f" ({discord.utils.escape_markdown(ui.cut(shown, 80))})"
    return words


def _status_words(feed: Feed, pause: LogEntry | None, shown: Mapping[int, str]) -> str:
    """The Feed's status line. A Paused feed whose pause has a Log entry says who and when."""
    if feed.paused is None or pause is None:
        return _one_line(status_line(feed), 300)
    when = f"<t:{pause.at}:R>"
    if pause.actor_id is None:
        return f"Paused by the bot: {pause_cause(feed.paused)} · {when}"
    return f"Paused by {ui.actor_words(pause, shown)} · {when}"


async def _panel(interaction: discord.Interaction, feed: Feed) -> tuple[str, discord.ui.View]:
    """The Feed panel, rebuilt from the database each time it is shown."""
    forum = feed.channel_kind is ChannelKind.FORUM
    attribution = ui.deps(interaction).db.feed_attribution(feed.id)
    shown = await ui.actors_of(
        interaction, [entry for entry in (attribution.added, attribution.paused) if entry]
    )
    filters = len(_service(interaction).list_filters(feed.server_id, feed.id))
    fields = len(feed.embed.fields) if feed.embed is not None else 0
    added = attribution.added
    added_by = [f"**Added by**: {ui.actor_words(added, shown)} · <t:{added.at}:D>"] if added else []
    lines = [
        f"**Feed**: {_name(feed)}",
        f"**Address**: <{ui.cut(feed.url, 300)}>",
        f"**Channel**: {ui.channel_mention(feed.channel_id)}" + (" (forum)" if forum else ""),
        f"**Check interval**: {interval_words(feed.interval_s)}",
        f"**Status**: {_status_words(feed, attribution.paused, shown)}",
        *(
            f"**{label}**: {words}"
            for label, words in _times(feed, _service(interaction).now(), always_next=True)
        ),
        *added_by,
        f"**Post as**: {_post_as_words(feed)}",
        f"**Message text**: {'yes' if feed.text_template.strip() else 'no'} · "
        f"**Embed**: {'yes' if feed.embed is not None else 'no'}",
        f"**Filters**: {filters} · **Fields**: {fields} · **Buttons**: {len(feed.buttons)} · "
        f"**Mention roles**: {len(feed.mention_role_ids)}",
    ]
    if forum:
        lines.append(
            f"**Forum post title**: `{ui.cut(feed.forum_title_template, 100)}` · "
            f"**Tags**: {len(feed.forum_tag_ids)} · "
            f"**Cover image**: {'on' if feed.forum_cover else 'off'}"
        )
    warning = _access_warning(interaction, feed.channel_id, embed=feed.embed is not None)
    if warning:
        lines.append(warning)
    pause_or_resume = (
        PauseFeed(feed.id, row=2) if feed.paused is None else ResumeFeed(feed.id, row=2)
    )
    view = ui.view_of(
        SettingsButton(feed.id, row=0),
        TextButton(feed.id, row=0),
        EmbedButton(feed.id, row=0),
        FieldsButton(feed.id, row=0),
        ButtonsButton(feed.id, row=0),
        FiltersButton(feed.id, row=1),
        MentionsButton(feed.id, row=1),
        PostAsButton(feed.id, row=1),
        ForumButton(feed.id, row=1) if forum else None,
        TestButton(feed.id, row=2),
        RefreshButton(feed.id, row=2) if feed.paused is None else None,
        pause_or_resume,
        AskRemoveButton(feed.id, row=2),
    )
    return "\n".join(lines), view


async def _show_panel(
    interaction: discord.Interaction, feed: Feed, *, edit: bool = False, note: str = ""
) -> None:
    content, view = await _panel(interaction, feed)
    if note:
        content = f"{note}\n\n{content}"
    if edit:
        await ui.edit(interaction, content, view=view)
    else:
        await ui.reply(interaction, content, view=view)


async def open_panel(interaction: discord.Interaction, feed_id: int, *, edit: bool = False) -> None:
    """Show the Feed panel. With edit=True it replaces the message the interaction came from."""
    await _show_panel(interaction, ui.feed_of(interaction, feed_id), edit=edit)


class BackToPanel(ui.ActionButton, action="feed_panel", ids=1, requires=Level.MANAGER):
    label = "Back to Feed"

    async def handle(self, interaction: discord.Interaction) -> None:
        await open_panel(interaction, self.ids[0], edit=True)


# -- Settings: the form behind the panel's Settings button --


async def _show_settings_form(interaction: discord.Interaction, feed: Feed) -> None:
    # Discord may refuse a pre-selected channel that was deleted since; the Feed stays bound to it.
    # Not in the cache is not deleted (an archived thread is dropped too), so with no default
    # to show the field is optional: leaving it empty keeps the Feed where it is.
    channel_here = ui.cached_channel(interaction, feed.channel_id) is not None
    await ui.show_form(
        interaction,
        "feed_edit",
        feed.id,
        title="Feed settings",
        fields=[
            ui.text_field("name", "Name", default=feed.name, max_length=MAX_NAME_CHARS),
            ui.text_field("url", "Feed address", default=feed.url, max_length=MAX_URL_CHARS),
            ui.channel_field(
                "channel",
                "Channel",
                channel_types=FEED_CHANNEL_TYPES,
                default_id=feed.channel_id if channel_here else None,
                required=channel_here,
                description="Where the Feed's Items are posted.",
            ),
            ui.choice_field(
                "interval",
                "Check every",
                _interval_choices(feed.interval_s),
                default=str(feed.interval_s),
            ),
        ],
    )


@ui.form_handler("feed_edit", ids=1, requires=Level.MANAGER)
async def _settings_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    picked = values.channel("channel")  # None: left empty, the Feed stays in its channel
    interval = _picked_interval(values)
    moved_to = picked if picked is not None and picked.id != feed.channel_id else None
    kind = _kind_of(interaction, moved_to) if moved_to else None
    in_place = _from_message(interaction)
    await ui.defer(interaction, update=in_place)
    updated, old_channel_id = await _service(interaction).edit_feed(
        feed.server_id,
        feed.id,
        name=values.text("name"),
        url=values.text("url"),
        channel_id=moved_to.id if moved_to else None,
        channel_kind=kind,
        interval_s=interval,
        actor=ui.actor_of(interaction),
    )
    if old_channel_id is not None:
        await _cleanup_webhook_if_unused(interaction, old_channel_id)
    await _show_panel(interaction, updated, edit=in_place)


class SettingsButton(ui.ActionButton, action="feed_settings", ids=1, requires=Level.MANAGER):
    label = "Settings"

    async def handle(self, interaction: discord.Interaction) -> None:
        await _show_settings_form(interaction, ui.feed_of(interaction, self.ids[0]))


# -- Template and Filters: owned by other command modules --


class _OpenerButton(ui.ActionButton):
    """A panel button that hands the interaction to another command module, undeferred."""

    module: ClassVar[str] = ""
    opener: ClassVar[str] = ""

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        # Imported here so that this module loads even when the other one is missing.
        if self.module == "filter":
            from . import filter as other
        else:
            from . import template as other
        await getattr(other, self.opener)(interaction, feed.id)


class TextButton(_OpenerButton, action="feed_text", ids=1, requires=Level.MANAGER):
    label = "Message text"
    module, opener = "template", "open_text"


class EmbedButton(_OpenerButton, action="feed_embed", ids=1, requires=Level.MANAGER):
    label = "Embed"
    module, opener = "template", "open_embed"


class FieldsButton(_OpenerButton, action="feed_fields", ids=1, requires=Level.MANAGER):
    label = "Fields"
    module, opener = "template", "open_fields"


class ButtonsButton(_OpenerButton, action="feed_buttons", ids=1, requires=Level.MANAGER):
    label = "Buttons"
    module, opener = "template", "open_buttons"


class FiltersButton(_OpenerButton, action="feed_filters", ids=1, requires=Level.MANAGER):
    label = "Filters"
    module, opener = "filter", "open_filters"


# -- Mentions --


class MentionsButton(ui.ActionButton, action="feed_mentions", ids=1, requires=Level.MANAGER):
    label = "Mentions"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        roles = feed.mention_role_ids
        now = ", ".join(ui.role_mention(role_id) for role_id in roles) or "none"
        content = (
            f"Choose the roles **{_name(feed)}** mentions with every Item it posts, "
            f"up to {MAX_MENTION_ROLES}.\n**Mention roles now**: {now}"
        )
        # Discord may refuse a pre-selected role that was deleted since.
        get_role = getattr(interaction.guild, "get_role", None)
        existing = [r for r in roles if get_role is None or get_role(r) is not None]
        view = ui.view_of(
            MentionsSelect(feed.id, current=existing),
            ClearMentions(feed.id, disabled=not roles),
            BackToPanel(feed.id),
        )
        await ui.edit(interaction, content, view=view)


class MentionsSelect(ui.ActionSelect, action="feed_mentions_set", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, current: Sequence[int] = ()
    ) -> discord.ui.RoleSelect[discord.ui.View]:
        return discord.ui.RoleSelect(
            custom_id=custom_id,
            placeholder="Choose the roles to mention",
            min_values=0,
            max_values=MAX_MENTION_ROLES,
            default_values=[discord.Object(role_id) for role_id in current[:MAX_MENTION_ROLES]],
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        picked = self.picked_ids
        updated = await _service(interaction).set_mentions(
            feed.server_id, feed.id, picked, actor=ui.actor_of(interaction)
        )
        left_out = set(picked) - set(updated.mention_role_ids)
        await _show_panel(
            interaction, updated, edit=True, note=EVERYONE_LEFT_OUT if left_out else ""
        )


class ClearMentions(ui.ActionButton, action="feed_mentions_clear", ids=1, requires=Level.MANAGER):
    label = "Clear"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        updated = await _service(interaction).set_mentions(
            feed.server_id, feed.id, (), actor=ui.actor_of(interaction)
        )
        await _show_panel(interaction, updated, edit=True)


# -- Post as --


class PostAsButton(ui.ActionButton, action="feed_post_as", ids=1, requires=Level.MANAGER):
    label = "Post as"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        content = (
            f"Choose the name and picture **{_name(feed)}** posts under.\n"
            f"**Post as now**: {_post_as_words(feed)}"
        )
        view = ui.view_of(PostAsSelect(feed.id, current=feed.post_as), BackToPanel(feed.id))
        await ui.edit(interaction, content, view=view)


class PostAsSelect(ui.ActionSelect, action="feed_post_as_set", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, current: PostAs | None = None
    ) -> discord.ui.Select[discord.ui.View]:
        return discord.ui.Select(
            custom_id=custom_id,
            placeholder="Choose who the Feed posts as",
            options=[
                discord.SelectOption(label=words, value=choice.value, default=choice is current)
                for choice, words in POST_AS_WORDS.items()
            ],
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        try:
            choice = PostAs(self.picked[0]) if self.picked else None
        except ValueError:
            choice = None
        if choice is None:
            raise ui.UserError("Choose who the Feed posts as from the list.")
        if choice is PostAs.CUSTOM:
            # A form must be the first response, so nothing is deferred here.
            await ui.show_form(
                interaction,
                "feed_custom",
                feed.id,
                title="Post as a custom name",
                fields=[
                    ui.text_field(
                        "name", "Name", default=feed.custom_name, max_length=MAX_USERNAME
                    ),
                    ui.text_field(
                        "picture",
                        "Picture address",
                        default=feed.custom_avatar,
                        required=False,
                        max_length=MAX_EMBED_URL,
                        placeholder="https://example.com/picture.png",
                        description="A web address of an image. Leave empty for no picture.",
                    ),
                ],
            )
            return
        await ui.defer(interaction, update=True)  # Site looks the site up; Bot may clean up
        updated = await _service(interaction).set_post_as(
            feed.server_id, feed.id, choice, actor=ui.actor_of(interaction)
        )
        if choice is PostAs.BOT and feed.post_as is not PostAs.BOT:
            await _cleanup_webhook_if_unused(interaction, feed.channel_id)
        await _show_panel(interaction, updated, edit=True)


@ui.form_handler("feed_custom", ids=1, requires=Level.MANAGER)
async def _custom_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = ui.feed_of(interaction, ids[0])
    updated = await _service(interaction).set_post_as(
        feed.server_id,
        feed.id,
        PostAs.CUSTOM,
        custom_name=values.text("name"),
        custom_avatar=values.text("picture"),
        actor=ui.actor_of(interaction),
    )
    await _show_panel(interaction, updated, edit=_from_message(interaction))


# -- Forum options --


def _forum_feed(interaction: discord.Interaction, feed_id: int) -> Feed:
    feed = ui.feed_of(interaction, feed_id)
    if feed.channel_kind is not ChannelKind.FORUM:
        raise ui.UserError(NOT_A_FORUM)
    return feed


class ForumButton(ui.ActionButton, action="feed_forum", ids=1, requires=Level.MANAGER):
    label = "Forum options"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = _forum_feed(interaction, self.ids[0])
        forum = ui.cached_channel(interaction, feed.channel_id)
        tags = list(getattr(forum, "available_tags", ()))[: ui.CHOICE_LIMIT]
        names = {tag.id: tag.name for tag in tags}
        chosen = ", ".join(
            discord.utils.escape_markdown(names.get(tag_id, "a removed tag"))
            for tag_id in feed.forum_tag_ids
        )
        lines = [
            f"Forum options of **{_name(feed)}** in {ui.channel_mention(feed.channel_id)}.",
            f"**Forum post title**: `{ui.cut(feed.forum_title_template, 200)}`",
            f"**Tags**: {chosen or 'none'}",
            f"**Cover image**: {'on' if feed.forum_cover else 'off'}",
        ]
        if forum is None:
            lines.append("The bot cannot see that forum right now, so its tags are not listed.")
        elif not tags:
            lines.append("That forum has no tags to choose from.")
        view = ui.view_of(
            ForumTagsSelect(feed.id, tags=tags, current=feed.forum_tag_ids) if tags else None,
            ForumTitleButton(feed.id),
            ForumCoverButton(
                feed.id,
                0 if feed.forum_cover else 1,
                label=f"Cover image: {'On' if feed.forum_cover else 'Off'}",
            ),
            BackToPanel(feed.id),
        )
        await ui.edit(interaction, "\n".join(lines), view=view)


class ForumTitleButton(ui.ActionButton, action="feed_forum_title", ids=1, requires=Level.MANAGER):
    label = "Post title"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = _forum_feed(interaction, self.ids[0])
        await ui.show_form(
            interaction,
            "feed_title",
            feed.id,
            title="Forum post title",
            fields=[
                ui.text_field(
                    "title",
                    "Forum post title",
                    default=feed.forum_title_template,
                    max_length=MAX_FORUM_TITLE_TEMPLATE_CHARS,
                    description="Placeholders such as {{title}} are replaced for each Item.",
                )
            ],
        )


@ui.form_handler("feed_title", ids=1, requires=Level.MANAGER)
async def _forum_title_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    feed = _forum_feed(interaction, ids[0])
    updated = await _service(interaction).set_forum_title(
        feed.server_id, feed.id, values.text("title"), actor=ui.actor_of(interaction)
    )
    await _show_panel(interaction, updated, edit=_from_message(interaction))


class ForumTagsSelect(ui.ActionSelect, action="feed_forum_tags", ids=1, requires=Level.MANAGER):
    def build(
        self, custom_id: str, *, tags: Sequence[Any] = (), current: Sequence[int] = ()
    ) -> discord.ui.Select[discord.ui.View]:
        options = [
            discord.SelectOption(
                label=ui.cut(tag.name, 100) or str(tag.id),
                value=str(tag.id),
                default=tag.id in current,
            )
            for tag in tags[: ui.CHOICE_LIMIT]
        ]
        return discord.ui.Select(
            custom_id=custom_id,
            placeholder="Choose the tags put on every Forum post",
            min_values=0,
            max_values=max(1, min(MAX_FORUM_TAGS, len(options))),
            options=options,
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = _forum_feed(interaction, self.ids[0])
        updated = await _service(interaction).set_forum_tags(
            feed.server_id, feed.id, self.picked_ids, actor=ui.actor_of(interaction)
        )
        await _show_panel(interaction, updated, edit=True)


class ForumCoverButton(ui.ActionButton, action="feed_forum_cover", ids=2, requires=Level.MANAGER):
    """Its second id is what a click sets: 1 turns the Cover image on, 0 turns it off."""

    label = "Cover image"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = _forum_feed(interaction, self.ids[0])
        updated = await _service(interaction).set_forum_cover(
            feed.server_id, feed.id, self.ids[1] == 1, actor=ui.actor_of(interaction)
        )
        await _show_panel(interaction, updated, edit=True)


# -- Test --


def _thread_title(message: OutgoingMessage) -> str:
    """The Forum post's title as the deliverer would make it."""
    title = (message.thread_title or "").strip()[:MAX_THREAD_TITLE].strip()
    return title or DEFAULT_THREAD_TITLE


def _item_ref(item: Item) -> int:
    """A number that stands for the Item in a custom id, which carries only numbers."""
    return int.from_bytes(hashlib.sha256(item.key.encode()).digest()[:7])


def _item_title(item: Item, limit: int) -> str:
    return discord.utils.escape_markdown(_one_line(item.title, limit)) or "(no title)"


def _held_back_line(held_back: Sequence[Item]) -> str:
    titles = ", ".join(f"*{_item_title(item, HELD_BACK_TITLE)}*" for item in held_back[:HELD_BACK])
    more = len(held_back) - HELD_BACK
    return f"Held back by your Filters: {titles}" + (f" and {more} more." if more > 0 else ".")


async def _send_preview(
    interaction: discord.Interaction, feed: Feed, ref: int | None = None
) -> None:
    """Show privately what the Feed would post for one of its newest Items, then offer to
    post it. Only Items the Feed's Filters let through are shown: the newest, or the one
    `ref` stands for."""
    await ui.defer(interaction)
    service = _service(interaction)
    listing = await service.test_listing(feed.server_id, feed.id)
    back = BackToPanel(feed.id)
    if not listing.choices:
        count = "the only Item" if listing.listed == 1 else f"all {listing.listed} Items"
        lines = [
            f"Your Filters hold back {count} that **{_name(feed)}** lists right now, "
            "so there is nothing to test.",
            _held_back_line(listing.held_back),
        ]
        await ui.reply(interaction, "\n".join(lines), view=ui.view_of(back))
        return
    refs = [_item_ref(item) for item in listing.choices]
    at = refs.index(ref) if ref in refs else 0
    item = listing.choices[at]
    message = service.render_item(feed.server_id, feed.id, item)
    embed = build_embed(message.embed, published=message.published)
    content = message.content or (None if embed is not None else "*(no message text)*")
    try:
        await ui.reply(interaction, content, embed=embed, view=build_view(message))
    except discord.HTTPException:
        await ui.reply(interaction, PREVIEW_REFUSED, view=ui.view_of(back))
        return

    channel = ui.channel_mention(feed.channel_id)
    if at:
        which = f"Item {at + 1}"
    elif listing.held_back:
        which = "the newest Item your Filters let through"
    else:
        which = "the newest Item"
    lines = []
    if ref is not None and ref not in refs:
        lines.append("The Item you chose is no longer one the Feed would post.")
    lines.append(
        f"Above is {which} of **{_name(feed)}** as it would be posted in {channel}. "
        "Nothing has been posted."
    )
    if feed.channel_kind is ChannelKind.FORUM:
        title = discord.utils.escape_markdown(_thread_title(message))
        lines.append(f"**Forum post title**: {title}")
        if message.cover_image_url:
            lines.append(f"**Cover image**: <{ui.cut(message.cover_image_url, 300)}>")
    if message.username:
        lines.append(f"**Post as**: {discord.utils.escape_markdown(message.username)}")
    several = len(listing.choices) > 1
    if several:
        for number, choice in enumerate(listing.choices, start=1):
            line = f"{number}. {_item_title(choice, CHOICE_TITLE)}"
            lines.append(f"**{line}**" if choice is item else line)
    if listing.held_back:
        lines.append(_held_back_line(listing.held_back))
    if feed.mention_role_ids:
        roles = ", ".join(ui.role_mention(role_id) for role_id in feed.mention_role_ids)
        lines.append(f"Posting it will mention {roles}.")
    numbers = [
        PickPreview(feed.id, other, label=str(number), disabled=other == refs[at], row=0)
        for number, other in enumerate(refs, start=1)
        if several
    ]
    view = ui.view_of(*numbers, PostPreview(feed.id, refs[at], row=1), BackToPanel(feed.id, row=1))
    await ui.reply(interaction, "\n".join(lines), view=view)


class TestButton(ui.ActionButton, action="feed_test", ids=1, requires=Level.MANAGER):
    label = "Test"

    async def handle(self, interaction: discord.Interaction) -> None:
        await _send_preview(interaction, ui.feed_of(interaction, self.ids[0]))


class PickPreview(ui.ActionButton, action="feed_test_pick", ids=2, requires=Level.MANAGER):
    """Its second id stands for the Item to show (`_item_ref`)."""

    label = "Item"

    async def handle(self, interaction: discord.Interaction) -> None:
        await _send_preview(interaction, ui.feed_of(interaction, self.ids[0]), self.ids[1])


class PostPreview(ui.ActionButton, action="feed_post", ids=2, requires=Level.MANAGER):
    """Its second id stands for the Item to post (`_item_ref`)."""

    label = "Post to channel"
    style = discord.ButtonStyle.primary

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        await ui.defer(interaction, update=True)
        shared = ui.deps(interaction)
        listing = await shared.service.test_listing(feed.server_id, feed.id)
        item = next((i for i in listing.choices if _item_ref(i) == self.ids[1]), None)
        if item is None:
            # The source moved on, or a Filter added since holds the Item back.
            ui.refused(interaction, "the Item is no longer one the Feed would post")
            view = ui.view_of(TestButton(feed.id, label="Test again"), BackToPanel(feed.id))
            await ui.edit(interaction, POST_GONE, view=view)
            return
        message = shared.service.render_item(feed.server_id, feed.id, item)
        outcome = await shared.deliverer.deliver(feed, message)
        if outcome is DeliveryOutcome.DELIVERED:  # no Check should post it again
            await shared.service.record_posted(feed.server_id, feed.id, item)
        words = OUTCOME_WORDS.get(outcome, OUTCOME_WORDS[DeliveryOutcome.RETRY])
        content = words.format(channel=ui.channel_mention(feed.channel_id))
        if outcome is DeliveryOutcome.DELIVERED:
            await ui.edit(interaction, content, view=ui.view_of(BackToPanel(feed.id)))
        else:
            again = PostPreview(feed.id, self.ids[1], label="Try again")
            await ui.edit(interaction, content, view=ui.view_of(again, BackToPanel(feed.id)))


# -- Refresh --


async def _refresh(interaction: discord.Interaction, feed: Feed) -> str:
    """Check the Feed without waiting for its turn and say how it went. Defer first."""
    ran = await ui.deps(interaction).scheduler.check_feed(feed.id)
    if not ran:
        return f"The Feed **{_name(feed)}** is being refreshed right now."
    after = ui.feed_of(interaction, feed.id)
    channel = ui.channel_mention(after.channel_id)
    if status_of(after) is not FeedStatus.WORKING:
        return f"Refreshed the Feed **{_name(after)}**. {_one_line(status_line(after), 300)}"
    return f"Refreshed the Feed **{_name(after)}**. Any new Items are now in {channel}."


class RefreshButton(ui.ActionButton, action="feed_refresh", ids=1, requires=Level.MANAGER):
    label = "Refresh"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        if feed.paused is not None:
            raise ui.UserError(PAUSED_NOT_REFRESHED)
        await ui.defer(interaction, update=True)
        note = await _refresh(interaction, feed)
        await _show_panel(interaction, ui.feed_of(interaction, feed.id), edit=True, note=note)


# -- Pause, resume, remove --


class PauseFeed(ui.ActionButton, action="feed_pause", ids=1, requires=Level.MANAGER):
    label = "Pause"

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        updated = await _service(interaction).pause_feed(
            feed.server_id, feed.id, actor=ui.actor_of(interaction)
        )
        await _show_panel(interaction, updated, edit=True)


def _needs_tag_again(reason: PauseReason | None, resumed: Feed) -> bool:
    """Whether a Feed paused for want of a forum tag was resumed still without one."""
    return reason is PauseReason.NEEDS_TAG and not resumed.forum_tag_ids


class ResumeFeed(ui.ActionButton, action="feed_resume", ids=1, requires=Level.MANAGER):
    label = "Resume"
    style = discord.ButtonStyle.success

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        updated = await _service(interaction).resume_feed(
            feed.server_id, feed.id, actor=ui.actor_of(interaction)
        )
        # A channel the bot still cannot post in is warned about on the panel itself.
        note = TAG_NEEDED_AGAIN if _needs_tag_again(feed.paused, updated) else ""
        await _show_panel(interaction, updated, edit=True, note=note)


def _remove_question(feed: Feed) -> str:
    return (
        f"Remove the Feed **{_name(feed)}** from {ui.channel_mention(feed.channel_id)}? "
        "Its Template and Filters are removed with it. This cannot be undone."
    )


def _remove_button(feed: Feed) -> RemoveFeed:
    return RemoveFeed(feed.id, label="Remove", style=discord.ButtonStyle.danger)


class AskRemoveButton(ui.ActionButton, action="feed_remove_ask", ids=1, requires=Level.MANAGER):
    label = "Remove"
    style = discord.ButtonStyle.danger

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        # Cancel leads back to the panel this confirmation replaces.
        view = ui.view_of(_remove_button(feed), BackToPanel(feed.id, label="Cancel"))
        await ui.edit(interaction, _remove_question(feed), view=view)


class RemoveFeed(ui.ActionButton, action="feed_remove", ids=1, requires=Level.MANAGER):
    label = "Remove"
    style = discord.ButtonStyle.danger

    async def handle(self, interaction: discord.Interaction) -> None:
        feed = ui.feed_of(interaction, self.ids[0])
        await ui.defer(interaction, update=True)
        shared = ui.deps(interaction)
        removed = await shared.service.remove_feed(
            feed.server_id, feed.id, actor=ui.actor_of(interaction)
        )
        if not removed.webhook_in_use:
            await shared.deliverer.cleanup_webhook(removed.channel_id)
        await ui.edit(
            interaction,
            f"Removed the Feed **{_name(removed.feed)}** from "
            f"{ui.channel_mention(removed.channel_id)}.",
        )


# -- The list --


_LIST_TIME_WORDS = {
    "Last checked": "checked",
    "Next Check": "next Check",
    "Last worked": "last worked",
}


def _list_entry(
    feed: Feed, now: int, pause: LogEntry | None = None, shown: Mapping[int, str] | None = None
) -> str:
    """A Feed in the list: its name, channel and status, and its times in small text under it."""
    entry = (
        f"**{discord.utils.escape_markdown(ui.cut(feed.name, 50))}** in "
        f"{ui.channel_mention(feed.channel_id)}: {_status_words(feed, pause, shown or {})}"
    )
    times = " · ".join(
        f"{_LIST_TIME_WORDS[label]} {words}"
        for label, words in _times(feed, now, always_next=False)
    )
    return f"{entry}\n-# {times}" if times else entry


def _list_header(feeds: Sequence[Feed]) -> str:
    """How many Feeds the Server has, and how many of each status when they are not alike."""
    counts = {status: 0 for status in reversed(FeedStatus)}  # working first
    for feed in feeds:
        counts[status_of(feed)] += 1
    header = f"**Feeds in this Server**: {len(feeds)}"
    if counts[FeedStatus.WORKING] == len(feeds):
        return header
    parts = ", ".join(f"{count} {status.value}" for status, count in counts.items() if count)
    return f"{header} ({parts})"


def _list_pages(entries: Sequence[str], room: int) -> list[range]:
    """Split the entries into pages, each as full as `room` characters and the select allow."""
    pages: list[range] = []
    start, used = 0, 0
    for index, entry in enumerate(entries):
        cost = len(entry) + 2  # the blank line before it
        if index > start and (used + cost > room or index - start >= LIST_PAGE_FEEDS):
            pages.append(range(start, index))
            start, used = index, 0
        used += cost
    pages.append(range(start, len(entries)))
    return pages


async def _list(
    interaction: discord.Interaction, page_number: int
) -> tuple[str, discord.ui.View | None]:
    server_id = ui.server_id_of(interaction)
    feeds = _service(interaction).list_feeds(server_id)
    if not feeds:
        return NO_FEEDS, None
    now = _service(interaction).now()
    attributions = ui.deps(interaction).db.feed_attributions(server_id)
    pauses = {
        feed.id: pause for feed in feeds if (pause := attributions[feed.id].paused) is not None
    }
    header = _list_header(feeds)
    # Pages are cut before the members on one are looked up, so each is budgeted for the
    # longest way a member can be shown.
    longest = {pause.actor_id: ui.LONGEST_ACTOR for pause in pauses.values() if pause.actor_id}
    budget = [_list_entry(feed, now, pauses.get(feed.id), longest) for feed in feeds]
    footer_room = len(f"\n\nPage {len(feeds)} of {len(feeds)}")
    spans = _list_pages(budget, ui.MESSAGE_LIMIT - len(header) - footer_room)
    number = min(max(page_number, 0), len(spans) - 1)
    span = spans[number]
    page = ui.Page(feeds[span.start : span.stop], number, len(spans))
    shown = await ui.actors_of(
        interaction, [pauses[feed.id] for feed in page.items if feed.id in pauses]
    )
    entries = [_list_entry(feed, now, pauses.get(feed.id), shown) for feed in page.items]
    lines = [header, *entries]
    if page.pages > 1:
        lines.append(page.footer)
    names = {
        feed.id: getattr(ui.cached_channel(interaction, feed.channel_id), "name", "")
        for feed in page.items
    }
    shown = page.items[: ui.CHOICE_LIMIT]
    details = {
        feed.id: detail
        for feed, detail in zip(shown, ui.feed_details(interaction, shown), strict=True)
    }
    view = ui.view_of(
        FeedListOpen(feeds=page.items, channel_names=names, details=details),
        *ui.page_buttons(FeedListPage, page=page.page, pages=page.pages),
    )
    return "\n\n".join(lines), view


class FeedListPage(ui.PageButton, action="feed_list_page", ids=1, requires=Level.MANAGER):
    async def handle(self, interaction: discord.Interaction) -> None:
        content, view = await _list(interaction, self.page)
        await ui.edit(interaction, content, view=view)


class FeedListOpen(ui.ActionSelect, action="feed_list_open", requires=Level.MANAGER):
    def build(
        self,
        custom_id: str,
        *,
        feeds: Sequence[Feed] = (),
        channel_names: dict[int, str] | None = None,
        details: dict[int, str] | None = None,
    ) -> discord.ui.Select[discord.ui.View]:
        """`details` holds, by Feed id, what tells apart two Feeds that would read the same."""
        names = channel_names or {}
        extra = details or {}

        def describe(feed: Feed) -> str | None:
            parts = [f"#{ui.cut(names[feed.id], 60)}"] if names.get(feed.id) else []
            if extra.get(feed.id):
                parts.append(ui.cut(extra[feed.id], 30))
            return " · ".join(parts) or None

        return discord.ui.Select(
            custom_id=custom_id,
            placeholder="Open a Feed's panel",
            options=[
                discord.SelectOption(
                    label=ui.cut(feed.name, 100) or f"Feed {feed.id}",
                    value=str(feed.id),
                    description=describe(feed),
                )
                for feed in feeds[: ui.CHOICE_LIMIT]
            ],
        )

    async def handle(self, interaction: discord.Interaction) -> None:
        picked = self.picked_ids
        if not picked:
            raise ui.UserError("Choose a Feed from the list.")
        try:
            feed = ui.feed_of(interaction, picked[0])
        except ui.UserError as exc:
            if exc.user_message != ui.FEED_GONE:
                raise
            # The list is out of date: show it again as it is now, in place.
            content, view = await _list(interaction, 0)
            await ui.edit(interaction, f"{ui.FEED_GONE}\n\n{content}", view=view)
            return
        await _show_panel(interaction, feed)  # a new message: the list stays usable


# -- History --


async def _history(
    interaction: discord.Interaction, feed: Feed, page_number: int
) -> tuple[str, discord.ui.View | None]:
    """One page of the Feed's Log entries, newest first."""
    db = ui.deps(interaction).db
    total = db.count_log_entries(feed.server_id, feed_id=feed.id)
    if total == 0:
        return NO_HISTORY, None
    number, pages = history.page_of(total, page_number)
    entries = db.list_log_entries(
        feed.server_id,
        feed_id=feed.id,
        limit=history.PAGE_SIZE,
        offset=number * history.PAGE_SIZE,
    )
    content = await history.render(
        interaction,
        entries,
        header=f"**History of {_name(feed)}**: {history.entries_words(total)}",
        footer=ui.Page(entries, number, pages).footer if pages > 1 else "",
        show_feed=False,
    )
    buttons = ui.page_buttons(HistoryPage, feed.id, page=number, pages=pages)
    return content, ui.view_of(*buttons) if buttons else None


class HistoryPage(ui.PageButton, action="feed_history_page", ids=2, requires=Level.MANAGER):
    async def handle(self, interaction: discord.Interaction) -> None:
        content, view = await _history(interaction, ui.feed_of(interaction, self.ids[0]), self.page)
        await ui.edit(interaction, content, view=view)


# -- Adding --


@ui.form_handler("feed_add", requires=Level.MANAGER)
async def _add_submitted(
    interaction: discord.Interaction, ids: tuple[int, ...], values: ui.FormValues
) -> None:
    picked = _picked_channel(interaction, values)
    kind = _kind_of(interaction, picked)
    interval = _picked_interval(values)
    try:
        post_as = PostAs(values.choice("post_as") or PostAs.BOT.value)
    except ValueError:
        raise ui.UserError("Choose who the Feed posts as from the list.") from None
    await ui.defer(interaction)
    typed = values.text("url")
    try:
        feed, listed = await _service(interaction).add_feed(
            ui.server_id_of(interaction),
            picked.id,
            kind,
            typed,
            interval_s=interval,
            # A custom name and picture are entered afterwards: this form has no room for them.
            post_as=PostAs.SITE if post_as is PostAs.SITE else PostAs.BOT,
            actor=ui.actor_of(interaction),
        )
    except ServiceError as exc:
        # The form is gone by now, so the sentence says which address it is about.
        address = _one_line(typed.replace("`", ""), ADDRESS_SHOWN)
        if not address:
            raise
        # The line in the container log goes without the address: it can carry a private key.
        sentence = ui.user_message(exc)
        raise ui.UserError(f"{sentence} (`{address}`)", log_reason=sentence) from exc
    note = f"Added the Feed **{_name(feed)}** in {ui.channel_mention(feed.channel_id)}. "
    if listed:
        now = "1 Item" if listed == 1 else f"{listed} Items"
        note += (
            f"Its source lists {now} now. None of them will be posted: "
            "only Items published from now on are."
        )
    else:
        note += "Its source lists no Items now. Items published from now on will be posted."
    if post_as is PostAs.CUSTOM:
        note += (
            "\nIt posts as the bot for now. Press **Post as** below to enter "
            "the custom name and picture."
        )
    await _show_panel(interaction, feed, note=note)


# -- Import summary --


def _import_summary(result: OpmlImport, channel_id: int, warning: str = "") -> str:
    """Counts first and failures last, so a cut for length only loses detail."""
    lines = [f"Imported the file into {ui.channel_mention(channel_id)}."]
    added = len(result.added)
    if warning and added:
        lines.append(warning)
    if added:
        names = ", ".join(
            discord.utils.escape_markdown(ui.cut(name, 40))
            for name in result.added[:IMPORT_NAMES_SHOWN]
        )
        more = added - IMPORT_NAMES_SHOWN
        lines.append(f"**Added**: {added} ({names}{f' and {more} more' if more > 0 else ''})")
        lines.append("Items their sources list now will not be posted, only later ones.")
    else:
        lines.append("**Added**: 0")
    if result.skipped:
        lines.append(
            f"**Skipped**: {result.skipped} (the channel already has a Feed for each of these)"
        )
    if result.left_out:
        lines.append(
            f"**Left out**: {result.left_out} (one import adds at most {MAX_IMPORT_FEEDS} "
            "Feeds; import the file again for the rest)"
        )
    if result.failed:
        lines.append(f"**Failed**: {len(result.failed)}")
        lines.extend(
            f"- {discord.utils.escape_markdown(ui.cut(failure.title or failure.url, 40))}: "
            f"{_one_line(failure.reason, 90)}"
            for failure in result.failed[:IMPORT_FAILURES_SHOWN]
        )
        more = len(result.failed) - IMPORT_FAILURES_SHOWN
        if more > 0:
            lines.append(f"…and {more} more.")
    return "\n".join(lines)


class _ChannelOption(app_commands.Transformer):
    """A channel option limited to where a Feed can post, passed on as Discord sent it."""

    @property
    def type(self) -> discord.AppCommandOptionType:
        return discord.AppCommandOptionType.channel

    @property
    def channel_types(self) -> list[discord.ChannelType]:
        return list(FEED_CHANNEL_TYPES)

    async def transform(self, interaction: discord.Interaction, value: Any, /) -> Any:
        return value


# -- /feed --

group = app_commands.guild_only()(
    app_commands.Group(name="feed", description="Add and manage this Server's Feeds.")
)


@group.command(name="add", description="Add a Feed that posts new Items in a channel.")
async def add_command(interaction: discord.Interaction) -> None:
    ui.require_manager(interaction)
    current = _current_channel(interaction)
    await ui.show_form(
        interaction,
        "feed_add",
        title="Add a Feed",
        fields=[
            ui.text_field(
                "url",
                "Feed address",
                max_length=MAX_URL_CHARS,
                placeholder="https://example.com/feed.xml",
            ),
            ui.channel_field(
                "channel",
                "Channel",
                channel_types=FEED_CHANNEL_TYPES,
                default_id=None if current is None else current.id,
                description="Where the Feed's Items are posted.",
            ),
            ui.choice_field(
                "interval", "Check every", _interval_choices(), default=str(DEFAULT_INTERVAL_S)
            ),
            ui.choice_field(
                "post_as",
                "Post as",
                [(choice.value, words) for choice, words in POST_AS_WORDS.items()],
                default=PostAs.BOT.value,
                description="The name and picture the Feed's messages appear under.",
            ),
        ],
    )


@group.command(name="list", description="List this Server's Feeds.")
async def list_command(interaction: discord.Interaction) -> None:
    ui.require_manager(interaction)
    content, view = await _list(interaction, 0)
    await ui.reply(interaction, content, view=view)


@group.command(name="history", description="Show who did what to a Feed, newest first.")
@app_commands.describe(feed="The Feed to show the Log entries of.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def history_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    content, view = await _history(interaction, ui.feed_from_option(interaction, feed), 0)
    await ui.reply(interaction, content, view=view)


@group.command(name="edit", description="Open a Feed's panel to change anything about it.")
@app_commands.describe(feed="The Feed to change.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def edit_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    await _show_panel(interaction, ui.feed_from_option(interaction, feed))


@group.command(name="remove", description="Remove a Feed.")
@app_commands.describe(feed="The Feed to remove.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def remove_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    await ui.reply(
        interaction, _remove_question(found), view=ui.confirm_view(_remove_button(found))
    )


@group.command(name="pause", description="Stop checking a Feed until it is resumed.")
@app_commands.describe(feed="The Feed to pause.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def pause_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    paused = await _service(interaction).pause_feed(
        found.server_id, found.id, actor=ui.actor_of(interaction)
    )
    await ui.reply(
        interaction, f"Paused the Feed **{_name(paused)}**. It is not checked until it is resumed."
    )


@group.command(name="resume", description="Check a Paused feed again.")
@app_commands.describe(feed="The Feed to resume.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def resume_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    found = ui.feed_from_option(interaction, feed)
    if found.paused is None:
        raise ui.UserError("That Feed is not paused.")
    resumed = await _service(interaction).resume_feed(
        found.server_id, found.id, actor=ui.actor_of(interaction)
    )
    lines = [f"Resumed the Feed **{_name(resumed)}**. It is checked again from now on."]
    if found.paused is PauseReason.LOST_CHANNEL:
        lines.append(
            _access_warning(interaction, resumed.channel_id, embed=resumed.embed is not None)
        )
    elif _needs_tag_again(found.paused, resumed):
        lines.append(TAG_NEEDED_AGAIN)
    await ui.reply(interaction, "\n".join(filter(None, lines)))


@group.command(name="refresh", description="Check a Feed now, or every Feed if none is chosen.")
@app_commands.describe(feed="The Feed to refresh. Leave it out to refresh every Feed.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def refresh_command(interaction: discord.Interaction, feed: str | None = None) -> None:
    ui.require_manager(interaction)
    if feed is None:
        count, waiting = _service(interaction).make_due(ui.server_id_of(interaction))
        left_out = "Paused feeds are left out."
        if waiting:
            left_out = (
                f"Paused feeds are left out, and so {_is_are(waiting, 'Rate-limited feed')}: "
                "the site asked the bot to wait."
            )
        if count == 0:
            raise ui.UserError(f"This Server has no Feeds to refresh. {left_out}")
        await ui.reply(
            interaction,
            f"{_count(count, 'Feed')} will be refreshed, usually within a minute. {left_out}",
        )
        return
    found = ui.feed_from_option(interaction, feed)
    if found.paused is not None:
        raise ui.UserError(PAUSED_NOT_REFRESHED)
    await ui.defer(interaction)
    await ui.reply(interaction, await _refresh(interaction, found))


@group.command(name="test", description="Preview what a Feed would post for one of its Items.")
@app_commands.describe(feed="The Feed to test.")
@app_commands.autocomplete(feed=ui.feed_autocomplete)
async def test_command(interaction: discord.Interaction, feed: str) -> None:
    ui.require_manager(interaction)
    await _send_preview(interaction, ui.feed_from_option(interaction, feed))


@group.command(name="import", description="Add a Feed for every address in an OPML file.")
@app_commands.describe(
    file="An OPML file, at most 1 MB.",
    channel="The channel the Feeds post in. Default: this channel.",
)
async def import_command(
    interaction: discord.Interaction,
    file: discord.Attachment,
    channel: app_commands.Transform[Any, _ChannelOption] = None,
) -> None:
    ui.require_manager(interaction)
    if file.size > MAX_IMPORT_BYTES:
        raise ui.UserError("That file is too large. An OPML file can be at most 1 MB.")
    target = channel if channel is not None else getattr(interaction, "channel", None)
    if target is None:
        raise ui.UserError("Choose the channel the Feeds post in, using the channel option.")
    kind = _kind_of(interaction, target)
    await ui.defer(interaction)
    try:
        data = await file.read()
    except discord.HTTPException:
        raise ui.UserError("The bot could not download that file. Try again.") from None
    result = await _service(interaction).import_opml(
        ui.server_id_of(interaction), target.id, kind, data, actor=ui.actor_of(interaction)
    )
    warning = _access_warning(interaction, target.id)
    await ui.reply(interaction, _import_summary(result, target.id, warning))


@group.command(name="export", description="Get this Server's Feeds as an OPML file.")
async def export_command(interaction: discord.Interaction) -> None:
    ui.require_manager(interaction)
    data = _service(interaction).export_opml(ui.server_id_of(interaction))
    # ui.reply cannot attach a file, so this one reply is sent here: private, no pings.
    await interaction.response.send_message(
        content="Exported this Server's Feeds as an OPML file.",
        file=discord.File(io.BytesIO(data), filename=EXPORT_FILENAME),
        ephemeral=True,
        allowed_mentions=ui.NO_MENTIONS,
    )


def register(tree: app_commands.CommandTree) -> None:
    tree.add_command(group)
