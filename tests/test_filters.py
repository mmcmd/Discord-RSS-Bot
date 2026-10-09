from __future__ import annotations

import itertools

import pytest

from rssbot.filters import normalise_word, passes
from rssbot.models import Filter, FilterField, FilterList, Item

_ids = itertools.count(1)


def item(
    title: str = "",
    summary: str = "",
    content: str = "",
    author: str = "",
    categories: tuple[str, ...] = (),
) -> Item:
    return Item(
        key="k",
        title=title,
        link="https://example.com/1",
        summary=summary,
        content=content,
        author=author,
        published=None,
        categories=categories,
        image="",
    )


def flt(
    word: str,
    list_: FilterList = FilterList.MUST_HAVE,
    field: FilterField = FilterField.ANY,
) -> Filter:
    return Filter(id=next(_ids), feed_id=1, list=list_, field=field, word=word)


def must(word: str, field: FilterField = FilterField.ANY) -> Filter:
    return flt(word, FilterList.MUST_HAVE, field)


def block(word: str, field: FilterField = FilterField.ANY) -> Filter:
    return flt(word, FilterList.BLOCK, field)


# --- no filters -------------------------------------------------------------


def test_no_filters_everything_passes():
    assert passes(item(), [])
    assert passes(item(title="anything"), [])


# --- whole-word matching ----------------------------------------------------


@pytest.mark.parametrize(
    "title",
    ["Leafs WIN in OT", "a win-win deal", "win", "Win!", "(win)", "they win, again"],
)
def test_word_matches_as_whole_word(title):
    assert passes(item(title=title), [must("win")])


@pytest.mark.parametrize("title", ["winner", "twin", "winning", "swing", "win2", "2win"])
def test_word_does_not_match_inside_other_words(title):
    assert not passes(item(title=title), [must("win")])


def test_case_is_ignored_on_both_sides():
    assert passes(item(title="LEAFS win"), [must("leafs")])
    assert passes(item(title="leafs win"), [must("LEAFS")])
    assert passes(item(title="Leafs win"), [must("lEaFs")])


def test_match_at_start_and_end_of_text():
    assert passes(item(title="win now"), [must("win")])
    assert passes(item(title="now win"), [must("win")])


# --- phrases ----------------------------------------------------------------


def test_phrase_matches_in_order():
    assert passes(item(title="Latest trade rumour roundup"), [must("trade rumour")])


def test_phrase_does_not_match_reversed_or_apart():
    assert not passes(item(title="rumour trade"), [must("trade rumour")])
    assert not passes(item(title="trade big rumour"), [must("trade rumour")])


@pytest.mark.parametrize(
    "title",
    ["trade  rumour", "trade\trumour", "trade\nrumour", "trade \n rumour", "trade rumour"],
)
def test_phrase_allows_any_whitespace_between_words(title):
    assert passes(item(title=title), [must("trade rumour")])


def test_phrase_in_filter_with_odd_whitespace():
    assert passes(item(title="trade rumour"), [must("  trade   rumour  ")])


def test_phrase_is_whole_word_at_both_ends():
    assert not passes(item(title="strade rumour"), [must("trade rumour")])
    assert not passes(item(title="trade rumours"), [must("trade rumour")])


def test_phrase_must_be_directly_adjacent_words():
    assert not passes(item(title="trade, rumour"), [must("trade rumour")])


# --- punctuation words ------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "title"),
    [
        ("c++", "Why c++ is still fast"),
        ("c++", "I like C++."),
        ("c++", "(C++)"),
        (".net", "New .NET release"),
        ("#nhl", "Tonight on #NHL"),
        ("#nhl", "#nhl"),
        ("c#", "Learning C# today"),
        ("node.js", "Node.js 24 is out"),
        ("what?", "so what? nobody knows"),
        ("[live]", "[LIVE] game thread"),
    ],
)
def test_punctuation_words_match(word, title):
    assert passes(item(title=title), [must(word)])


@pytest.mark.parametrize(
    ("word", "title"),
    [
        ("c++", "abc++"),
        ("c++", "c++x"),
        (".net", "asp.net"),
        ("#nhl", "a#nhl"),
        ("#nhl", "#nhlpa"),
        ("c#", "abc#"),
    ],
)
def test_punctuation_words_respect_edges(word, title):
    assert not passes(item(title=title), [must(word)])


