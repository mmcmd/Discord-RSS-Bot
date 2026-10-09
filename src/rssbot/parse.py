"""Feed bytes to a ParsedFeed, using feedparser.

Feeds in the wild are often broken, so parsing is forgiving: anything that still yields a
feed title or an entry is accepted, and one entry that cannot be read never loses the others.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import io
import re
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.parsers import expat

import feedparser
from feedparser.encodings import convert_to_utf8
from feedparser.sanitizer import replace_doctype

from .html2md import first_image, to_markdown, to_text
from .models import Item, ParsedFeed

MAX_TEXT_CHARS = 20_000  # per text field, after conversion
MAX_CATEGORIES = 50

_KEY_CHARS = 32
_HTML_SNIFF_BYTES = 2048
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp")

_NOT_A_FEED = "That address did not return an RSS or Atom feed."
_WEB_PAGE = "That address returned a web page, not an RSS or Atom feed."
_EMPTY = "That address returned an empty response instead of a feed."

type _Entry = Mapping[str, Any]


class ParseError(Exception):
    """The body is not a feed. The message is one plain sentence for a Discord user."""


def parse_feed(body: bytes, url: str = "", content_type: str = "") -> ParsedFeed:
    """Parse a fetched feed. `url` is the Feed's address, used to resolve relative links.

    `content_type` is the Content-Type header, whose charset decides the text encoding when the
    feed does not declare one.
    """
    try:
        return _parse(body, url, content_type)
    except ParseError:
        raise
    except Exception as error:
        raise ParseError(_NOT_A_FEED) from error


MAX_ITEMS = 500  # per listing; a source listing more is cut to the newest ones
_MAX_TAG_ATTRIBUTES = 1_000  # measured: 8 000 take 0.8 s to parse, and each doubling costs 4x
_MAX_DEPTH = 10_000  # measured: about 430 bytes of parser memory per level
_TOO_STRANGE = "That feed is built in a way this bot refuses to read."
# A start or end tag as the XML parser reads it: a ">" inside a quoted value does not end it.
# Possessive, so that a body built to make a scanner back up is still read in one pass.
_TAG = re.compile(rb"""<[^>=]*+(?:=[ \t\r\n]*+(?:"[^"]*+"|'[^']*+'|(?!["']))[^>=]*+)*+>""")


class _RootReached(Exception):
    """The root element has started, so the part that can declare entities is over."""


def _refuse_entity(*declaration: object) -> None:
    raise ParseError(_TOO_STRANGE)


def _stop_at_root(*element: object) -> None:
    raise _RootReached


def _refuse_entities(data: bytes) -> None:
    """Refuse a document whose DOCTYPE declares an entity or attribute defaults, as the XML
    parser reads it."""
    parser = expat.ParserCreate()
    parser.EntityDeclHandler = _refuse_entity
    # Attribute defaults are given to every such tag: thousands of them parse like thousands
    # of attributes, in quadratic time.
    parser.AttlistDeclHandler = _refuse_entity
    parser.StartElementHandler = _stop_at_root
    # Text that only looks like a declaration, e.g. quoted in an Item, is not one.
    with contextlib.suppress(_RootReached, expat.ExpatError):
        parser.Parse(data, True)


def _refuse_hostile(body: bytes, headers: dict[str, str]) -> None:
    """Refuse the few shapes that make the XML parser use huge time or memory.

    Entity declarations expand to a hundred times their size, one tag with thousands of
    attributes parses in quadratic time, and very deep nesting costs memory per level. No
    real feed needs any of them. Checked before the parser sees the body, which `headers` are
    the response headers of.
    """
    # feedparser's own first step, with its own headers, so that the checks read what the
    # parser will read: in UTF-16, or in a charset only the Content-Type names, nothing below
    # would recognise a tag.
    data = convert_to_utf8(headers, body, {})
    # feedparser rewrites the DOCTYPE before parsing and keeps the entities it thinks safe.
    # Its rewrite can be steered, so the document is refused before and after it.
    _refuse_entities(data)
    _, rewritten, kept = replace_doctype(data)
    if kept:
        raise ParseError(_TOO_STRANGE)
    _refuse_entities(rewritten)

    # Unclosed tags count as nesting, as they do for the parser: it keeps something for
    # every tag that is still open, even an HTML <br> written raw inside a description.
    depth = 0
    loose = False  # a quote never closed: only the loose parser reads this, and ends a tag at ">"
    at = data.find(b"<")
    while at != -1:
        if data.startswith(b"<!--", at):
            end = data.find(b"-->", at + 4)
        elif data.startswith(b"<![CDATA[", at):
            end = data.find(b"]]>", at + 9)
        elif data.startswith((b"<!", b"<?"), at):
            end = data.find(b">", at + 1)
        else:
            tag = None if loose else _TAG.match(data, at)
            if tag is None:
                # Not well-formed, so the XML parser gives up and the loose one takes over.
                # It ends every tag at the first ">", and so does the rest of this scan.
                loose = True
                end = data.find(b">", at + 1)
                if end == -1:
                    return  # a cut-off body
            else:
                end = tag.end() - 1
            # Counted by "=", so that one long value (a data: URI) is not mistaken for many.
            if data.count(b"=", at, end) > _MAX_TAG_ATTRIBUTES:
                raise ParseError(_TOO_STRANGE)
            if data[at + 1 : at + 2] == b"/":
                depth = max(depth - 1, 0)  # the parser cannot go below the root either
            elif data[end - 1 : end] != b"/":
                depth += 1
                if depth > _MAX_DEPTH:
                    raise ParseError(_TOO_STRANGE)
        if end == -1:
            return
        at = data.find(b"<", end + 1)


def _newest(entries: list[Any]) -> list[Any]:
    """At most MAX_ITEMS entries: the newest ones, in the order the source listed them.

    An entry without a date takes the date of the entry listed before it (at the top: after
    it). A listing with no dates at all is cut to its first entries: usually the newest.
    """
    if len(entries) <= MAX_ITEMS:
        return entries
    dates = [_published(entry) if isinstance(entry, Mapping) else None for entry in entries]
    if not any(dates):
        return entries[:MAX_ITEMS]
    for index in range(1, len(dates)):
        if dates[index] is None:
            dates[index] = dates[index - 1]
    for index in range(len(dates) - 2, -1, -1):
        if dates[index] is None:
            dates[index] = dates[index + 1]
    newest = sorted(range(len(entries)), key=lambda index: (-(dates[index] or 0), index))
    return [entries[index] for index in sorted(newest[:MAX_ITEMS])]


def _parse(body: bytes, url: str, content_type: str = "") -> ParsedFeed:
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    if not isinstance(body, bytes | bytearray | memoryview):
        raise ParseError(_NOT_A_FEED)
    body = bytes(body)
    if not body.strip():
        raise ParseError(_EMPTY)
    headers = (
        {"content-type": content_type} if isinstance(content_type, str) and content_type else {}
    )
    _refuse_hostile(body, headers)
    base = url if isinstance(url, str) else ""

    # A stream, because feedparser would try a bare string as a URL or a file name.
    # It is given no base URL, so ids and links come back exactly as the source wrote them:
    # an Item's key must not change when the Feed's address does. html2md does the cleaning.
    result = feedparser.parse(
        io.BytesIO(body),
        response_headers=headers,
        sanitize_html=False,
        resolve_relative_uris=False,
    )

    feed = result.get("feed")
    if not isinstance(feed, Mapping):
        feed = {}
    entries = result.get("entries")
    if not isinstance(entries, list):
        entries = []
    entries = _newest(entries)

    title = _cap(to_text(_markup(feed, "title")))
    recognised = bool(result.get("version"))  # feedparser saw an RSS, Atom or RDF root element
    if not recognised and _looks_like_html(body):
        raise ParseError(_WEB_PAGE)

    items: list[Item] = []
    seen: set[str] = set()
    for entry in entries:
        try:
            item = _build_item(entry, base)
        except Exception:
            continue  # one unreadable entry must not lose the rest
        if item is None or item.key in seen:
            continue
        seen.add(item.key)
        items.append(item)

    # feedparser's "bozo" flag is ignored on purpose: a broken feed that still yields
    # something is worth having. Only a body with nothing feed-like in it is refused.
    if not recognised and not title and not items:
        raise ParseError(_NOT_A_FEED)

    return ParsedFeed(
        title=title,
        link=_resolve(feed.get("link"), base),
        image=_feed_image(feed, base),
        items=tuple(items),
    )


def _build_item(entry: object, base: str) -> Item | None:
    if not isinstance(entry, Mapping):
        return None
    title_html = _markup(entry, "title")
    summary_html = _markup(entry, "summary")
    content_html, content_base = _content(entry, base)
    summary_base = _detail_base(entry.get("summary_detail"), base)
    key_text = summary_html or content_html  # what the last-resort key has always hashed
    if not isinstance(entry.get("summary_detail"), Mapping):
        summary_html = ""  # feedparser's copy of an Atom <content>, which has its own type and base
    if not summary_html:
        summary_html, content_html = content_html, ""
        summary_base = content_base

    key = _key(entry, title_html, key_text)
    if not key:
        return None

    summary = _cap(to_markdown(summary_html, summary_base))
    content = _cap(to_markdown(content_html, content_base))
    if content == summary:
        content = ""

    return Item(
        key=key,
        title=_cap(to_text(title_html)),
        link=_resolve(_link(entry), base),
        summary=summary,
        content=content,
        author=_author(entry),
        published=_published(entry),
        categories=_categories(entry),
        image=_image(entry, content_html, summary_html, base, content_base, summary_base),
    )


def _link(entry: _Entry) -> object:
    """The entry's link, unless it is only an opaque `<guid>` that feedparser took for one."""
    link = entry.get("link")
    if (
        entry.get("guidislink")
        and not entry.get("links")
        and link == entry.get("id")
        and isinstance(link, str)
        and not link.strip().lower().startswith(("http://", "https://", "/"))
    ):
        return ""
    return link


def _key(entry: _Entry, title: str, text: str) -> str:
    """Hash the raw source values, so that the key survives changes to how text is converted."""
    title = title.strip()
    date = _string(_own(entry, "published")) or _string(_own(entry, "updated"))
    candidates = (
        ("id", _string(entry.get("id"))),
        ("link", _string(entry.get("link"))),
        ("dated", f"{title}\n{date}" if title and date else ""),
        ("text", f"{title}\n{text.strip()}" if title or text.strip() else ""),
    )
    for kind, value in candidates:
        if value:
            data = f"{kind}\n{value}".encode("utf-8", "surrogatepass")
            return hashlib.sha256(data).hexdigest()[:_KEY_CHARS]
    return ""


def _own(entry: _Entry, name: str) -> Any:
    """A value the entry really has. feedparser's own lookup substitutes one date for another."""
    return dict.get(entry, name) if isinstance(entry, dict) else entry.get(name)


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS].rstrip()


