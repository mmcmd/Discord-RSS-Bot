from __future__ import annotations

import re
import time

import pytest

from rssbot.html2md import MAX_INPUT_CHARS, first_image, to_markdown, to_text

BASE = "https://site.example/blog/post"


# Inline marks


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<b>bold</b>", "**bold**"),
        ("<strong>bold</strong>", "**bold**"),
        ("<i>it</i>", "*it*"),
        ("<em>it</em>", "*it*"),
        ("<s>gone</s>", "~~gone~~"),
        ("<del>gone</del>", "~~gone~~"),
        ("<strike>gone</strike>", "~~gone~~"),
        ("<code>x = 1</code>", "`x = 1`"),
        ("<B>Upper</B> <EM>case</EM>", "**Upper** *case*"),
        ("<b><i>both</i></b>", "***both***"),
        ("a <span>plain</span> <u>span</u>", "a plain span"),
    ],
)
def test_inline_marks(html, expected):
    assert to_markdown(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<b> </b>", ""),
        ("<b></b>", ""),
        ("a<b> </b>b", "a b"),
        ("a<i></i>b", "ab"),
        ("<b><i> </i></b>x", "x"),
        ("<code> </code>", ""),
        ("<p><strong><br></strong></p>", ""),
    ],
)
def test_marks_never_wrap_nothing(html, expected):
    assert to_markdown(html) == expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("a<b> hi </b>b", "a **hi** b"),
        ("<b> hi </b>", "**hi**"),
        ("one<em> two</em> three", "one *two* three"),
        ("one <s>two </s>three", "one ~~two~~ three"),
        ("a <b> b </b> c", "a **b** c"),
        ("<b>line<br></b>next", "**line**\nnext"),
    ],
)
def test_whitespace_inside_a_mark_moves_outside(html, expected):
    assert to_markdown(html) == expected


def test_nested_same_mark_is_written_once():
    assert to_markdown("<b>a <strong>b</strong> c</b>") == "**a b c**"


def test_marks_inside_code_are_left_out():
    assert to_markdown("<code>a <b>b</b> <a href='https://e.com'>c</a></code>") == "`a b c`"


def test_code_holding_a_backtick_uses_double_backticks():
    assert to_markdown("<code>a ` b</code>") == "``a ` b``"
    assert to_markdown("<code>`a`</code>") == "`` `a` ``"


def test_markdown_in_the_feed_text_is_not_escaped():
    html = "<p>2 * 3 * 4, snake_case_name, [brackets] and # hash</p>"

    assert to_markdown(html) == "2 * 3 * 4, snake_case_name, [brackets] and # hash"


# Links


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<a href="https://e.com/a">text</a>', "[text](https://e.com/a)"),
        ('<a href="http://e.com/a">text</a>', "[text](http://e.com/a)"),
        ('<a href="HTTPS://E.com/a">text</a>', "[text](HTTPS://E.com/a)"),
        ('<a href="https://e.com/?a=1&amp;b=2">q</a>', "[q](https://e.com/?a=1&b=2)"),
        ('<a href=" https://e.com/a\n">text</a>', "[text](https://e.com/a)"),
        ('<a href="https://e.com/a b">text</a>', "[text](https://e.com/a%20b)"),
        ('<a href="https://e.com/a_(b)">wiki</a>', "[wiki](https://e.com/a_%28b%29)"),
        ('<a href="https://e.com/a"> text </a>', "[text](https://e.com/a)"),
        ('see<a href="https://e.com/a"> text </a>now', "see [text](https://e.com/a) now"),
        ('<a href="https://e.com/a"><b>bold</b></a>', "[**bold**](https://e.com/a)"),
        ('<b><a href="https://e.com/a">bold</a></b>', "**[bold](https://e.com/a)**"),
        ('<a href="https://e.com/a">two<br>lines</a>', "[two lines](https://e.com/a)"),
        # Square brackets that do not pair up would end the link early.
        (
            '<a href="http://x.com/a(b)">te]xt) [y]</a>',
            r"[te\]xt) \[y\]](http://x.com/a%28b%29)",
        ),
        ('<a href="https://e.com/a">a [b</a>', r"[a \[b](https://e.com/a)"),
        ('<a href="https://e.com/a">a] [b</a>', r"[a\] \[b](https://e.com/a)"),
        # Brackets that pair up are left as they are.
        ('<a href="https://e.com/a">[1]</a>', "[[1]](https://e.com/a)"),
        ('<a href="https://e.com/a">a [b [c]] d</a>', "[a [b [c]] d](https://e.com/a)"),
    ],
)
def test_links(html, expected):
    assert to_markdown(html) == expected