def test_regex_metacharacters_are_literal():
    assert not passes(item(title="abc"), [must("a.c")])
    assert passes(item(title="a.c"), [must("a.c")])
    assert not passes(item(title="anything"), [must(".*")])
    assert passes(item(title="all (a|b) done"), [must("(a|b)")])
    assert passes(item(title="a\\b"), [must("a\\b")])


# --- non-English text -------------------------------------------------------


def test_accented_words():
    assert passes(item(title="Un café très bon"), [must("café")])
    assert passes(item(title="UN CAFÉ"), [must("café")])
    assert passes(item(title="un café"), [must("CAFÉ")])


def test_accented_letter_is_a_letter_for_word_edges():
    assert not passes(item(title="cafés"), [must("café")])
    assert not passes(item(title="café"), [must("caf")])
    assert not passes(item(title="éwin"), [must("win")])
    assert not passes(item(title="winé"), [must("win")])


def test_decomposed_and_composed_forms_match_each_other():
    decomposed = "café"
    assert passes(item(title=f"un {decomposed} noir"), [must("café")])
    assert passes(item(title="un café noir"), [must(decomposed)])


def test_combining_mark_is_not_a_word_edge():
    assert not passes(item(title="café"), [must("cafe")])


def test_german_and_turkish_style_case():
    assert passes(item(title="STRASSE Größe"), [must("größe")])
    assert passes(item(title="ÜBER uns"), [must("über")])


def test_greek_and_cyrillic_case():
    assert passes(item(title="ΟΔΟΣ Αθηνών"), [must("οδος")])  # final sigma
    assert passes(item(title="Новости из МОСКВЫ"), [must("москвы")])
    assert passes(item(title="новости"), [must("НОВОСТИ")])
    assert not passes(item(title="новостной"), [must("новост")])


def test_chinese_japanese_and_thai_words_match_anywhere():
    # These are written without spaces, so a word has no gap around it to look for.
    assert passes(item(title="今日は東京で会議"), [must("東京")])
    assert passes(item(title="今日のニュースです"), [must("ニュース")])
    assert passes(item(title="今日 ニュース です"), [must("ニュース")])
    assert passes(item(title="我今天去北京开会"), [must("北京")])
    assert passes(item(title="ไปกรุงเทพวันนี้"), [must("กรุงเทพ")])
    assert passes(item(title="abc東京def"), [must("東京")])
    assert not passes(item(title="今日は大阪で会議"), [must("東京")])
    assert not passes(item(title="今日は東京で会議"), [block("東京")])


def test_a_spaced_word_next_to_an_unspaced_script_is_still_whole():
    assert passes(item(title="新しいiPhone発売"), [must("iphone")])
    assert not passes(item(title="新しいiPhones発売"), [must("iphone")])
    # Only the end written in the unspaced script is free of the whole-word rule.
    assert passes(item(title="最新のiPhone発売について"), [must("iphone発売")])
    assert not passes(item(title="最新のxiPhone発売について"), [must("iphone発売")])
    assert not passes(item(title="한국어뉴스입니다"), [must("뉴스")])  # Korean uses spaces


def test_arabic_and_hebrew():
    assert passes(item(title="أخبار اليوم"), [must("أخبار")])
    assert passes(item(title="חדשות היום"), [must("חדשות")])


def test_digits_count_as_word_characters():
    assert not passes(item(title="win10"), [must("win")])
    assert passes(item(title="win 10"), [must("win")])
    assert passes(item(title="win10"), [must("win10")])
    assert passes(item(title="٣ ٤"), [must("٤")])  # Arabic-Indic digits


# --- fields -----------------------------------------------------------------


def test_title_field_only_looks_at_title():
    f = must("goal", FilterField.TITLE)
    assert passes(item(title="Big goal"), [f])
    assert not passes(item(summary="goal", content="goal", author="goal"), [f])


def test_description_field_looks_at_summary_and_content_not_title():
    f = must("goal", FilterField.DESCRIPTION)
    assert passes(item(summary="a goal"), [f])
    assert passes(item(content="a goal"), [f])
    assert passes(item(summary="nothing", content="a goal"), [f])
    assert not passes(item(title="goal", author="goal", categories=("goal",)), [f])