def _markup(source: _Entry, name: str) -> str:
    """A text element as HTML. Text declared plain keeps any `<` it has as a character."""
    value = source.get(name)
    if not isinstance(value, str):
        return ""
    return _as_html(value, source.get(f"{name}_detail"))


def _as_html(value: str, detail: object) -> str:
    if isinstance(detail, Mapping) and detail.get("type") == "text/plain":
        # Entities are left alone: double-escaped titles are far more common than a real "&amp;".
        return value.replace("<", "&lt;")
    return value


def _content(entry: _Entry, base: str) -> tuple[str, str]:
    """The first content part as HTML, with the base its links resolve against."""
    parts = entry.get("content")
    if not isinstance(parts, list):
        return "", base
    for part in parts:
        if isinstance(part, Mapping):
            value = part.get("value")
            if isinstance(value, str) and value.strip():
                return _as_html(value, part), _detail_base(part, base)
    return "", base


def _detail_base(detail: object, base: str) -> str:
    """The Feed's address, or the xml:base feedparser worked out for this text, made absolute."""
    declared = detail.get("base") if isinstance(detail, Mapping) else None
    if not isinstance(declared, str) or not declared.strip():
        return base
    try:
        return urljoin(base, declared.strip()) if base else declared.strip()
    except ValueError:
        return base


