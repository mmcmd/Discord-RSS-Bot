"""The Placeholder language used in every Template string.

`{{title}}` is replaced by a field of the Item, `{{summary||description}}` is a Fallback,
and `{{description:200}}` cuts the result to 200 characters. Everything else is literal text.
Pure: no I/O.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from typing import NamedTuple

from rssbot.models import Feed, Item

PLACEHOLDERS: tuple[str, ...] = (
    "title",
    "link",
    "description",
    "summary",
    "content",
    "author",
    "date",
    "categories",
    "image",
    "feed_title",
    "feed_link",
    "mentions",
)
ALIASES: dict[str, str] = {"url": "link"}
# The Placeholders whose value is a web address.
ADDRESS_PLACEHOLDERS: tuple[str, ...] = ("link", "image", "feed_link")

ELLIPSIS = "…"

# The body holds no braces, so in "{{{title}}}" the innermost pair is the Placeholder
# and an unclosed "{{" before a real Placeholder stays literal.
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")

_MAX_QUOTED = 60  # how much of the user's own text an error message repeats
_MAX_LIMIT_DIGITS = 18  # beyond this the limit is larger than any string


class TemplateError(ValueError):
    """A Placeholder that cannot be used. The message is one sentence for a Discord user."""


class Use(NamedTuple):
    """One Placeholder being replaced by a value."""

    name: str  # the name that supplied the value: in a Fallback, the first that is not empty
    start: int  # where the Placeholder starts in the template
    limited: bool  # whether it has its own length limit


def validate(template: str) -> None:
    """Raise TemplateError for the first Placeholder that cannot be used."""
    for match in _PLACEHOLDER.finditer(template):
        _parse(match.group(1))


def render(
    template: str,
    values: Mapping[str, str],
    *,
    transform: Callable[[str, Use], str] | None = None,
) -> str:
    """Replace every Placeholder. Never raises; an invalid Placeholder renders as nothing.

    `transform` is given each value, already cut to its own length limit, and returns what
    is put in the text.
    """

    def replace(match: re.Match[str]) -> str:
        try:
            names, limit = _parse(match.group(1))
        except TemplateError:
            return ""
        for name in names:
            value = _value(values, name)
            if value.strip():
                value = _cut(value, limit)
                if transform is None:
                    return value
                return transform(value, Use(name, match.start(), limit is not None))
        return ""

    # A function replacement is inserted verbatim: no escapes, no second scan.
    return _PLACEHOLDER.sub(replace, template)


def uses(template: str, name: str) -> bool:
    """Whether a valid Placeholder in the template refers to this name or one of its aliases."""
    wanted = _canonical(name)
    if wanted not in PLACEHOLDERS:
        return False
    for match in _PLACEHOLDER.finditer(template):
        try:
            names, _ = _parse(match.group(1))
        except TemplateError:
            continue
        if wanted in names:
            return True
    return False


def leading_names(template: str) -> tuple[str, ...]:
    """The names in the Placeholder the template starts with, or () if it starts with none."""
    match = _PLACEHOLDER.match(template)
    if match is None:
        return ()
    try:
        return _parse(match.group(1))[0]
    except TemplateError:
        return ()


def values_for(feed: Feed, item: Item) -> dict[str, str]:
    """The value of every Placeholder for one Item."""
    return {
        "title": item.title,
        "link": item.link,
        "description": item.description,
        "summary": item.summary,
        "content": item.content,
        "author": item.author,
        "date": "" if item.published is None else f"<t:{item.published}:f>",
        "categories": ", ".join(item.categories),
        "image": item.image,
        "feed_title": feed.source_title or feed.name,
        "feed_link": feed.source_link,
        "mentions": " ".join(f"<@&{role_id}>" for role_id in feed.mention_role_ids),
    }


def _canonical(name: str) -> str:
    name = name.strip().lower()
    return ALIASES.get(name, name)


def _parse(body: str) -> tuple[tuple[str, ...], int | None]:
    """Split the text between the braces into canonical names and an optional length limit."""
    expression, colon, limit_text = body.partition(":")
    names: list[str] = []
    for raw in expression.split("||"):
        name = _canonical(raw)
        if not name:
            raise TemplateError(f"{_quote(body)} has an empty Placeholder name.")
        if name not in PLACEHOLDERS:
            raise TemplateError(
                f'"{_shorten(raw.strip())}" in {_quote(body)} is not a Placeholder name; '
                f"the valid names are {', '.join(PLACEHOLDERS)}."
            )
        names.append(name)
    limit = None
    if colon:
        limit = _parse_limit(limit_text.strip())
        if limit is None:
            raise TemplateError(
                f'The length limit "{_shorten(limit_text.strip())}" in {_quote(body)} '
                "must be a whole number greater than 0."
            )
    return tuple(names), limit


def _parse_limit(text: str) -> int | None:
    if not (text.isascii() and text.isdigit()):
        return None
    digits = text.lstrip("0")
    if not digits:
        return None
    # int() refuses very long digit strings, and render must never raise.
    return int(digits) if len(digits) <= _MAX_LIMIT_DIGITS else sys.maxsize


def _value(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _cut(value: str, limit: int | None) -> str:
    if limit is None or len(value) <= limit:
        return value
    kept = value[: limit - 1].rstrip()
    if (len(kept) - len(kept.rstrip("\\"))) % 2:
        kept = kept[:-1]  # the cut went through an escaped character
    return kept + ELLIPSIS


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_QUOTED else text[: _MAX_QUOTED - 1] + ELLIPSIS


def _quote(body: str) -> str:
    return "{{" + _shorten(body) + "}}"
