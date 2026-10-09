"""Turn one Item into the message to post, cut to Discord's limits.

`render_item` applies the Feed's own Template; `render_default` is the plain retry used when
Discord rejects the customised message. Both always return a message Discord accepts
structurally. Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from rssbot.models import (
    DEFAULT_TEXT_TEMPLATE,
    MAX_BUTTONS,
    MAX_EMBED_FIELDS,
    MAX_FORUM_TAGS,
    ButtonSpec,
    ChannelKind,
    EmbedSpec,
    Feed,
    FieldSpec,
    Item,
    OutgoingMessage,
    PostAs,
)
from rssbot.template import ADDRESS_PLACEHOLDERS, ELLIPSIS, Use, render, uses, values_for

MAX_CONTENT = 2000
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FOOTER = 2048
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_EMBED_TOTAL = 6000
MAX_EMBED_URL = 2048
MAX_BUTTON_LABEL = 80
MAX_BUTTON_URL = 512
MAX_USERNAME = 80
MAX_THREAD_TITLE = 100
MAX_COLOUR = 0xFFFFFF

LAST_RESORT_TEXT = "New item"

ZWSP = "​"
FENCE = "```"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_EVERYONE = re.compile(r"@(?=everyone|here)", re.IGNORECASE)
_REFUSED_IN_NAMES = re.compile(r"(?<=d)(?=iscord)|(?<=c)(?=lyde)", re.IGNORECASE)
_REFUSED_NAMES = ("everyone", "here")
# What Discord reads as formatting. A web address is left alone: a backslash would break it.
_FORMATTING = re.compile(r"(https?://\S+)|([\\*_~`|>#\[\]<])")


def render_item(feed: Feed, item: Item) -> OutgoingMessage:
    """The Feed's own Template applied to the Item."""
    return _build(feed, item, customised=True)


def render_default(feed: Feed, item: Item) -> OutgoingMessage:
    """The Item with the default text, no Embed and no Buttons."""
    return _build(feed, item, customised=False)


def hidden_buttons(feed: Feed, item: Item) -> tuple[int, ...]:
    """The positions, from 1, of the Feed's Buttons that are left out for this Item."""
    plain = _plain_values(values_for(feed, item), item)
    return tuple(
        number
        for number, spec in enumerate(feed.buttons[:MAX_BUTTONS], start=1)
        if not _buttons((spec,), plain)
    )


def _build(feed: Feed, item: Item, *, customised: bool) -> OutgoingMessage:
    raw = values_for(feed, item)
    # Where Discord formats text, the title is shown as the source wrote it. Where it does
    # not (a Forum post's title, a Button, a footer, an address), a backslash would show.
    values = {**raw, "title": _literal(raw["title"])}
    plain = _plain_values(raw, item)

    thread_title: str | None = None
    tag_ids: tuple[int, ...] = ()
    cover: str | None = None
    if feed.channel_kind == ChannelKind.FORUM:
        thread_title = _thread_title(feed, item, plain)
        tag_ids = tuple(feed.forum_tag_ids[:MAX_FORUM_TAGS])
        if feed.forum_cover:
            cover = _url(item.image, MAX_EMBED_URL) or None

    embed: EmbedSpec | None = None
    buttons: tuple[ButtonSpec, ...] = ()
    if customised:
        if feed.embed is not None:
            embed = _embed(feed.embed, values, plain)
            if cover is not None and embed.image == cover:
                embed = replace(embed, image="")  # the Cover image already shows it
            if _is_empty(embed):
                embed = None
        buttons = _buttons(feed.buttons, plain)

    template = feed.text_template if customised else DEFAULT_TEXT_TEMPLATE
    username, avatar_url = _post_as(feed)
    return OutgoingMessage(
        content=_content(feed, values, template, has_embed=embed is not None),
        embed=embed,
        buttons=buttons,
        mention_role_ids=tuple(feed.mention_role_ids),
        username=username,
        avatar_url=avatar_url,
        thread_title=thread_title,
        tag_ids=tag_ids,
        cover_image_url=cover,
        published=item.published if embed is not None else None,
    )


def _plain_values(values: Mapping[str, str], item: Item) -> dict[str, str]:
    """The values for the places where Discord shows a timestamp or a mention as raw text."""
    return {**values, "date": _plain_date(item.published), "mentions": ""}


def _plain_date(published: int | None) -> str:
    if published is None:
        return ""
    try:
        when = datetime.fromtimestamp(published, UTC)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{when.day} {_MONTHS[when.month - 1]} {when.year} {when:%H:%M} UTC"