@pytest.mark.parametrize(
    "html",
    [
        '<a href="https://e.com/a"></a>',
        '<a href="https://e.com/a"> </a>',
        '<a href="https://e.com/a">https://e.com/a</a>',
        '<a href="https://e.com/a">https://e.com/…</a>',
        '<a href="https://e.com/a"/>',
    ],
)
def test_link_with_no_text_or_a_url_as_text_is_just_the_url(html):
    assert to_markdown(html) == "https://e.com/a"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<a href="javascript:alert(1)">click</a>', "click"),
        ('<a href="mailto:me@e.com">mail me</a>', "mail me"),
        ('<a href="data:text/html,hi">data</a>', "data"),
        ('<a href="ftp://e.com/f">file</a>', "file"),
        ('<a href="#top">top</a>', "top"),
        ('<a name="anchor">anchor</a>', "anchor"),
        ("<a href>empty</a>", "empty"),
        ('<a href="http://[bad">broken</a>', "broken"),
        ('<a href="javascript:alert(1)"></a>', ""),
    ],
)
def test_links_that_are_not_web_links_keep_only_their_text(html, expected):
    assert to_markdown(html) == expected


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/about", "https://site.example/about"),
        ("other", "https://site.example/blog/other"),
        ("../up", "https://site.example/up"),
        ("?page=2", "https://site.example/blog/post?page=2"),
        ("//cdn.example/x", "https://cdn.example/x"),
        ("https://else.example/x", "https://else.example/x"),
    ],
)
def test_relative_links_resolve_against_the_base_url(href, expected):
    assert to_markdown(f'<a href="{href}">go</a>', BASE) == f"[go]({expected})"


def test_relative_link_with_no_base_url_keeps_only_its_text():
    assert to_markdown('<a href="/about">about</a>') == "about"
    assert to_markdown('<a href="//cdn.example/x">cdn</a>') == "[cdn](https://cdn.example/x)"


def test_link_around_only_an_image_is_dropped():
    html = '<p><a href="https://e.com/full.jpg"><img src="https://e.com/thumb.jpg"></a></p><p>x</p>'

    assert to_markdown(html) == "x"


def test_link_around_blocks_is_repeated_for_each_block():
    html = '<a href="https://e.com/a"><div><img src="i.png"></div><h3>Title</h3><p>More</p></a>'

    assert to_markdown(html) == "[**Title**](https://e.com/a)\n\n[More](https://e.com/a)"


def test_link_inside_a_link_ends_the_first():
    html = '<a href="https://e.com/1">one <a href="https://e.com/2">two</a>'

    assert to_markdown(html) == "[one](https://e.com/1) [two](https://e.com/2)"


# Blocks


def test_paragraphs_are_separated_by_one_blank_line():
    assert to_markdown("<p>one</p><p>two</p>\n\n<p>three</p>") == "one\n\ntwo\n\nthree"


def test_divs_and_stray_text_are_paragraphs():
    assert to_markdown("intro<div>one</div><div>two</div>outro") == "intro\n\none\n\ntwo\n\noutro"


def test_br_is_a_newline():
    assert to_markdown("one<br>two<br/>three<BR />four") == "one\ntwo\nthree\nfour"


def test_many_brs_leave_one_blank_line():
    assert to_markdown("one<br><br><br><br>two") == "one\n\ntwo"


def test_empty_blocks_leave_nothing():
    assert to_markdown("<p>one</p><p></p><div> </div><p>&nbsp;</p><p><br></p><p>two</p>") == (
        "one\n\ntwo"
    )


@pytest.mark.parametrize("level", range(1, 7))
def test_headings_are_bold_on_their_own_line(level):
    html = f"<p>before</p><h{level}>Title</h{level}><p>after</p>"

    assert to_markdown(html) == "before\n\n**Title**\n\nafter"


def test_heading_with_inner_bold_is_bold_once():
    assert to_markdown("<h2><strong>Title</strong> here</h2>") == "**Title here**"


def test_heading_with_a_link():
    assert to_markdown('<h2><a href="https://e.com">Title</a></h2>') == "**[Title](https://e.com)**"


def test_horizontal_rule_is_a_paragraph_break():
    assert to_markdown("one<hr>two<hr/>three") == "one\n\ntwo\n\nthree"


def test_definition_list():
    assert to_markdown("<dl><dt>Term</dt><dd>Meaning</dd></dl>") == "Term\nMeaning"


# Lists