def _author(entry: _Entry) -> str:
    detail = entry.get("author_detail")
    name = detail.get("name") if isinstance(detail, Mapping) else None
    if not _string(name):
        name = entry.get("author")
    return _cap(to_text(name)) if isinstance(name, str) else ""


def _categories(entry: _Entry) -> tuple[str, ...]:
    tags = entry.get("tags")
    if not isinstance(tags, list):
        return ()
    found: dict[str, None] = {}
    for tag in tags:
        if not isinstance(tag, Mapping):
            continue
        term = _string(tag.get("term")) or _string(tag.get("label"))
        text = _cap(to_text(term))
        if text:
            found.setdefault(text)
        if len(found) >= MAX_CATEGORIES:
            break
    return tuple(found)


def _published(entry: _Entry) -> int | None:
    for name in ("published_parsed", "updated_parsed"):
        parsed = _own(entry, name)
        if not parsed:
            continue
        try:
            seconds = calendar.timegm(parsed)
        except (TypeError, ValueError, OverflowError):
            continue
        if seconds > 0:  # year 1 and the like are placeholders, not dates
            return seconds
    return None


def _resolve(url: object, base: str) -> str:
    """An absolute http or https URL, or "" when it is neither."""
    if not isinstance(url, str):
        return ""
    url = "".join(url.split())
    if not url:
        return ""
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return url


