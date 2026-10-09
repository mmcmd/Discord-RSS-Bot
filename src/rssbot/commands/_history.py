"""How Log entries read as a page of lines. Shared by /feed history and /log.

A page holds PAGE_SIZE entries, one line each: when, who, what, then what changed. The
parts that come from members and feeds are escaped and cut, and the longest lines give way
first, so a page always fits in one message whatever the entries say.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

import discord

from ..logembed import safe
from ..models import LogEntry, LogKind
from . import _ui as ui

PAGE_SIZE = ui.PAGE_SIZE
SEPARATOR = " · "
FEED_NAME_CHARS = 25  # of a Feed's name, before it is escaped
LABEL_CHARS = 30
VALUE_CHARS = 80  # one side of a change
DETAIL_CHARS = 1000
MIN_TAIL = 12  # a line gives up its changes rather than show fewer characters than this

# What a Log entry says was done; {feed} is the Feed, as feed_words writes it.
VERBS: dict[LogKind, str] = {
    LogKind.FEED_ADDED: "added {feed}",
    LogKind.FEED_REMOVED: "removed {feed}",
    LogKind.FEED_PAUSED: "paused {feed}",
    LogKind.FEED_RESUMED: "resumed {feed}",
    LogKind.FEED_EDITED: "edited {feed}",
    LogKind.TEMPLATE_CHANGED: "changed the Template of {feed}",
    LogKind.FILTER_CHANGED: "changed the Filters of {feed}",
    LogKind.POST_AS_CHANGED: "changed Post as of {feed}",
    LogKind.MENTIONS_CHANGED: "changed the mentions of {feed}",
    LogKind.FORUM_TAGS_CHANGED: "changed the forum tags of {feed}",
    LogKind.GRANT_GIVEN: "gave access",
    LogKind.GRANT_TAKEN: "took away access",
    LogKind.LOGS_CHANNEL_CHANGED: "changed the Logs channel",
    LogKind.FEED_AUTO_PAUSED: "paused {feed}",
    LogKind.FEED_BROKEN: "reported {feed} as a Broken feed",
    LogKind.FEED_WORKING_AGAIN: "reported {feed} as working again",
}

# A Grant's detail is the role or member it was given to, written by the bot.
_GRANT_KINDS = frozenset({LogKind.GRANT_GIVEN, LogKind.GRANT_TAKEN})
_CHANNEL = re.compile(r"<#\d+>")


def page_of(total: int, wanted: int) -> tuple[int, int]:
    """(page, pages) for `total` entries: the page asked for, clamped into range."""
    pages = max(1, math.ceil(total / PAGE_SIZE))
    return min(max(wanted, 0), pages - 1), pages


def entries_words(total: int) -> str:
    return f"{total} Log {'entry' if total == 1 else 'entries'}"


def feed_words(entry: LogEntry, *, show_feed: bool) -> str:
    """The Feed an entry is about: "this Feed", or its saved name and channel."""
    if not show_feed:
        return "this Feed"
    name = safe(ui.cut(entry.feed_name, FEED_NAME_CHARS))
    named = f"**{name}**" if name else ""
    channel = ui.channel_mention(entry.channel_id) if entry.channel_id is not None else ""
    return f"{named} in {channel}" if named and channel else named or channel or "a Feed"


def _value(text: str) -> str:
    # A channel the bot wrote as a reference stays one; anything else is text, defused.
    if _CHANNEL.fullmatch(text):
        return text
    return safe(ui.cut(" ".join(text.split()), VALUE_CHARS)) or "*nothing*"


def _changes(entry: LogEntry) -> str:
    """What the entry changed and its detail, as one string; "" when it has neither."""
    parts = [
        f"{safe(ui.cut(change.label, LABEL_CHARS))}: "
        f"{_value(change.before)} → {_value(change.after)}"
        for change in entry.changes
    ]
    if entry.detail.strip():
        detail = ui.cut(" ".join(entry.detail.split()), DETAIL_CHARS)
        parts.append(safe(detail, keep_mentions=entry.kind in _GRANT_KINDS))
    return SEPARATOR.join(parts)


def _trim(text: str, limit: int) -> str:
    """Text of escaped characters cut to `limit` without leaving half an escape at the end."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if (len(cut) - len(cut.rstrip("\\"))) % 2:
        cut = cut[:-1]
    return cut.rstrip() + "…"


def _fit(lines: Sequence[tuple[str, str]], room: int) -> list[str]:
    """(head, changes) pairs as lines that together take at most `room` characters.

    The shortest lines are settled first and the room they leave is shared by the rest, so
    only the longest lines lose their tail, never their head.
    """
    fitted = [""] * len(lines)
    left = room - (len(lines) - 1)  # for the line breaks
    order = sorted(range(len(lines)), key=lambda i: len(lines[i][0]) + len(lines[i][1]))
    for turn, index in enumerate(order):
        head, tail = lines[index]
        share = left // (len(lines) - turn)
        text = head + (SEPARATOR + tail if tail else "")
        if len(text) > share:
            space = share - len(head) - len(SEPARATOR)
            text = head + SEPARATOR + _trim(tail, space) if space >= MIN_TAIL else head
        fitted[index] = text
        left -= len(text)
    return fitted


async def render(
    interaction: discord.Interaction,
    entries: Sequence[LogEntry],
    *,
    header: str,
    footer: str,
    show_feed: bool,
) -> str:
    """The message for one page: `header`, a line per entry (newest first as given), `footer`.

    `show_feed` names each entry's Feed; /feed history leaves it out. Looks up the members
    of these entries only, see ui.actors_of.
    """
    shown = await ui.actors_of(interaction, entries)
    lines = []
    for entry in entries:
        verb = VERBS[entry.kind].format(feed=feed_words(entry, show_feed=show_feed))
        who = ui.actor_words(entry, shown)
        lines.append((f"<t:{entry.at}:f> {who} {verb}", _changes(entry)))
    room = ui.MESSAGE_LIMIT - len(header) - len(footer) - 2
    return "\n".join(part for part in (header, *_fit(lines, room), footer) if part)