def test_unordered_list():
    assert to_markdown("<ul><li>one</li><li>two</li></ul>") == "- one\n- two"


def test_ordered_list():
    assert to_markdown("<ol><li>one</li><li>two</li><li>three</li></ol>") == (
        "1. one\n2. two\n3. three"
    )


def test_ordered_list_start():
    assert to_markdown('<ol start="5"><li>five</li><li>six</li></ol>') == "5. five\n6. six"
    assert to_markdown('<ol start="x"><li>one</li></ol>') == "1. one"


def test_list_is_set_apart_from_the_text_around_it():
    html = "<p>before</p><ul><li>one</li><li>two</li></ul><p>after</p>"

    assert to_markdown(html) == "before\n\n- one\n- two\n\nafter"


def test_nested_lists_indent_two_spaces_per_level():
    html = (
        "<ul><li>one</li><li>two<ul><li>deep <em>er</em></li>"
        "<li>deep2<ol><li>x</li><li>y</li></ol></li></ul></li><li>three</li></ul>"
    )

    assert to_markdown(html) == (
        "- one\n- two\n  - deep *er*\n  - deep2\n    1. x\n    2. y\n- three"
    )


def test_list_with_whitespace_between_tags():
    html = (
        "<ul>\n  <li>one</li>\n  <li>two\n    <ul>\n      <li>deep</li>\n    </ul>\n  </li>\n</ul>"
    )

    assert to_markdown(html) == "- one\n- two\n  - deep"


def test_list_items_holding_paragraphs_stay_tight():
    assert to_markdown("<ul><li><p>one</p></li><li><p>two</p></li></ul>") == "- one\n- two"


def test_second_paragraph_of_an_item_lines_up_under_the_first():
    assert to_markdown("<ol><li><p>one</p><p>more</p></li><li>two</li></ol>") == (
        "1. one\n\n   more\n2. two"
    )


def test_line_break_inside_an_item_is_indented():
    assert to_markdown("<ul><li>one<br>more</li></ul>") == "- one\n  more"


def test_unclosed_list_items():
    assert to_markdown("<ul><li>one<li>two<li>three</ul>") == "- one\n- two\n- three"


def test_list_items_outside_any_list():
    assert to_markdown("<li>one</li><li>two</li><p>after</p>") == "- one\n- two\n\nafter"


def test_empty_list_items_leave_nothing():
    assert to_markdown("<ul><li></li><li> </li></ul><p>x</p>") == "x"


def test_formatting_inside_list_items():
    html = '<ul><li><b>one</b>: <a href="https://e.com">link</a></li></ul>'

    assert to_markdown(html) == "- **one**: [link](https://e.com)"


# Quotes, code blocks, tables


def test_blockquote_lines_are_prefixed():
    assert to_markdown("<blockquote>one<br>two</blockquote>") == "> one\n> two"


def test_blockquote_between_paragraphs():
    html = "<p>He said:</p><blockquote><p>one</p><p>two</p></blockquote><p>after</p>"

    assert to_markdown(html) == "He said:\n\n> one\n\n> two\n\nafter"


def test_nested_blockquotes_share_one_level():
    assert to_markdown("<blockquote>out<blockquote>in</blockquote>out</blockquote>x") == (
        "> out\n\n> in\n\n> out\n\nx"
    )


def test_list_inside_a_blockquote():
    assert to_markdown("<blockquote><ul><li>a<ul><li>b</li></ul></li></ul></blockquote>") == (
        "> - a\n>   - b"
    )


def test_pre_is_a_fenced_code_block():
    html = "<p>Run:</p><pre>def f():\n    return 1 &lt; 2\n</pre><p>done</p>"

    assert to_markdown(html) == "Run:\n\n```\ndef f():\n    return 1 < 2\n```\n\ndone"


def test_pre_keeps_indentation_and_ignores_marks():
    html = "<pre><code class='language-py'>if x:\n\t<b>y</b> = <span>2</span>\n</code></pre>"

    assert to_markdown(html) == "```\nif x:\n\ty = 2\n```"


def test_pre_with_br_and_line_elements():
    assert to_markdown("<pre>one<br>two</pre>") == "```\none\ntwo\n```"
    assert to_markdown("<pre><div>one</div><div>two</div></pre>") == "```\none\ntwo\n```"


def test_empty_pre_leaves_nothing():
    assert to_markdown("a<pre>  \n </pre>b") == "a\n\nb"


def test_pre_content_cannot_close_the_fence():
    out = to_markdown("<pre>a ``` b</pre>after")

    assert out.count("```") == 2
    assert out.endswith("```\n\nafter")


