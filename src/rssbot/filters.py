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


def _contains(texts: Iterable[str], word: str) -> bool:
    pattern = _pattern(word)
    if pattern is None:
        return False
    return any(pattern.search(_fold(text)) for text in texts if text)


def _texts(item: Item, field: FilterField) -> Iterable[str]:
    if field == FilterField.TITLE:
        return (item.title,)
    if field == FilterField.DESCRIPTION:
        return (_visible(item.summary), _visible(item.content))
    if field == FilterField.CATEGORY:
        return item.categories  # each category is matched on its own
    if field == FilterField.AUTHOR:
        return (item.author,)
    return (item.title, _visible(item.summary), _visible(item.content))


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
