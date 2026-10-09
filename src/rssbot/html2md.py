"""Feed HTML to Discord message formatting. Pure, standard library only.

Feed text is often messy or broken, so nothing here raises: the worst case is plain text
with the tags stripped. Parsing is a single pass with no recursion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

# Longer input is cut here. A Discord message holds a few thousand characters at most.
MAX_INPUT_CHARS = 250_000

_MAX_LIST_INDENT = 8  # levels; deeper lists stop indenting further

_TEXT, _OPEN, _CLOSE = 0, 1, 2
# (_TEXT, text, False), (_OPEN, mark kind, False) or (_CLOSE, link URL, bare URL allowed)
type _Token = tuple[int, str, bool]
type _Attrs = list[tuple[str, str | None]]

_MARK_KINDS = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "s": "s",
    "del": "s",
    "strike": "s",
    "code": "code",
    "tt": "code",
    "kbd": "code",
    "samp": "code",
}
_MARK_TEXT = {"b": "**", "i": "*", "s": "~~"}

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "body",
        "center",
        "details",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "hr",
        "html",
        "main",
        "nav",
        "p",
        "section",
        "summary",
    }
)
_LINES = frozenset({"caption", "dd", "dt"})  # start on a new line, without a blank line
_DROPPED = frozenset(
    {
        "applet",
        "audio",
        "button",
        "canvas",
        "head",
        "iframe",
        "math",
        "noscript",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "template",
        "textarea",
        "title",
        "video",
    }
)
# Tags that do not separate words in to_text. Everything else counts as a gap.
_INLINE = frozenset(
    {
        "a",
        "abbr",
        "acronym",
        "b",
        "bdi",
        "bdo",
        "big",
        "cite",
        "code",
        "del",
        "dfn",
        "em",
        "font",
        "i",
        "img",
        "input",
        "ins",
        "kbd",
        "label",
        "mark",
        "nobr",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        "wbr",
    }
)
_IMAGE_SOURCES = ("src", "data-src", "data-lazy-src", "data-original")  # lazy loaders use the rest

_WHITESPACE = re.compile(r"[ \t\n\r\f\v\xa0]+")
_SPACES = re.compile(r" {2,}")
_SPACE_AT_NEWLINE = re.compile(r" ?\n ?")
_URL_WHITESPACE = re.compile(r"[\t\r\n]")
_TAG = re.compile(r"<[^<>]*>")
# Control characters and lone surrogates, which Discord rejects or which cannot be encoded.
_UNSAFE = dict.fromkeys([*range(9), 11, *range(14, 32), 127, *range(0xD800, 0xE000)])


def to_markdown(html: str, base_url: str = "") -> str:
    """Convert feed HTML to Discord formatting."""
    text = _clean(html)
    if "<" not in text:
        return _plain(text)
    try:
        builder = _MarkdownBuilder(base_url if isinstance(base_url, str) else "")
        builder.feed(text)
        return builder.finish()
    except Exception:
        return _plain(_TAG.sub(" ", text))


def to_text(html: str) -> str:
    """Plain text on one line, with no formatting marks. For titles and author names."""
    text = _clean(html)
    if "<" not in text:
        return " ".join(unescape(text).split())
    try:
        extractor = _TextExtractor()
        extractor.feed(text)
        extractor.close()
        return " ".join("".join(extractor.parts).split())
    except Exception:
        return " ".join(unescape(_TAG.sub(" ", text)).split())


def first_image(html: str, base_url: str = "") -> str:
    """The URL of the first usable image, or ""."""
    text = _clean(html)
    if "<" not in text:
        return ""
    try:
        finder = _ImageFinder(base_url if isinstance(base_url, str) else "")
        finder.feed(text)
        finder.close()
        return finder.url
    except Exception:
        return ""


def _clean(html: object) -> str:
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    if not isinstance(html, str):
        return ""
    if len(html) > MAX_INPUT_CHARS:
        html = html[:MAX_INPUT_CHARS]
        cut = html.rfind("<")
        if cut > html.rfind(">"):  # do not leave half a tag at the end
            html = html[:cut]
    return html.translate(_UNSAFE)


def _plain(text: str) -> str:
    """Text with no tags in it. Its own line breaks are kept."""
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    return _join([" ".join(line.split()) for line in text.split("\n")])


def _join(lines: list[str]) -> str:
    """Join lines, allowing one blank line in a row and none at either end."""
    kept: list[str] = []
    blank = False
    for line in lines:
        if not line:
            blank = bool(kept)
            continue
        if blank:
            kept.append("")
            blank = False
        kept.append(line)
    return "\n".join(kept).strip()


def _attr(attrs: _Attrs, name: str) -> str:
    for key, value in attrs:
        if key == name:
            return value or ""
    return ""


def _resolve(url: str, base_url: str) -> str:
    """An absolute http or https URL, or "" when it is neither."""
    url = _URL_WHITESPACE.sub("", url.strip())
    if not url:
        return ""
    try:
        if base_url:
            url = urljoin(base_url, url)
        elif url.startswith("//"):
            url = "https:" + url
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return url.replace(" ", "%20")


def _link_target(url: str) -> str:
    """Escape what would end a `[text](url)` link early."""
    return url.replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def _is_pixel(size: str) -> bool:
    return size.strip().lower().removesuffix("px").strip() in ("0", "1")


def _tidy(text: str) -> str:
    text = _SPACES.sub(" ", text)
    return _SPACE_AT_NEWLINE.sub("\n", text).strip()


def _code(text: str) -> str:
    text = text.replace("\n", " ")
    if "`" not in text:
        return f"`{text}`"
    if text.startswith("`") or text.endswith("`"):
        text = f" {text} "
    return f"``{text}``"


def _link(label: str, url: str) -> str:
    label = label.replace("\n", " ")
    # Discord will not hide one URL behind another, so a URL as link text gives way to the target.
    if label.startswith(("http://", "https://")) and " " not in label:
        return url
    # Square brackets that do not pair up would end the link early; nested pairs do not.
    if not _brackets_pair_up(label):
        label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({url})"


def _brackets_pair_up(text: str) -> bool:
    depth = 0
    for char in text:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _wrap(kind: str, inner: str, url: str, bare_ok: bool, in_code: bool) -> str:
    """Put one mark around its content, leaving whitespace at the edges outside it."""
    if in_code:
        return inner
    core = inner.strip()
    if kind == "a":
        if not url:
            return inner
        if not core:
            return inner + url if bare_ok else inner
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    if kind == "a":
        body = _link(core, url)
    elif kind == "code":
        body = _code(core)
    else:
        body = f"{_MARK_TEXT[kind]}{core}{_MARK_TEXT[kind]}"
    return f"{lead}{body}{trail}"


def _render(tokens: list[_Token]) -> str:
    frames: list[tuple[str, list[str]]] = [("", [])]
    for op, value, bare_ok in tokens:
        if op == _TEXT:
            frames[-1][1].append(value)
        elif op == _OPEN:
            frames.append((value, []))
        elif len(frames) > 1:
            kind, parts = frames.pop()
            in_code = any(outer == "code" for outer, _ in frames)
            frames[-1][1].append(_wrap(kind, "".join(parts), value, bare_ok, in_code))
    return "".join("".join(parts) for _, parts in frames)


class _Parser(HTMLParser):
    """Hides dropped elements and their content from the subclass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._dropping = ""
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: _Attrs) -> None:
        if self._dropping:
            if tag == self._dropping:
                self._drop_depth += 1
                return
            if not (self._dropping == "head" and tag == "body"):
                return
            self._dropping = ""  # an unclosed <head> ends where <body> starts
        if tag in _DROPPED:
            self._dropping = tag
            self._drop_depth = 1
            return
        self.on_start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._dropping:
            if tag == self._dropping:
                self._drop_depth -= 1
                if not self._drop_depth:
                    self._dropping = ""
            return
        self.on_end(tag)

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self.on_text(data)

    def on_start(self, tag: str, attrs: _Attrs) -> None:
        raise NotImplementedError

    def on_end(self, tag: str) -> None:
        raise NotImplementedError

    def on_text(self, data: str) -> None:
        raise NotImplementedError