def test_unclosed_pre_is_still_fenced():
    assert to_markdown("<pre>code") == "```\ncode\n```"


def test_table_cells_and_rows():
    html = (
        "<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
        "<tbody><tr><td>Ann</td><td>3</td></tr><tr><td>Bob</td><td>5</td></tr></tbody></table>"
    )

    assert to_markdown(html) == "Name | Score\nAnn | 3\nBob | 5"


def test_table_is_set_apart_and_cells_stay_on_one_line():
    html = "<p>x</p><table><tr><td><p>a</p><p>b</p></td><td><b>c</b></td></tr></table><p>y</p>"

    assert to_markdown(html) == "x\n\na b | **c**\n\ny"


def test_table_with_empty_cells():
    assert to_markdown("<table><tr><td></td><td>a</td><td></td><td>b</td></tr></table>") == (
        "a | | b"
    )


# Dropped elements


@pytest.mark.parametrize(
    "html",
    [
        "a<script>alert('<b>x</b>')</script>b",
        "a<style>p { color: red }</style>b",
        "a<iframe src='https://e.com'>fallback</iframe>b",
        "a<svg><g><text>t</text><svg><text>u</text></svg></g></svg>b",
        "a<!-- a <b>comment</b> -->b",
        "a<img src='https://e.com/i.png' alt='alt text'>b",
        "a<video controls><source src='v.mp4'>no video</video>b",
        "a<noscript>enable js</noscript>b",
        "a<input type='text' value='v'><button>Go</button>b",
        "a<script src='x.js'/>b",
        "a<SCRIPT>x</SCRIPT>b",
    ],
)
def test_non_text_elements_are_dropped(html):
    assert to_markdown(html) == "ab"
    assert to_text(html) == "ab"


def test_head_is_dropped_even_when_unclosed():
    html = "<html><head><title>T</title><style>x{}</style><body><p>text</p></body></html>"

    assert to_markdown(html) == "text"
    assert to_markdown("<html><head><title>T</title></head><body>text</body></html>") == "text"


def test_doctype_and_processing_instructions_are_dropped():
    assert to_markdown("<!DOCTYPE html><?xml version='1.0'?><p>text</p>") == "text"


# Entities and whitespace


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>Tom &amp; Jerry</p>", "Tom & Jerry"),
        ("<p>&lt;b&gt; is bold</p>", "<b> is bold"),
        ("<p>&#8220;quoted&#8221; &#x2014; &hellip;</p>", "\u201cquoted\u201d \u2014 \u2026"),
        ("<p>caf&eacute; &copy; 2026</p>", "caf\u00e9 \u00a9 2026"),
        ("<p>a&nbsp;&nbsp;b&#160;c</p>", "a b c"),
        ("<p>&bogus; &amp</p>", "&bogus; &"),
        ("<p>x&#0;y&#xD800;z</p>", "x\ufffdy\ufffdz"),
    ],
)
def test_entities_are_decoded(html, expected):
    assert to_markdown(html) == expected


def test_whitespace_collapses_as_in_a_browser():
    html = "<p>\n  one\n  two   three\t four\r\n</p>\n\n<p>  five  </p>"

    assert to_markdown(html) == "one two three four\n\nfive"


def test_whitespace_between_inline_tags_is_one_space():
    assert to_markdown("<b>a</b>   \n <i>b</i><span> </span> <span> </span>c") == "**a** *b* c"


def test_no_spaces_around_line_breaks():
    assert to_markdown("<p>one  <br>  two <br/> </p>") == "one\ntwo"


@pytest.mark.parametrize(
    "html",
    [
        "<p>one</p>\n\n\n<p></p><br><br><div><div><p>two</p></div></div><br>",
        "<ul><li><p>a</p><p><br><br></p><p>b</p></li></ul><hr><hr><pre>\n\n\nx\n\n\n\ny\n\n</pre>",
        "<blockquote><p> a </p><br><br><br><p> b </p></blockquote>",
        "<table><tr><td> a </td><td> </td></tr></table> <b> x </b> ",
    ],
)
def test_output_whitespace_rules(html):
    out = to_markdown(html)

    assert out == out.strip()
    assert "\n\n\n" not in out
    assert not re.search(r"[ \t]\n", out)


# Plain text input


def test_empty_input():
    assert to_markdown("") == ""
    assert to_markdown("   \n ") == ""
    assert to_markdown("<p></p>") == ""


def test_plain_text_passes_through():
    assert to_markdown("Just a sentence.") == "Just a sentence."


