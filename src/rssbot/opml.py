"""OPML import and export. Pure: bytes in, data out. Uploaded files are untrusted."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.parsers import expat
from xml.sax.saxutils import escape

MAX_OPML_BYTES = 1024 * 1024
MAX_OPML_ENTRIES = 500


@dataclass(frozen=True, slots=True)
class OpmlEntry:
    title: str
    url: str


class OpmlError(ValueError):
    """The message is one plain sentence, safe to show to a Discord user."""


# Checked on the raw bytes before any parsing. NUL bytes are removed first so that
# UTF-16 and UTF-32 files are caught too.
_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

_ENCODING = re.compile(
    rb"\A\s*<\?xml[^>]*?\sencoding\s*=\s*[\"']([A-Za-z][A-Za-z0-9._-]*)[\"']", re.IGNORECASE
)
# What expat decodes itself. Any other declared encoding is decoded here first.
_EXPAT_ENCODINGS = frozenset(
    {"utf-8", "utf8", "utf-16", "utf16", "iso-8859-1", "us-ascii", "ascii"}
)

# Characters XML 1.0 cannot carry at all, not even as character references.
_ILLEGAL_XML = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")

_ATTRIBUTE_ESCAPES = {'"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}


def _reject_declaration(*_args: object) -> None:
    raise OpmlError("The file contains a DOCTYPE or entity declaration, which is not allowed.")


class _Collector:
    def __init__(self) -> None:
        self.entries: list[OpmlEntry] = []
        self._seen: set[str] = set()

    def start_element(self, name: str, attrs: dict[str, str]) -> None:
        if name != "outline":
            return
        lowered: dict[str, str] = {}
        for key, value in attrs.items():
            lowered.setdefault(key.lower(), value)
        url = _feed_url(lowered.get("xmlurl", "").strip())
        if not _is_web_url(url) or url in self._seen:
            return
        if len(self.entries) >= MAX_OPML_ENTRIES:
            raise OpmlError(f"The file lists more than {MAX_OPML_ENTRIES} feeds: too many.")
        self._seen.add(url)
        title = lowered.get("title", "").strip() or lowered.get("text", "").strip() or url
        self.entries.append(OpmlEntry(title=title, url=url))


def _feed_url(url: str) -> str:
    """Browsers' feed: scheme, in both of its forms, as the plain address it wraps."""
    if url[:5].lower() != "feed:":
        return url
    rest = url[5:]
    if rest.startswith("//"):
        return "http:" + rest
    return rest


def _is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def parse_opml(data: bytes) -> list[OpmlEntry]:
    if len(data) > MAX_OPML_BYTES:
        raise OpmlError(f"The file is larger than {MAX_OPML_BYTES // (1024 * 1024)} MB.")
    if not data.removeprefix(b"\xef\xbb\xbf").strip():
        raise OpmlError("The file is empty.")
    if _DECLARATION.search(data) or _DECLARATION.search(data.replace(b"\x00", b"")):
        _reject_declaration()

    collector = _Collector()
    parser = expat.ParserCreate()
    parser.StartElementHandler = collector.start_element
    # Backstop for encodings the byte check above cannot see through.
    parser.StartDoctypeDeclHandler = _reject_declaration
    parser.EntityDeclHandler = _reject_declaration
    try:
        parser.Parse(_decoded(data), True)
    except OpmlError:
        raise
    except expat.ExpatError:
        raise OpmlError("The file is not valid XML, so it is not an OPML feed list.") from None
    except (LookupError, ValueError):
        raise OpmlError("The file uses a text encoding that cannot be read.") from None

    if not collector.entries:
        raise OpmlError("The file does not contain any feeds.")
    return collector.entries


def _decoded(data: bytes) -> bytes | str:
    """Text for expat. It cannot read gb2312, shift_jis, euc-kr and the like, so Python does."""
    match = _ENCODING.match(data)
    if match is None:
        return data
    name = match.group(1).decode("ascii").lower()
    if name in _EXPAT_ENCODINGS:
        return data
    return data.decode(name)  # LookupError or UnicodeError: parse_opml reports it


def _attribute(value: str) -> str:
    return escape(_ILLEGAL_XML.sub("", value), _ATTRIBUTE_ESCAPES)


def build_opml(entries: Sequence[OpmlEntry], title: str) -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        f"<head><title>{escape(_ILLEGAL_XML.sub('', title))}</title></head>",
        "<body>",
    ]
    for entry in entries:
        name = _attribute(entry.title)
        url = _attribute(entry.url)
        lines.append(f'<outline type="rss" text="{name}" title="{name}" xmlUrl="{url}"/>')
    lines += ["</body>", "</opml>", ""]
    return "\n".join(lines).encode("utf-8")