class _TextExtractor(_Parser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def on_start(self, tag: str, attrs: _Attrs) -> None:
        if tag not in _INLINE:
            self.parts.append(" ")

    def on_end(self, tag: str) -> None:
        if tag not in _INLINE:
            self.parts.append(" ")

    def on_text(self, data: str) -> None:
        self.parts.append(data)


class _ImageFinder(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self.url = ""

    def handle_starttag(self, tag: str, attrs: _Attrs) -> None:
        if tag != "img" or self.url:
            return
        if _is_pixel(_attr(attrs, "width")) or _is_pixel(_attr(attrs, "height")):
            return
        for name in _IMAGE_SOURCES:
            url = _resolve(_attr(attrs, name), self._base_url)
            if url:
                self.url = url
                return


@dataclass(slots=True)
class _List:
    ordered: bool
    number: int  # of the next item
    base: str  # indentation of this list's markers
    indent: str  # indentation of text under the current item
    implicit: bool = False  # made up for an <li> outside any list
    items: int = 0


class _MarkdownBuilder(_Parser):
    """Collects inline text into a buffer and writes it out as lines at each block boundary."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._out: list[str] = []
        self._gap = 0  # before the next block: 1 for a new line, 2 for a blank line

        self._buf: list[_Token] = []
        self._has_content = False
        self._marks: list[tuple[str, str]] = []  # open marks as (kind, link URL), outermost first
        self._nested = dict.fromkeys(_MARK_TEXT.keys() | {"code"}, 0)
        self._headings = 0
        self._in_link = False
        self._link_has_text = False
        self._link_has_image = False

        self._quotes = 0
        self._lists: list[_List] = []
        self._marker = ""  # list marker waiting for its item's first line
        self._in_row = False
        self._pre = 0
        self._pre_parts: list[str] = []

    def finish(self) -> str:
        self.close()
        if self._pre:
            self._end_pre()
        self._flush()
        return _join(self._out)

    # Parser events

    def on_text(self, data: str) -> None:
        if self._pre:
            self._pre_parts.append(data)
            return
        data = _WHITESPACE.sub(" ", data)
        self._buf.append((_TEXT, data, False))
        if data != " ":
            self._has_content = True
            self._link_has_text = self._in_link

    def on_start(self, tag: str, attrs: _Attrs) -> None:
        if self._pre:
            if tag == "pre":
                self._pre += 1
            elif tag == "br":
                self._pre_parts.append("\n")
            elif tag not in _INLINE:
                self._pre_line()
        elif tag in _MARK_KINDS:
            self._open(_MARK_KINDS[tag])
        elif tag == "a":
            self._open("a", _link_target(_resolve(_attr(attrs, "href"), self._base_url)))
        elif tag == "br":
            self._buf.append((_TEXT, "\n", False))
        elif tag == "img":
            self._link_has_image = self._in_link
        elif tag in _HEADINGS:
            self._break(2)
            self._headings += 1
            self._open("b")
        elif tag in ("ul", "ol"):
            self._start_list(tag == "ol", _attr(attrs, "start"))
        elif tag == "li":
            self._start_item()
        elif tag == "blockquote":
            self._break(2)
            self._quotes += 1
        elif tag == "pre":
            self._break(2)
            self._pre = 1
        elif tag == "table":
            self._break(2)
            self._in_row = False
        elif tag == "tr":
            self._break(1)
            self._in_row = True
        elif tag in ("td", "th"):
            self._start_cell()
        elif tag in _LINES:
            self._break(1)
        elif tag in _BLOCKS:
            self._block()

    def on_end(self, tag: str) -> None:
        if self._pre:
            if tag == "pre":
                self._pre -= 1
                if not self._pre:
                    self._end_pre()
            elif tag not in _INLINE:
                self._pre_line()
        elif tag in _MARK_KINDS:
            self._close(_MARK_KINDS[tag])
        elif tag == "a":
            self._close("a")
        elif tag in _HEADINGS:
            if self._headings:
                self._headings -= 1
                self._close("b")
            self._break(2)
        elif tag in ("ul", "ol"):
            self._end_list()
        elif tag == "li":
            self._end_item()
        elif tag == "blockquote":
            self._break(2)
            self._quotes = max(self._quotes - 1, 0)
        elif tag == "table":
            self._break(2)
            self._in_row = False
        elif tag == "tr":
            self._break(1)
            self._in_row = False
        elif tag in _LINES:
            self._break(1)
        elif tag in _BLOCKS and tag != "hr":
            self._block()

    # Inline marks

    def _open(self, kind: str, url: str = "") -> None:
        if kind == "a":
            if self._in_link:
                self._close("a")  # links do not nest
            self._in_link = True
            self._link_has_text = False
            self._link_has_image = False
        else:
            self._nested[kind] += 1
            if self._nested[kind] > 1:
                return
        self._marks.append((kind, url))
        self._buf.append((_OPEN, kind, False))

    def _close(self, kind: str) -> None:
        bare_ok = False
        if kind == "a":
            if not self._in_link:
                return
            self._in_link = False
            bare_ok = not (self._link_has_text or self._link_has_image)
        else:
            if not self._nested[kind]:
                return  # stray closing tag
            self._nested[kind] -= 1
            if self._nested[kind]:
                return
        index = next(i for i, (open_kind, _) in enumerate(self._marks) if open_kind == kind)
        url = self._marks[index][1]
        # Marks opened later are closed first, then reopened, so the output nests properly.
        later = self._marks[index + 1 :]
        del self._marks[index:]
        self._buf.extend((_CLOSE, later_url, False) for _, later_url in reversed(later))
        self._buf.append((_CLOSE, url, bare_ok))
        if bare_ok and url:
            self._has_content = True
        for mark in later:
            self._marks.append(mark)
            self._buf.append((_OPEN, mark[0], False))

    # Blocks

    def _flush(self) -> None:
        """Write the buffered inline text. Open marks end here and start again in the next block."""
        if self._has_content:
            self._buf.extend((_CLOSE, url, False) for _, url in reversed(self._marks))
            text = _tidy(_render(self._buf))
            if text:
                self._write(text.split("\n"))
        self._buf = [(_OPEN, kind, False) for kind, _ in self._marks]
        self._has_content = False

    def _write(self, lines: list[str]) -> None:
        if self._out and self._gap == 2:
            self._out.append("")
        # Discord has one level of quote, so nested quotes share it.
        quote = "> " if self._quotes else ""
        indent = self._lists[-1].indent if self._lists else ""
        prefix = quote + (self._marker or indent)
        for line in lines:
            self._out.append(prefix + line if line else "")
            prefix = quote + indent
        self._marker = ""
        self._gap = 1

    def _break(self, gap: int) -> None:
        self._flush()
        if not self._marker:  # a block that opens a list item stays on the marker's line
            self._gap = max(self._gap, gap)

    def _block(self) -> None:
        if self._in_row:
            self._buf.append((_TEXT, " ", False))  # a table row stays on one line
        else:
            self._break(2)

    def _start_list(self, ordered: bool, start: str) -> None:
        self._break(1 if self._lists else 2)
        number = int(start) if start.isascii() and start.isdigit() and len(start) < 10 else 1
        base = "  " * min(len(self._lists), _MAX_LIST_INDENT)
        self._lists.append(_List(ordered, number, base, base))

    def _end_list(self) -> None:
        self._flush()
        self._marker = ""
        if self._lists:
            self._lists.pop()
        self._gap = max(self._gap, 1 if self._lists else 2)

    def _start_item(self) -> None:
        self._flush()
        if not self._lists:
            self._lists.append(_List(False, 1, "", "", implicit=True))
        current = self._lists[-1]
        if current.items:
            self._gap = 1  # items sit on consecutive lines even when they hold paragraphs
        marker = f"{current.number}. " if current.ordered else "- "
        current.number += 1
        current.items += 1
        current.indent = current.base + " " * len(marker)
        self._marker = current.base + marker

    def _end_item(self) -> None:
        self._flush()
        self._marker = ""
        if self._lists:
            current = self._lists[-1]
            if current.implicit:
                self._lists.pop()
            else:
                current.indent = current.base

    def _start_cell(self) -> None:
        if not self._in_row:  # a cell with no <tr>
            self._break(1)
            self._in_row = True
        if self._has_content:  # so empty leading cells leave no separator behind
            self._buf.append((_TEXT, " | ", False))

    def _pre_line(self) -> None:
        if self._pre_parts and not self._pre_parts[-1].endswith("\n"):
            self._pre_parts.append("\n")

    def _end_pre(self) -> None:
        text = "".join(self._pre_parts).replace("\r\n", "\n").replace("\r", "\n")
        # A zero-width space stops the content from closing the fence.
        text = text.replace("\xa0", " ").replace("```", "`​``")
        lines = [line.rstrip() for line in text.split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        first = next((i for i, line in enumerate(lines) if line), 0)
        if lines:
            self._write(["```", *lines[first:], "```"])
        self._pre = 0
        self._pre_parts = []
        self._gap = max(self._gap, 2)