def test_plain_text_with_entities():
    assert to_markdown("Fish &amp; chips &lt;3 for &pound;5 &#8211; today") == (
        "Fish & chips <3 for \u00a35 \u2013 today"
    )


def test_plain_text_keeps_its_own_line_breaks():
    text = "First line  \r\nsecond   line\n\n\n\nNew paragraph\n"

    assert to_markdown(text) == "First line\nsecond line\n\nNew paragraph"


# Broken HTML


def test_unclosed_bold_is_closed_at_the_end():
    assert to_markdown("<p>Some <b>bold text") == "Some **bold text**"


def test_unclosed_bold_carries_into_the_next_paragraph():
    assert to_markdown("<p>a <b>b</p><p>c</p>") == "a **b**\n\n**c**"


def test_stray_closing_tags_are_ignored():
    assert to_markdown("one</b> two</i></a></code> three</p></div></li></ul></td></tr>") == (
        "one two three"
    )
    assert to_markdown("</blockquote>text</pre></h2>") == "text"


def test_misnested_marks_come_out_properly_nested():
    assert to_markdown("<b>bold <i>both</b> italic</i>") == "**bold *both*** *italic*"


def test_unclosed_link():
    assert to_markdown('<p>see <a href="https://e.com">here</p><p>next</p>') == (
        "see [here](https://e.com)\n\n[next](https://e.com)"
    )


def test_unquoted_and_duplicate_attributes():
    assert to_markdown("<a href=https://e.com/a class=x href=https://e.com/b>t</a>") == (
        "[t](https://e.com/a)"
    )


def test_text_cut_off_inside_a_tag_does_not_raise():
    assert to_markdown('<p>text</p><a href="https://e.com') == "text"
    assert to_markdown("<p>text</p><") == "text\n\n<"


@pytest.mark.parametrize(
    "html",
    [
        "<",
        ">",
        "<>",
        "</>",
        "< p >",
        "<<<>>>",
        "&#;&#x;&#99999999999;&",
        "<a href=>x</a>",
        "<p <b>>x",
        "<!---->",
        "<!-->",
        "<![CDATA[ x ]]>",
        "<? x",
        "<!DOCTYPE",
        "<ol start='99999999999999999999'><li>x",
        "<td>cell</td></tr></table></table>",
        "<li></ul></ol></li>",
        "<pre><pre></pre>",
        "</pre></pre>",
        "\x00\x01\x02<b>\x00</b>\x7f",
        "\ud800<b>\udfff</b>",
        "<b>\U0001f600</b>",
        "<a href='https://e.com/\ud800'>x</a>",
        "<img><img src><img src=''><img width>",
        "PK\x03\x04\x14\x00\x08\x08<\xff\xfe\x00>\x1b[0m%PDF-1.7",
    ],
)
def test_junk_never_raises(html):
    for base in ("", BASE, "not a url", "http://["):
        assert isinstance(to_markdown(html, base), str)
        assert isinstance(first_image(html, base), str)
    assert isinstance(to_text(html), str)


def test_control_characters_and_lone_surrogates_are_removed():
    out = to_markdown("a\x00b\x08c<b>\ud83dd</b>\x7f")

    assert out == "abc**d**"
    out.encode("utf-8")


def test_binary_junk():
    junk = bytes(range(256)).decode("latin-1") * 200

    for convert in (to_markdown, to_text, first_image):
        convert(junk).encode("utf-8")


def test_wrong_types_give_an_empty_string():
    assert to_markdown(None) == ""  # type: ignore[arg-type]
    assert to_text(None) == ""  # type: ignore[arg-type]
    assert first_image(None) == ""  # type: ignore[arg-type]
    assert to_markdown(b"<b>bytes</b>") == "**bytes**"  # type: ignore[arg-type]


# Size and depth


def _timed(function, *args) -> tuple[str, float]:
    start = time.perf_counter()
    result = function(*args)
    return result, time.perf_counter() - start