def test_any_field_looks_at_title_and_description():
    f = must("goal", FilterField.ANY)
    assert passes(item(title="goal"), [f])
    assert passes(item(summary="goal"), [f])
    assert passes(item(content="goal"), [f])
    assert not passes(item(author="goal", categories=("goal",)), [f])


def test_author_field_only_looks_at_author():
    f = must("jane doe", FilterField.AUTHOR)
    assert passes(item(author="Jane Doe"), [f])
    assert not passes(item(title="jane doe", summary="jane doe"), [f])


def test_category_field_matches_if_any_category_matches():
    f = must("hockey", FilterField.CATEGORY)
    assert passes(item(categories=("news", "Hockey")), [f])
    assert not passes(item(categories=("news", "football")), [f])
    assert not passes(item(categories=()), [f])
    assert not passes(item(title="hockey"), [f])


def test_category_phrase_does_not_span_categories():
    f = must("trade rumour", FilterField.CATEGORY)
    assert not passes(item(categories=("trade", "rumour")), [f])
    assert passes(item(categories=("trade rumour",)), [f])


def test_category_is_whole_word_within_category():
    f = must("nhl", FilterField.CATEGORY)
    assert passes(item(categories=("NHL playoffs",)), [f])
    assert not passes(item(categories=("nhlpa",)), [f])


def test_phrase_does_not_span_title_and_summary():
    assert not passes(item(title="trade", summary="rumour"), [must("trade rumour")])


def test_empty_fields_do_not_match():
    for field in FilterField:
        assert not passes(item(), [must("x", field)])


# --- link addresses are not words -------------------------------------------


@pytest.mark.parametrize(
    ("word", "body"),
    [
        ("example", "[read more](https://example.com/story)"),
        ("story", "[read more](https://example.com/story)"),
        ("politics", "see https://news.com/politics/x"),
        ("politics", "see http://news.com/politics"),
        ("politics", "see <https://news.com/politics/x> now"),
        ("cdn", "![chart](https://cdn.example.com/a.png)"),
        ("https", "[read more](https://example.com/story)"),
        ("news", "[one](https://news.com/1) and [two](https://news.com/2)"),
    ],
)
def test_a_word_inside_a_link_address_does_not_match(word, body):
    for field in (FilterField.ANY, FilterField.DESCRIPTION):
        assert passes(item(title="Budget", summary=body), [block(word, field)])
        assert passes(item(title="Budget", content=body), [block(word, field)])
        assert not passes(item(title="Budget", summary=body), [must(word, field)])
        assert not passes(item(title="Budget", content=body), [must(word, field)])


@pytest.mark.parametrize(
    ("word", "body"),
    [
        ("read more", "[read more](https://example.com/story)"),
        ("chart", "![chart](https://cdn.example.com/a.png)"),
        ("politics", "[one](https://news.com/1) politics today"),
        ("politics", "see https://news.com/x for politics"),
        ("politics", "https://news.com/x\npolitics"),
        ("two", "[one](https://news.com/1) and [two](https://news.com/2)"),
        ("budget vote", "the [budget](https://news.com/b) vote"),
        ("[live]", "[LIVE] from https://news.com/x"),
    ],
)
def test_the_visible_words_around_a_link_still_match(word, body):
    assert not passes(item(summary=body), [block(word)])
    assert passes(item(content=body), [must(word, FilterField.DESCRIPTION)])


def test_a_title_is_plain_text_and_is_matched_as_it_is():
    assert passes(item(title="Visit https://example.com today"), [must("example")])
    assert passes(item(title="Notes [draft](v2)"), [must("v2", FilterField.TITLE)])


def test_many_brackets_and_addresses_are_still_fast():
    text = "[" * 50_000 + "[a](https://x.com/needle) " * 20_000 + "needle"
    assert passes(item(summary=text), [must("needle")])
    assert not passes(item(summary=text), [must("x.com")])


# --- empty words ------------------------------------------------------------


@pytest.mark.parametrize("word", ["", " ", "   ", "\t\n", " "])
def test_blank_word_matches_nothing(word):
    assert not passes(item(title="some title", summary="text"), [must(word)])


