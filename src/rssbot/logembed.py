"""What a Log entry looks like in a Server's Logs channel: embeds, and plain text as the fallback.

Pure functions: nothing here talks to Discord. Text that came from a member or a feed (names,
addresses, details) is escaped, so it cannot add formatting, links or mentions to a report
the bot posts in its own voice. The sender still sends with no allowed mentions.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Sequence

import discord

from .models import Actor, LogEntry, LogKind

GREEN = 0x2ECC71
GREY = 0x95A5A6
RED = 0xE74C3C
BLUE = 0x3498DB
AMBER = 0xF1C40F

# Discord's limits, and the room this module leaves under them.
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_EMBEDS = 10  # per message
MAX_EMBED_CHARS = 6000  # per message, over all its embeds
MAX_TEXT = 2000  # a plain message
MAX_AUTHOR = 256
MAX_LINE = 1000  # one line of a report; only an absurd name, address or detail is ever cut

_TITLES: dict[LogKind, str] = {
    LogKind.FEED_ADDED: "Feed added",
    LogKind.FEED_REMOVED: "Feed removed",
    LogKind.FEED_PAUSED: "Feed paused",
    LogKind.FEED_RESUMED: "Feed resumed",
    LogKind.FEED_EDITED: "Feed edited",
    LogKind.TEMPLATE_CHANGED: "Template changed",
    LogKind.FILTER_CHANGED: "Filters changed",
    LogKind.POST_AS_CHANGED: "Post as changed",
    LogKind.MENTIONS_CHANGED: "Mentions changed",
    LogKind.FORUM_TAGS_CHANGED: "Forum tags changed",
    LogKind.GRANT_GIVEN: "Access given",
    LogKind.GRANT_TAKEN: "Access taken away",
    LogKind.LOGS_CHANNEL_CHANGED: "Logs channel changed",
    LogKind.FEED_AUTO_PAUSED: "Feed paused by the bot",
    LogKind.FEED_BROKEN: "Broken feed",
    LogKind.FEED_WORKING_AGAIN: "Feed working again",
}

# Several Log entries of one kind reported together. Only an OPML import adds many at once.
_GROUP_TITLES: dict[LogKind, str] = {
    LogKind.FEED_ADDED: "{count} Feeds imported",
    LogKind.FEED_REMOVED: "{count} Feeds removed",
    LogKind.FEED_PAUSED: "{count} Feeds paused",
    LogKind.FEED_RESUMED: "{count} Feeds resumed",
    LogKind.FEED_AUTO_PAUSED: "{count} Feeds paused by the bot",
}

_COLOURS: dict[LogKind, int] = {
    LogKind.FEED_ADDED: GREEN,
    LogKind.FEED_RESUMED: GREEN,
    LogKind.FEED_WORKING_AGAIN: GREEN,
    LogKind.FEED_PAUSED: GREY,
    LogKind.FEED_REMOVED: RED,
    LogKind.FEED_BROKEN: RED,
    LogKind.FEED_AUTO_PAUSED: AMBER,
}

_GRANT_KINDS = frozenset({LogKind.GRANT_GIVEN, LogKind.GRANT_TAKEN})

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#<:-])")
_ESCAPED_MENTION = re.compile(r"\\<(@&?|\\#)(\d+)\\>")
_NOT_IN_A_LINK = re.compile(r"[\s<>`]")
_CHANNEL = re.compile(r"<#\d+>")


def title_of(kind: LogKind) -> str:
    """What a Log entry of this kind is called, e.g. "Feed paused"."""
    return _TITLES[kind]


def colour_of(kind: LogKind) -> int:
    return _COLOURS.get(kind, BLUE)


def safe(text: str, *, keep_mentions: bool = False) -> str:
    """Text from a member or a feed, as one line that shows exactly as it was typed.

    Formatting, links, headings and mentions in it are all defused. `keep_mentions` lets
    whole `<@id>`, `<@&id>` and `<#id>` references through, for text the bot wrote itself.
    """
    text = _MARKDOWN.sub(r"\\\1", " ".join(text.split()))
    if keep_mentions:
        text = _ESCAPED_MENTION.sub(lambda m: f"<{m[1].lstrip('\\')}{m[2]}>", text)
    # @everyone and @here: a zero-width space after the @, as discord.py does it.
    return text.replace("@everyone", "@​everyone").replace("@here", "@​here")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _link(url: str) -> str:
    # In <>, so that Discord shows the address without a preview. Nothing in it can end the <>.
    return "<" + _NOT_IN_A_LINK.sub(lambda m: f"%{ord(m[0]):02X}", _cut(url, 500)) + ">"


def _where(entry: LogEntry) -> str:
    """The Feed and its channel, e.g. "**BBC News** in <#123>"; "" when the entry has neither."""
    name = f"**{safe(_cut(entry.feed_name, 300))}**" if entry.feed_name else ""
    channel = f"<#{entry.channel_id}>" if entry.channel_id is not None else ""
    return f"{name} in {channel}" if name and channel else name or channel


def _detail(entry: LogEntry) -> str:
    # A Grant's target is a role or member reference the bot wrote.
    return safe(_cut(entry.detail, 500), keep_mentions=entry.kind in _GRANT_KINDS)


def _value(text: str) -> str:
    """One side of a Change. A value that is nothing but a channel reference, as the bot
    writes a changed channel, stays one; anything else is text and is defused, so a name
    that holds a reference or a mention shows as it was typed."""
    if _CHANNEL.fullmatch(text):
        return text
    return safe(_cut(text, 300)) or "*nothing*"


def _body(entry: LogEntry) -> list[str]:
    """What one Log entry says, line by line, without who did it."""
    lines = [_where(entry)]
    if entry.feed_url:
        lines.append(_link(entry.feed_url))
    for change in entry.changes:
        lines.append(f"**{safe(change.label)}**: {_value(change.before)} → {_value(change.after)}")
    if entry.detail:
        lines.append(_detail(entry))
    return [_cut(line, MAX_LINE) for line in lines if line]


def _group_line(entry: LogEntry) -> str:
    return _cut(f"- {_where(entry) or _detail(entry) or title_of(entry.kind)}", MAX_LINE)


def _by(actor: Actor) -> str:
    """The line that names a member, as a mention and an id that survives their leaving."""
    return "" if actor.id is None else f"By <@{actor.id}> `{actor.id}`"


def _reports(entries: Sequence[LogEntry]) -> list[tuple[str, int, int, list[str]]]:
    """Split entries into reports: (title, colour, time, lines).

    Entries that share a kind are one report with a line per entry; anything else is a
    report per entry.
    """
    if len(entries) > 1 and len({entry.kind for entry in entries}) == 1:
        kind = entries[0].kind
        pattern = _GROUP_TITLES.get(kind, title_of(kind) + " ({count})")
        title = pattern.format(count=len(entries))
        lines = [_group_line(entry) for entry in entries]
        return [(title, colour_of(kind), max(entry.at for entry in entries), lines)]
    return [(title_of(e.kind), colour_of(e.kind), e.at, _body(e)) for e in entries]


def _pack(lines: Iterable[str], limit: int, first_limit: int | None = None) -> list[str]:
    """Join lines into as few blocks of at most `limit` characters as it takes, in order.

    No line is dropped or cut; the first block may be given a smaller limit.
    """
    blocks: list[str] = []
    block = ""
    room = limit if first_limit is None else first_limit
    for line in lines:
        if block and len(block) + 1 + len(line) > room:
            blocks.append(block)
            block, room = "", limit
        block = f"{block}\n{line}" if block else line
    if block:
        blocks.append(block)
    return blocks


def _author(embed: discord.Embed, actor: Actor) -> None:
    if actor.name:
        embed.set_author(name=_cut(actor.name, MAX_AUTHOR), icon_url=actor.avatar_url or None)


def build_messages(entries: Sequence[LogEntry], actor: Actor) -> list[list[discord.Embed]]:
    """The report of these Log entries for a Logs channel: the embeds of each message to send.

    Usually one message with one embed. Entries of one kind passed together (an OPML
    import) are one report that names every Feed; when that is too long for one embed it
    continues in more embeds and more messages, each within Discord's limits, with
    nothing left out. For the bot as `actor`, pass its own name and picture in an Actor
    whose id is None: it gets the author line and no "By" line.
    """
    embeds: list[discord.Embed] = []
    for title, colour, at, lines in _reports(entries):
        by = _by(actor)
        blocks = _pack([*lines, *([by] if by else [])], MAX_DESCRIPTION) or [""]
        when = datetime.datetime.fromtimestamp(at, datetime.UTC)
        for number, block in enumerate(blocks):
            heading = title if number == 0 else f"{title} (continued)"
            embed = discord.Embed(
                title=_cut(heading, MAX_TITLE),
                description=block or None,
                colour=colour,
                timestamp=when,
            )
            _author(embed, actor)
            embeds.append(embed)

    messages: list[list[discord.Embed]] = []
    size = 0
    for embed in embeds:
        if not messages or len(messages[-1]) == MAX_EMBEDS or size + len(embed) > MAX_EMBED_CHARS:
            messages.append([])
            size = 0
        messages[-1].append(embed)
        size += len(embed)
    return messages


def render_text(entries: Sequence[LogEntry], actor: Actor) -> list[str]:
    """The same report as plain messages, for a Logs channel where the bot may not embed.

    Each string is one message of at most 2000 characters; nothing is left out.
    """
    who = f"**{safe(_cut(actor.name, 100))}**" if actor.name else ""
    if actor.id is not None:
        who = f"{who} (<@{actor.id}> `{actor.id}`)" if who else f"<@{actor.id}> `{actor.id}`"
    messages: list[str] = []
    for title, _, at, lines in _reports(entries):
        heading = f"**{title}**" + (f" by {who}" if who else "") + f" <t:{at}:f>"
        # MAX_LINE keeps every line under the limit, so nothing has to be cut here.
        messages.extend(_pack([heading, *lines], MAX_TEXT))
    return messages


def build_note(text: str, actor: Actor) -> discord.Embed:
    """A note in the bot's own words (already safe to show), as an amber embed."""
    embed = discord.Embed(description=_cut(text, MAX_DESCRIPTION), colour=AMBER)
    _author(embed, actor)
    return embed