def test_two_megabyte_input_finishes_quickly():
    paragraph = '<p>Hello <b>world</b> &amp; <a href="/x">friends</a>, some more words here.</p>\n'
    html = paragraph * (2 * 1024 * 1024 // len(paragraph) + 1)
    assert len(html) >= 2 * 1024 * 1024

    for function in (to_markdown, to_text, first_image):
        result, seconds = _timed(function, html)
        assert seconds < 5
        assert len(result) <= MAX_INPUT_CHARS

    out = to_markdown(html, BASE)
    assert out.startswith("Hello **world** & [friends](https://site.example/x), some more")
    assert out.endswith("here.")


def test_two_megabytes_of_plain_text():
    text = "word &amp; word " * (2 * 1024 * 1024 // 16)

    out, seconds = _timed(to_markdown, text)

    assert seconds < 5
    assert out.startswith("word & word word & word")


@pytest.mark.parametrize(
    "html",
    [
        "<" * 2_000_000,
        ">" * 2_000_000,
        "&" * 2_000_000,
        "<a " * 700_000,
        "</" * 1_000_000,
        "<!--" * 500_000,
        "<![CDATA[" * 250_000,
        '<a href="' * 250_000,
        "<b><i>" * 400_000,
        "<script>" * 250_000,
        "<pre>" * 400_000 + "x",
        "\x00\ufffd<\xff" * 500_000,
    ],
)
def test_huge_hostile_input_finishes_quickly(html):
    for function in (to_markdown, to_text, first_image):
        result, seconds = _timed(function, html)
        assert isinstance(result, str)
        assert seconds < 5


@pytest.mark.parametrize(
    "tag", ["div", "b", "i", "span", "a", "p", "blockquote", "ul", "table", "pre", "svg", "x-y"]
)
def test_five_thousand_deep_nesting(tag):
    html = f"<{tag}>" * 5000 + "deep" + f"</{tag}>" * 5000

    for function in (to_markdown, to_text, first_image):
        result, seconds = _timed(function, html)
        assert isinstance(result, str)
        assert seconds < 5

    if tag not in ("svg",):
        assert "deep" in to_markdown(html)


def test_five_thousand_deep_lists_stay_small():
    for html in ("<ul><li>item" * 5000, "<blockquote>quote" * 5000, "<ol><li><b><i>x" * 5000):
        out, seconds = _timed(to_markdown, html)

        assert seconds < 5
        assert len(out) < 200_000
        assert max(len(line) for line in out.split("\n")) < 100


def test_five_thousand_deep_nesting_keeps_its_formatting():
    html = "<div><b>" * 5000 + "deep" + "</b></div>" * 5000

    assert to_markdown(html) == "**deep**"


# Realistic feed HTML


def test_wordpress_post():
    html = """<p><img fetchpriority="high" decoding="async" class="alignnone size-large wp-image-12"
src="https://blog.example/wp-content/uploads/2026/10/hero-1024x576.jpg" alt="Hero" width="1024"
height="576" /></p>
<p>We&#8217;re excited to announce <strong>version 2.0</strong> of our app. Read the
<a href="https://blog.example/changelog/" target="_blank" rel="noopener">full changelog</a> or
<a href="/download">download it now</a>.</p>
<h2 class="wp-block-heading">What&#8217;s new</h2>
<ul class="wp-block-list">
<li>Faster <em>sync</em></li>
<li>Dark mode</li>
</ul>
<figure class="wp-block-image"><a href="https://blog.example/wp-content/uploads/shot.png"><img
src="https://blog.example/wp-content/uploads/shot-300x200.png" alt="" /></a>
<figcaption class="wp-element-caption">The new look</figcaption></figure>
<blockquote class="wp-block-quote"><p>Best release yet.</p><cite>A user</cite></blockquote>
<p>The post <a rel="nofollow" href="https://blog.example/v2/">Version 2.0</a> appeared first on
<a rel="nofollow" href="https://blog.example">Example Blog</a>.</p>
"""

    assert to_markdown(html, "https://blog.example/v2/") == (
        "We\u2019re excited to announce **version 2.0** of our app. Read the "
        "[full changelog](https://blog.example/changelog/) or "
        "[download it now](https://blog.example/download).\n"
        "\n"
        "**What\u2019s new**\n"
        "\n"
        "- Faster *sync*\n"
        "- Dark mode\n"
        "\n"
        "The new look\n"
        "\n"
        "> Best release yet.\n"
        "\n"
        "> A user\n"
        "\n"
        "The post [Version 2.0](https://blog.example/v2/) appeared first on "
        "[Example Blog](https://blog.example)."
    )
    assert first_image(html) == (
        "https://blog.example/wp-content/uploads/2026/10/hero-1024x576.jpg"
    )
    assert to_text(html).startswith("We\u2019re excited to announce version 2.0 of our app.")


def test_reddit_link_post():
    html = (
        '<table> <tr><td> <a href="https://www.reddit.com/r/python/comments/abc/title/"> '
        '<img src="https://b.thumbs.redditmedia.com/thumb.jpg" alt="Title" title="Title" /> </a> '
        '</td><td> &#32; submitted by &#32; <a href="https://www.reddit.com/user/someone"> '
        '/u/someone </a> <br/> <span><a href="https://example.com/article">[link]</a></span> '
        '&#32; <span><a href="https://www.reddit.com/r/python/comments/abc/title/">[comments]</a>'
        "</span> </td></tr></table>"
    )

    assert to_markdown(html) == (
        "submitted by [/u/someone](https://www.reddit.com/user/someone)\n"
        "[[link]](https://example.com/article) "
        "[[comments]](https://www.reddit.com/r/python/comments/abc/title/)"
    )
    assert first_image(html) == "https://b.thumbs.redditmedia.com/thumb.jpg"
    assert to_text(html) == "submitted by /u/someone [link] [comments]"


def test_reddit_self_post_with_a_table():
    html = (
        '<!-- SC_OFF --><div class="md"><p>Benchmarks for the new release:</p> <table><thead> '
        '<tr> <th align="left">Version</th> <th align="right">Time</th> </tr> </thead><tbody> '
        '<tr> <td align="left">3.12</td> <td align="right">1.00s</td> </tr> '
        '<tr> <td align="left"><strong>3.13</strong></td> <td align="right">0.91s</td> </tr> '
        "</tbody></table> <p>Thoughts?</p> </div><!-- SC_ON --> &#32; submitted by &#32; "
        '<a href="https://www.reddit.com/user/someone"> /u/someone </a>'
    )

    assert to_markdown(html) == (
        "Benchmarks for the new release:\n"
        "\n"
        "Version | Time\n"
        "3.12 | 1.00s\n"
        "**3.13** | 0.91s\n"
        "\n"
        "Thoughts?\n"
        "\n"
        "submitted by [/u/someone](https://www.reddit.com/user/someone)"
    )


def test_nested_list_from_a_changelog():
    html = """
<h3>Changed</h3>
<ol>
  <li>Parser
    <ul>
      <li>Handles <code>&lt;br&gt;</code> tags</li>
      <li>Drops <del>old</del> rules
        <ul><li>Even the deep ones</li></ul>
      </li>
    </ul>
  </li>
  <li>Docs</li>
</ol>
"""

    assert to_markdown(html) == (
        "**Changed**\n"
        "\n"
        "1. Parser\n"
        "  - Handles `<br>` tags\n"
        "  - Drops ~~old~~ rules\n"
        "    - Even the deep ones\n"
        "2. Docs"
    )


def test_plain_text_description_with_entities():
    html = "Q&amp;A with the R&amp;D team &#8212; rock &amp; roll, 5 &gt; 3"

    assert to_markdown(html) == "Q&A with the R&D team \u2014 rock & roll, 5 > 3"
    assert to_text(html) == "Q&A with the R&D team \u2014 rock & roll, 5 > 3"
    assert first_image(html) == ""


def test_description_with_an_unclosed_bold():
    html = "<p>Breaking: <b>servers are down</p><p>More soon.</p>"

    assert to_markdown(html) == "Breaking: **servers are down**\n\n**More soon.**"
    assert to_text(html) == "Breaking: servers are down More soon."


def test_description_with_a_stray_closing_tag():
    html = "<p>Fixed the bug</strong> in the parser.</div></p><p>Thanks!</p>"

    assert to_markdown(html) == "Fixed the bug in the parser.\n\nThanks!"


def test_description_with_a_script_in_the_middle():
    html = (
        "<p>Before the ad.</p>"
        '<script type="text/javascript">document.write("<p>ad</p>"); var a = 1 < 2;</script>'
        "<div class='ad'><iframe src='https://ads.example/frame'></iframe></div>"
        "<p>After the ad.</p>"
    )

    assert to_markdown(html) == "Before the ad.\n\nAfter the ad."
    assert to_text(html) == "Before the ad. After the ad."


def test_tracking_pixel_and_share_links_at_the_end():
    html = (
        "<p>Story text.</p>"
        '<a href="https://feeds.example/~ff/x?a=1"><img src="https://feeds.example/~ff/x?d=1" '
        'border="0"></a> '
        '<img src="https://feeds.example/~r/x/~4/abc" height="1" width="1" alt=""/>'
    )

    assert to_markdown(html) == "Story text."


def test_youtube_style_plain_description():
    text = "New video!\n\nChapters:\n0:00 Intro\n1:30 Demo\n\nhttps://example.com/more"

    assert to_markdown(text) == text


# to_text


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("Plain title", "Plain title"),
        ("  Spaced \n\t out  \r\n title ", "Spaced out title"),
        ("Tom &amp; Jerry&#8217;s &lt;b&gt;", "Tom & Jerry\u2019s <b>"),
        ("<b>Bold</b> and <i>italic</i> and <code>code</code>", "Bold and italic and code"),
        ("un<b>broken</b>word", "unbrokenword"),
        ('<a href="https://e.com">Link</a> text', "Link text"),
        ("<p>one</p><p>two</p>", "one two"),
        ("one<br>two<br/>three", "one two three"),
        ("<ul><li>a</li><li>b</li></ul>", "a b"),
        ("<table><tr><td>a</td><td>b</td></tr></table>", "a b"),
        ("<h1>Head</h1>body", "Head body"),
        ("a&nbsp;b", "a b"),
        ("<![CDATA[x]]>", ""),
        ("Jane Doe <jane@example.com>", "Jane Doe"),
        ("2 < 3 and 4 > 1", "2 < 3 and 4 > 1"),
        ("<b>unclosed", "unclosed"),
        ("stray</b> tag", "stray tag"),
        ("<pre>  code\n  here </pre>", "code here"),
    ],
)
def test_to_text(html, expected):
    assert to_text(html) == expected