@pytest.mark.parametrize("word", ["", "  "])
def test_blank_block_word_blocks_nothing(word):
    assert passes(item(title="some title"), [block(word)])


def test_blank_must_have_filter_still_counts_as_a_must_have_list():
    # The list exists, nothing can satisfy it, so nothing passes.
    assert not passes(item(title="some title"), [must("")])
    assert not passes(item(title="some title"), [must(""), must("   ")])
    assert passes(item(title="some title"), [must(""), must("title")])


# --- must-have and block lists ----------------------------------------------


def test_must_have_requires_at_least_one_match():
    filters = [must("nhl"), must("nba")]
    assert passes(item(title="NHL tonight"), filters)
    assert passes(item(title="NBA tonight"), filters)
    assert passes(item(title="NHL and NBA"), filters)
    assert not passes(item(title="MLB tonight"), filters)


def test_block_rejects_on_any_match():
    filters = [block("spoiler"), block("rumour")]
    assert not passes(item(title="spoiler alert"), filters)
    assert not passes(item(title="a rumour"), filters)
    assert passes(item(title="confirmed"), filters)


def test_only_block_filters_let_everything_else_through():
    assert passes(item(title="plain"), [block("spoiler")])


def test_must_have_and_block_together():
    filters = [must("leafs"), block("rumour")]
    assert passes(item(title="Leafs win"), filters)
    assert not passes(item(title="Leafs rumour"), filters)
    assert not passes(item(title="Habs win"), filters)
    assert not passes(item(title="Habs rumour"), filters)


def test_block_wins_over_must_have_in_different_fields():
    filters = [
        must("leafs", FilterField.TITLE),
        block("sponsored", FilterField.CATEGORY),
    ]
    assert passes(item(title="Leafs", categories=("news",)), filters)
    assert not passes(item(title="Leafs", categories=("Sponsored",)), filters)


def test_mixed_fields_in_must_have_list_are_alternatives():
    filters = [
        must("jane", FilterField.AUTHOR),
        must("hockey", FilterField.CATEGORY),
    ]
    assert passes(item(author="Jane"), filters)
    assert passes(item(categories=("hockey",)), filters)
    assert not passes(item(title="jane hockey"), filters)


def test_block_phrase():
    assert not passes(item(title="big trade   rumour"), [block("trade rumour")])
    assert passes(item(title="trade news and rumour"), [block("trade rumour")])


def test_block_on_description_does_not_block_on_title():
    assert passes(
        item(title="spoiler", summary="clean"),
        [block("spoiler", FilterField.DESCRIPTION)],
    )


def test_block_with_punctuation_word():
    assert not passes(item(title="Using C++ daily"), [block("c++")])


def test_duplicate_filters_are_harmless():
    filters = [must("win"), must("win"), block("loss"), block("loss")]
    assert passes(item(title="win"), filters)
    assert not passes(item(title="win loss"), filters)


def test_accepts_any_sequence_type():
    assert passes(item(title="win"), (must("win"),))


# --- robustness -------------------------------------------------------------


def test_very_long_text_and_word():
    text = "word " * 50_000 + "needle"
    assert passes(item(title=text), [must("needle")])
    assert not passes(item(title=text), [must("needle" * 1000)])


def test_unusual_characters_never_raise():
    weird = "\x00\ud800 \U0001f600 ‮ text"
    assert passes(item(title=weird), [block("zzz")])
    assert passes(item(title=weird), [must("\U0001f600")])
    assert not passes(item(title="x"), [must("\x00")])


def test_never_raises_on_broken_filter():
    bad = Filter(id=1, feed_id=1, list=FilterList.MUST_HAVE, field=FilterField.ANY, word=None)  # type: ignore[arg-type]
    assert passes(item(title="x"), []) is True
    assert passes(item(title="x"), [bad]) is False


# --- normalise_word ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("win", "win"),
        ("  win  ", "win"),
        ("trade    rumour", "trade rumour"),
        ("\ttrade\n rumour \t", "trade rumour"),
        ("a b", "a b"),
        ("", ""),
        ("   ", ""),
        ("  c++ ", "c++"),
    ],
)
def test_normalise_word(raw, expected):
    assert normalise_word(raw) == expected


def test_normalise_word_keeps_case():
    assert normalise_word("  NHL ") == "NHL"