def _looks_like_image(url: str) -> bool:
    try:
        return urlsplit(url).path.lower().endswith(_IMAGE_SUFFIXES)
    except ValueError:
        return False


def _dicts(value: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, list):
        for element in value:
            if isinstance(element, Mapping):
                yield element


def _media_content_image(media: Mapping[str, Any], base: str) -> str:
    url = _resolve(media.get("url"), base)
    kind = _string(media.get("type")).lower()
    medium = _string(media.get("medium")).lower()
    if kind.startswith("image/") or medium == "image":
        return url
    if kind or medium:
        return ""  # declared as something else, e.g. a video
    return url if _looks_like_image(url) else ""


def _image_candidates(
    entry: _Entry, content: str, summary: str, base: str, content_base: str, summary_base: str
) -> Iterator[str]:
    for media in _dicts(entry.get("media_content")):
        yield _media_content_image(media, base)
    for thumbnail in _dicts(entry.get("media_thumbnail")):
        yield _resolve(thumbnail.get("url"), base)  # a thumbnail is an image by definition
    for enclosure in _dicts(entry.get("enclosures")):
        if _string(enclosure.get("type")).lower().startswith("image/"):
            yield _resolve(enclosure.get("href"), base)
    image = entry.get("image")
    if isinstance(image, Mapping):
        yield _resolve(image.get("href"), base)
    yield first_image(content, content_base)
    yield first_image(summary, summary_base)


def _image(
    entry: _Entry, content: str, summary: str, base: str, content_base: str, summary_base: str
) -> str:
    candidates = _image_candidates(entry, content, summary, base, content_base, summary_base)
    return next((url for url in candidates if url), "")


def _feed_image(feed: _Entry, base: str) -> str:
    image = feed.get("image")
    candidates = [
        image.get("href") if isinstance(image, Mapping) else None,
        feed.get("icon"),
        feed.get("logo"),
    ]
    return next((url for url in (_resolve(c, base) for c in candidates) if url), "")


def _looks_like_html(body: bytes) -> bool:
    head = body[:_HTML_SNIFF_BYTES].lower()
    return b"<!doctype html" in head or b"<html" in head