def test_to_text_has_no_formatting_marks():
    html = '<h2><b>T</b>itle</h2> <a href="https://e.com">x</a> <s>y</s> <code>z</code>'

    assert to_text(html) == "Title x y z"


# first_image


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("", ""),
        ("no images here", ""),
        ("<p>no images here</p>", ""),
        ('<img src="https://e.com/a.png">', "https://e.com/a.png"),
        ('<IMG SRC="http://e.com/a.png" />', "http://e.com/a.png"),
        ("<img src=https://e.com/a.png>", "https://e.com/a.png"),
        (
            '<p>x <img alt="a" src="https://e.com/a.png?w=1&amp;h=2"> y</p>',
            "https://e.com/a.png?w=1&h=2",
        ),
        ('<img src="https://e.com/a.png"><img src="https://e.com/b.png">', "https://e.com/a.png"),
        ('<img src="https://e.com/a b.png">', "https://e.com/a%20b.png"),
        ('<img src="https://e.com/a_(1).png">', "https://e.com/a_(1).png"),
        ('<img src="//cdn.example/a.png">', "https://cdn.example/a.png"),
        ('<a href="https://e.com"><img src="https://e.com/a.png"></a>', "https://e.com/a.png"),
        ('<img src="/relative.png">', ""),
        ('<img alt="no source">', ""),
        ('<img src="">', ""),
        ("<img src>", ""),
        ('<img src="ftp://e.com/a.png">', ""),
        ('<img src="javascript:alert(1)">', ""),
        ('<img src="https://e.com/a.png"', ""),
    ],
)
def test_first_image(html, expected):
    assert first_image(html) == expected