def _content(feed: Feed, values: Mapping[str, str], template: str, *, has_embed: bool) -> str:
    mentions = values.get("mentions", "")
    reserve = len(mentions) + 1 if mentions else 0
    places_mentions = uses(template, "mentions")

    text = _text(template, values, 0 if places_mentions else reserve)
    # Mentions alone are not something to show.
    bare = render(template, {**values, "mentions": ""}).strip() if places_mentions else text
    if not bare and not has_embed:
        text = _default_text(values, reserve) or feed.name.strip() or LAST_RESORT_TEXT
        places_mentions = False
    if mentions and not places_mentions:
        text = f"{mentions} {text}".strip()
    return _cut_code(_defuse(text), MAX_CONTENT)


def _text(template: str, values: Mapping[str, str], reserve: int) -> str:
    if template == DEFAULT_TEXT_TEMPLATE:
        return _default_text(values, reserve)
    return _fitted(template, values, MAX_CONTENT - reserve)


def _fitted(template: str, values: Mapping[str, str], limit: int) -> str:
    """The rendered text, with its longest values shortened if it is over the limit.

    Shortening the values rather than the end of the text keeps what the Template puts
    after them, such as the link and the mentions. A Placeholder with its own length limit
    keeps it, and mentions are never shortened. The result can still be over the limit
    when the rest of the Template is too long by itself.
    """
    sizes: dict[int, int] = {}

    def fixed(use: Use) -> bool:
        return use.limited or use.name == "mentions"

    def measure(value: str, use: Use) -> str:
        value = _defuse(value)
        if not fixed(use):
            sizes[use.start] = len(value)
        return value

    text = _defuse(render(template, values, transform=measure).strip())
    over = len(text) - limit
    if over <= 0 or not sizes:
        return text

    # Share the room among the values, taking most from the longest.
    room = sum(sizes.values()) - over
    caps: dict[int, int] = {}
    order = sorted(sizes, key=lambda start: sizes[start])
    for done, start in enumerate(order):
        caps[start] = min(sizes[start], max(room, 0) // (len(order) - done))
        room -= caps[start]

    def shorten(value: str, use: Use) -> str:
        value = _defuse(value)
        return value if fixed(use) else _cut_code(value, caps[use.start])

    return _defuse(render(template, values, transform=shorten).strip())


def _default_text(values: Mapping[str, str], reserve: int) -> str:
    """The default text, or "" for an Item with no title and no link."""
    title = _defuse(values.get("title", "").strip())
    link = values.get("link", "").strip()
    if not title:
        return link  # an empty bold title would show as stray asterisks
    # Shorten the title rather than lose the link off the end.
    room = MAX_CONTENT - reserve - len(render(DEFAULT_TEXT_TEMPLATE, {"link": link}))
    if room >= MAX_THREAD_TITLE:
        title = _cut(title, room)
    return render(DEFAULT_TEXT_TEMPLATE, {"title": title, "link": link}).strip()


def _embed(spec: EmbedSpec, values: Mapping[str, str], plain: Mapping[str, str]) -> EmbedSpec:
    """`plain` holds the values for the title, the footer and the Field names."""
    # The title and the Field names are formatted by Discord; the footer is not.
    headings = {**plain, "title": values.get("title", "")}
    title = _plain(spec.title, headings, MAX_EMBED_TITLE)
    description = _cut_code(
        _fitted(spec.description, values, MAX_EMBED_DESCRIPTION), MAX_EMBED_DESCRIPTION
    )
    footer = _plain(spec.footer, plain, MAX_EMBED_FOOTER)

    fields: list[FieldSpec] = []
    for field in spec.fields:
        name = _plain(field.name, headings, MAX_FIELD_NAME)
        value = _plain(field.value, values, MAX_FIELD_VALUE)
        if name and value:
            fields.append(FieldSpec(name=name, value=value, inline=bool(field.inline)))
    del fields[MAX_EMBED_FIELDS:]

    def total() -> int:
        return (
            len(title)
            + len(description)
            + len(footer)
            + sum(len(f.name) + len(f.value) for f in fields)
        )

    over = total() - MAX_EMBED_TOTAL
    if over > 0:
        description = _cut_code(description, len(description) - over)
    while fields and total() > MAX_EMBED_TOTAL:
        fields.pop()

    colour = spec.colour
    if not (isinstance(colour, int) and 0 <= colour <= MAX_COLOUR):
        colour = None
    return EmbedSpec(
        title=title,
        description=description,
        url=_address(spec.url, plain, MAX_EMBED_URL),
        image=_address(spec.image, plain, MAX_EMBED_URL),
        footer=footer,
        colour=colour,
        fields=tuple(fields),
        timestamp=spec.timestamp,
    )


def _is_empty(embed: EmbedSpec) -> bool:
    return not (
        embed.title or embed.description or embed.image or embed.footer or embed.fields
    )


def _buttons(specs: tuple[ButtonSpec, ...], values: Mapping[str, str]) -> tuple[ButtonSpec, ...]:
    buttons: list[ButtonSpec] = []
    for spec in specs:
        label = _plain(spec.label, values, MAX_BUTTON_LABEL)
        url = _address(spec.url, values, MAX_BUTTON_URL)
        if label and url:
            buttons.append(ButtonSpec(label=label, url=url))
    return tuple(buttons[:MAX_BUTTONS])


def _post_as(feed: Feed) -> tuple[str | None, str | None]:
    if feed.post_as == PostAs.SITE:
        name = feed.site_name.strip() or feed.source_title.strip() or feed.name
        avatar = feed.site_icon
    elif feed.post_as == PostAs.CUSTOM:
        name = feed.custom_name.strip() or feed.name
        avatar = feed.custom_avatar
    else:
        return None, None
    username = _username(name) or _username(feed.name) or None
    return username, _url(avatar, MAX_EMBED_URL) or None


def _username(name: str) -> str:
    name = " ".join(name.split())
    name = _REFUSED_IN_NAMES.sub(ZWSP, name)
    if name.lower() in _REFUSED_NAMES:
        name = name[0] + ZWSP + name[1:]
    return _cut(name, MAX_USERNAME)


def _thread_title(feed: Feed, item: Item, values: Mapping[str, str]) -> str:
    for candidate in (render(feed.forum_title_template, values), item.title, feed.name):
        title = _cut(_defuse(" ".join(candidate.split())), MAX_THREAD_TITLE)
        if title:
            return title
    return LAST_RESORT_TEXT


def _plain(template: str, values: Mapping[str, str], limit: int) -> str:
    """A rendered piece of text that cannot ping everyone, cut to its limit."""
    return _cut(_defuse(render(template, values).strip()), limit)


def _literal(text: str) -> str:
    """The text with Discord's formatting characters escaped, so that they show as written."""
    text = _FORMATTING.sub(lambda m: m.group(1) or "\\" + m.group(2), text)
    return "\\" + text if text.startswith("- ") else text  # a list bullet


def _defuse(text: str) -> str:
    """Break @everyone and @here. Applying it twice changes nothing more."""
    return _EVERYONE.sub("@" + ZWSP, text)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return _whole_escapes(text[: limit - 1].rstrip()) + ELLIPSIS


def _whole_escapes(text: str) -> str:
    """The text without a backslash left at its end by a cut through an escaped character."""
    backslashes = len(text) - len(text.rstrip("\\"))
    return text[:-1] if backslashes % 2 else text


def _cut_code(text: str, limit: int) -> str:
    """_cut, closing a code block that the cut would leave open."""
    cut = _cut(text, limit)
    if cut == text or cut.count(FENCE) % 2 == 0:
        return cut
    closing = "\n" + FENCE
    kept = text[: max(limit - len(ELLIPSIS) - len(closing), 0)].rstrip().rstrip("`")
    if kept.count(FENCE) % 2 == 0:
        return kept + ELLIPSIS  # the block's opening went with the cut
    return kept + ELLIPSIS + closing


def _address(template: str, values: Mapping[str, str], limit: int) -> str:
    """The rendered web address if Discord will take it, else ""."""

    def encode(value: str, use: Use) -> str:
        before = template[: use.start]
        if not before.strip():
            return value  # the value is the address, or its beginning
        if use.name in ADDRESS_PLACEHOLDERS and "?" not in before:
            return value  # an address in the path of another, as web archives take it
        # Anything else is text put inside the address, which must not break it.
        return quote(value, safe="", errors="replace")

    return _url(render(template, values, transform=encode), limit)


def _url(text: str, limit: int) -> str:
    """The URL if Discord will take it, else ""."""
    url = text.strip()
    if not url or len(url) > limit or any(c.isspace() or not c.isprintable() for c in url):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return url
