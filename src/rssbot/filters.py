"""Decide whether an Item should be posted, given its Feed's Filters. Pure: no I/O."""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterable, Sequence

from rssbot.models import Filter, FilterField, FilterList, Item

# A match is whole when the characters around it are not letters or digits.
# Combining marks count as part of the letter they follow, so "cafe" does not
# match inside a decomposed "café".
_NOT_AFTER = r"(?<![^\W_])(?<![̀-ͯ])"
_NOT_BEFORE = r"(?![^\W_])(?![̀-ͯ])"

# Chinese, Japanese and Thai are written without spaces between words, so there a word has
# no gap around it to look for. A character of one of these scripts is never part of a
# neighbouring word, and a Filter word that begins or ends with one matches anywhere.
_UNSPACED = (
    "\u0e00-\u0e7f"  # Thai
    "\u3005-\u3007\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f"  # kana and iteration marks
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af"  # Han
)
_UNSPACED_CHAR = re.compile(f"[{_UNSPACED}]")
_NOT_AFTER_WORD = f"(?:{_NOT_AFTER}|(?<=[{_UNSPACED}]))"
_NOT_BEFORE_WORD = f"(?:{_NOT_BEFORE}|(?=[{_UNSPACED}]))"

# The description is Markdown, where a link is "[label](address)". Only the label is read by
# people, so the address is taken out before matching, as is an address written out in the text.
_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\(https?://[^\s()]*\)")
_ADDRESS = re.compile(r"https?://\S+")

# html2md also marks emphasis (`*`, `**`), strikethrough (`~~`), code (backticks and fences) and
# quotes (a leading `>` on each line). A phrase runs through them, so a description is also
# matched with them taken out. Only the marks: html2md does not escape a `*` that is just a
# character, as in "a * b" or "2**10", and that one stays.
_QUOTE = re.compile(r"^[ \t]*> ?", re.MULTILINE)
_EMPHASIS = re.compile(r"\*+")
_STRIKE = re.compile(r"~{2,}")
_CODE = re.compile(r"`+")


def normalise_word(word: str) -> str:
    """Trim and collapse inner whitespace to single spaces."""
    return " ".join(word.split())


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text.casefold())


@functools.lru_cache(maxsize=1024)
def _pattern(word: str) -> re.Pattern[str] | None:
    tokens = _fold(word).split()
    if not tokens:
        return None
    body = r"\s+".join(re.escape(token) for token in tokens)
    before = "" if _UNSPACED_CHAR.match(tokens[0][0]) else _NOT_AFTER_WORD
    after = "" if _UNSPACED_CHAR.match(tokens[-1][-1]) else _NOT_BEFORE_WORD
    return re.compile(before + body + after)


def _visible(markdown: str) -> str:
    """The Markdown without its link addresses: the words a reader sees."""
    return _ADDRESS.sub(" ", _LINK.sub(r"\1", markdown))


def _strip_code(text: str) -> str:
    """`text` without the backticks that open and close a code span: a run of backticks is
    closed by the next run of the same length, and one with no such run is a character."""
    runs = list(_CODE.finditer(text))
    closing: list[int | None] = [None] * len(runs)
    latest: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        length = len(runs[index].group())
        closing[index] = latest.get(length)
        latest[length] = index
    left = [len(run.group()) for run in runs]
    index = 0
    while index < len(runs):
        end = closing[index]
        if end is None:
            index += 1
        else:
            left[index] = left[end] = 0
            index = end + 1
    return _keep(text, runs, left)


def _strip_pairs(text: str, pattern: re.Pattern[str]) -> str:
    """`text` without the runs of `pattern` that open and close a mark, as Discord reads
    them: a run with a space after it cannot open, one with a space before it cannot close,
    and one with no partner is a character (a*b)."""
    runs = list(pattern.finditer(text))
    left = [len(run.group()) for run in runs]
    opening: list[int] = []
    for index, run in enumerate(runs):
        before = text[run.start() - 1] if run.start() else " "
        after = text[run.end()] if run.end() < len(text) else " "
        if not before.isspace():
            while left[index] and opening:
                other = opening[-1]
                paired = min(left[index], left[other])
                left[index] -= paired
                left[other] -= paired
                if not left[other]:
                    opening.pop()
        if left[index] and not after.isspace():
            opening.append(index)
    return _keep(text, runs, left)


def _keep(text: str, runs: list[re.Match[str]], left: list[int]) -> str:
    """`text` with only the first `left[n]` characters of its n-th run."""
    parts: list[str] = []
    at = 0
    for run, count in zip(runs, left, strict=True):
        parts.append(text[at : run.start()])
        parts.append(run.group()[:count])
        at = run.end()
    parts.append(text[at:])
    return "".join(parts)


def _unmarked(visible: str) -> str:
    """The visible words without the marks html2md puts around them."""
    text = _strip_code(_QUOTE.sub("", visible))
    return _strip_pairs(_strip_pairs(text, _STRIKE), _EMPHASIS)


def _described(markdown: str) -> tuple[str, str]:
    """A description as it reads, and without its marks: a Filter word may match either."""
    visible = _visible(markdown)
    return visible, _unmarked(visible)


def _contains(texts: Iterable[str], word: str) -> bool:
    pattern = _pattern(word)
    if pattern is None:
        return False
    return any(pattern.search(_fold(text)) for text in texts if text)


def _texts(item: Item, field: FilterField) -> Iterable[str]:
    if field == FilterField.TITLE:
        return (item.title,)
    if field == FilterField.DESCRIPTION:
        return (*_described(item.summary), *_described(item.content))
    if field == FilterField.CATEGORY:
        return item.categories  # each category is matched on its own
    if field == FilterField.AUTHOR:
        return (item.author,)
    return (item.title, *_described(item.summary), *_described(item.content))


def _matches(item: Item, flt: Filter) -> bool:
    try:
        return _contains(_texts(item, flt.field), flt.word)
    except Exception:
        return False


def passes(item: Item, filters: Sequence[Filter]) -> bool:
    """Whether the Item should be posted."""
    must_have = [f for f in filters if f.list == FilterList.MUST_HAVE]
    block = [f for f in filters if f.list == FilterList.BLOCK]
    if must_have and not any(_matches(item, f) for f in must_have):
        return False
    return not any(_matches(item, f) for f in block)