def test_first_image_resolves_against_the_base_url():
    assert first_image('<img src="/img/a.png">', BASE) == "https://site.example/img/a.png"
    assert first_image('<img src="a.png">', BASE) == "https://site.example/blog/a.png"
    assert first_image('<img src="//cdn.example/a.png">', "http://site.example/") == (
        "http://cdn.example/a.png"
    )


@pytest.mark.parametrize(
    "pixel",
    [
        '<img src="https://t.example/p.gif" width="1" height="1">',
        '<img src="https://t.example/p.gif" width="1">',
        '<img src="https://t.example/p.gif" height="1">',
        '<img src="https://t.example/p.gif" height="0" width="0">',
        '<img src="https://t.example/p.gif" width=" 1px ">',
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">',
        '<img src="DATA:image/png;base64,AAAA">',
    ],
)
def test_first_image_skips_tracking_pixels_and_data_urls(pixel):
    assert first_image(pixel) == ""
    assert first_image(pixel + '<img src="https://e.com/real.jpg" width="100">') == (
        "https://e.com/real.jpg"
    )


def test_first_image_uses_the_lazy_source_when_src_is_a_placeholder():
    html = '<img src="data:image/gif;base64,R0lGOD" data-src="https://e.com/real.jpg">'

    assert first_image(html) == "https://e.com/real.jpg"


def test_first_image_ignores_images_in_scripts_and_comments():
    html = (
        "<script>document.write('<img src=\"https://e.com/s.png\">')</script>"
        '<!-- <img src="https://e.com/c.png"> -->'
        '<img src="https://e.com/real.png">'
    )

    assert first_image(html) == "https://e.com/real.png"
