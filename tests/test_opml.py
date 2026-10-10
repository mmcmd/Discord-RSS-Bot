from __future__ import annotations

import pytest

from rssbot.opml import (
    MAX_OPML_BYTES,
    MAX_OPML_ENTRIES,
    OpmlEntry,
    OpmlError,
    build_opml,
    parse_opml,
)

NESTED = b"""<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Tech" title="Tech">
      <outline text="Ars" title="Ars Technica" type="rss"
               xmlUrl="https://feeds.arstechnica.com/arstechnica/index" htmlUrl="https://arstechnica.com"/>
      <outline text="Dev" title="Dev">
        <outline text="Blog" type="rss" xmlUrl="http://example.com/blog.xml"/>
      </outline>
    </outline>
    <outline text="Plain" type="rss" xmlUrl="https://example.org/feed"/>
    <outline text="Folder without feeds"/>
  </body>
</opml>
"""

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<opml version="2.0"><body>
<outline text="&lol3;" xmlUrl="https://example.com/feed"/>
</body></opml>
"""

HTML_PAGE = b"""<!DOCTYPE html>
<html><head><title>Login</title></head><body><p>Sign in<br>to continue</p></body></html>
"""


def opml(*outlines: str) -> bytes:
    return ("<opml version='2.0'><body>" + "".join(outlines) + "</body></opml>").encode()


def test_nested_folders_are_flattened_in_file_order() -> None:
    assert parse_opml(NESTED) == [
        OpmlEntry("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        OpmlEntry("Blog", "http://example.com/blog.xml"),
        OpmlEntry("Plain", "https://example.org/feed"),
    ]


@pytest.mark.parametrize("attr", ["xmlUrl", "xmlurl", "XMLURL", "XmlUrl"])
def test_url_attribute_is_case_insensitive(attr: str) -> None:
    data = opml(f'<outline text="A" {attr}="https://example.com/a"/>')
    assert parse_opml(data) == [OpmlEntry("A", "https://example.com/a")]


def test_title_falls_back_to_text_then_url() -> None:
    data = opml(
        '<outline title="T" text="X" xmlUrl="https://e.com/1"/>',
        '<outline text="X" xmlUrl="https://e.com/2"/>',
        '<outline title=" " text=" " xmlUrl="https://e.com/3"/>',
        '<outline xmlUrl="https://e.com/4"/>',
    )
    assert parse_opml(data) == [
        OpmlEntry("T", "https://e.com/1"),
        OpmlEntry("X", "https://e.com/2"),
        OpmlEntry("https://e.com/3", "https://e.com/3"),
        OpmlEntry("https://e.com/4", "https://e.com/4"),
    ]


def test_non_web_urls_are_dropped_and_urls_trimmed() -> None:
    data = opml(
        '<outline text="ftp" xmlUrl="ftp://example.com/feed"/>',
        '<outline text="js" xmlUrl="javascript:alert(1)"/>',
        '<outline text="file" xmlUrl="file:///etc/passwd"/>',
        '<outline text="bare" xmlUrl="example.com/feed"/>',
        '<outline text="nohost" xmlUrl="https://"/>',
        '<outline text="empty" xmlUrl=""/>',
        '<outline text="ok" xmlUrl="  HTTPS://Example.com/feed \n"/>',
    )
    assert parse_opml(data) == [OpmlEntry("ok", "HTTPS://Example.com/feed")]


def test_duplicates_keep_the_first() -> None:
    data = opml(
        '<outline text="First" xmlUrl="https://e.com/a"/>',
        '<outline text="Other" xmlUrl="https://e.com/b"/>',
        '<outline text="Second" xmlUrl=" https://e.com/a "/>',
    )
    assert parse_opml(data) == [
        OpmlEntry("First", "https://e.com/a"),
        OpmlEntry("Other", "https://e.com/b"),
    ]


def test_utf8_bom() -> None:
    data = b"\xef\xbb\xbf" + NESTED
    assert len(parse_opml(data)) == 3


def test_declared_latin1_encoding() -> None:
    data = (
        b'<?xml version="1.0" encoding="ISO-8859-1"?>'
        b'<opml><body><outline text="Caf\xe9" xmlUrl="https://e.com/f"/></body></opml>'
    )
    assert parse_opml(data) == [OpmlEntry("Café", "https://e.com/f")]


def test_declared_windows_1252_encoding() -> None:
    data = (
        b'<?xml version="1.0" encoding="windows-1252"?>'
        b'<opml><body><outline text="Say \x93hi\x94" xmlUrl="https://e.com/f"/></body></opml>'
    )
    assert parse_opml(data) == [OpmlEntry("Say “hi”", "https://e.com/f")]


def test_utf16_with_bom() -> None:
    text = '<?xml version="1.0" encoding="UTF-16"?><opml><body><outline text="Ünï" xmlUrl="https://e.com/f"/></body></opml>'
    assert parse_opml(text.encode("utf-16")) == [OpmlEntry("Ünï", "https://e.com/f")]


def test_billion_laughs_is_rejected() -> None:
    with pytest.raises(OpmlError, match="DOCTYPE"):
        parse_opml(BILLION_LAUGHS)


def test_entity_declaration_without_doctype_check_in_utf16_is_rejected() -> None:
    text = BILLION_LAUGHS.decode().replace('<?xml version="1.0"?>', "")
    with pytest.raises(OpmlError):
        parse_opml(text.encode("utf-16"))


def test_doctype_with_odd_spacing_and_case_is_rejected() -> None:
    data = b'<!doctype opml><opml><body><outline xmlUrl="https://e.com/f"/></body></opml>'
    with pytest.raises(OpmlError):
        parse_opml(data)


def test_html_page_is_rejected() -> None:
    with pytest.raises(OpmlError):
        parse_opml(HTML_PAGE)


def test_html_without_doctype_is_rejected() -> None:
    with pytest.raises(OpmlError):
        parse_opml(b"<html><body><p>Sign in<br>to continue</body></html>")


def test_well_formed_xml_that_is_not_opml_has_no_feeds() -> None:
    with pytest.raises(OpmlError, match="any feeds"):
        parse_opml(b"<html><body><p>hello</p></body></html>")


@pytest.mark.parametrize("data", [b"", b"   \n\t ", b"\xef\xbb\xbf"])
def test_empty_file_is_rejected(data: bytes) -> None:
    with pytest.raises(OpmlError, match="empty"):
        parse_opml(data)


def test_opml_without_feeds_is_rejected() -> None:
    with pytest.raises(OpmlError, match="any feeds"):
        parse_opml(opml('<outline text="Folder"/>'))


@pytest.mark.parametrize(
    "data",
    [
        b"not xml at all",
        b"<opml><body><outline xmlUrl='https://e.com/f'></body></opml>",
        b"\x00\x01\x02\xff",
    ],
)
def test_malformed_xml_is_rejected(data: bytes) -> None:
    with pytest.raises(OpmlError, match="not valid XML"):
        parse_opml(data)


def test_unknown_declared_encoding_is_rejected_cleanly() -> None:
    with pytest.raises(OpmlError):
        parse_opml(b'<?xml version="1.0" encoding="no-such-charset"?><opml/>')


def test_oversized_file_is_rejected() -> None:
    filler = b" " * (MAX_OPML_BYTES + 1)
    with pytest.raises(OpmlError, match="larger"):
        parse_opml(NESTED + filler)


def test_file_at_the_size_limit_is_accepted() -> None:
    pad = MAX_OPML_BYTES - len(NESTED)
    assert len(parse_opml(NESTED + b" " * pad)) == 3


def _many(count: int) -> bytes:
    return opml(*(f'<outline text="f{i}" xmlUrl="https://e.com/{i}"/>' for i in range(count)))


def test_entry_limit() -> None:
    assert len(parse_opml(_many(MAX_OPML_ENTRIES))) == MAX_OPML_ENTRIES
    with pytest.raises(OpmlError, match="too many"):
        parse_opml(_many(MAX_OPML_ENTRIES + 1))


def test_duplicates_do_not_count_towards_the_entry_limit() -> None:
    data = opml(*(['<outline xmlUrl="https://e.com/same"/>'] * (MAX_OPML_ENTRIES + 10)))
    assert parse_opml(data) == [OpmlEntry("https://e.com/same", "https://e.com/same")]


# ----- build_opml -----

NASTY = [
    OpmlEntry("Plain", "https://example.com/feed"),
    OpmlEntry('Fish & "Chips" <live>', "https://example.com/feed?a=1&b=2&c=<x>"),
    OpmlEntry('It\'s "quoted"', 'https://example.com/it\'s?q="x"'),
    OpmlEntry("Ünïcödé 日本語 🎉", "https://例え.jp/フィード?名=値"),
    OpmlEntry("&amp; &lt; already-escaped", "https://example.com/&amp;"),
    OpmlEntry("Tab\tand\nnewline", "https://example.com/multi"),
    OpmlEntry("<!DOCTYPE x> <!ENTITY y>", "https://example.com/doctype"),
]


def test_round_trip() -> None:
    assert parse_opml(build_opml(NASTY, "Backup & <more>")) == NASTY


def test_round_trip_at_the_entry_limit() -> None:
    entries = [OpmlEntry(f"Feed {i}", f"https://example.com/{i}") for i in range(MAX_OPML_ENTRIES)]
    assert parse_opml(build_opml(entries, "t")) == entries


def test_build_output_shape() -> None:
    out = build_opml([OpmlEntry("A & B", "https://e.com/f?x=1&y=2")], 'My "feeds"')
    text = out.decode("utf-8")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert '<opml version="2.0">' in text
    assert '<title>My "feeds"</title>' in text
    assert (
        '<outline type="rss" text="A &amp; B" title="A &amp; B" xmlUrl="https://e.com/f?x=1&amp;y=2"/>'
    ) in text


def test_build_output_is_well_formed_for_any_title() -> None:
    out = build_opml([OpmlEntry("a\x00b\x0bc\ud800d", "https://e.com/f")], "t\x01")
    assert parse_opml(out) == [OpmlEntry("abcd", "https://e.com/f")]


def test_build_with_no_entries_is_valid_xml_but_has_no_feeds() -> None:
    out = build_opml([], "Empty")
    assert out.startswith(b"<?xml")
    with pytest.raises(OpmlError, match="any feeds"):
        parse_opml(out)


# --- review fixes ---


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("feed://a.com/rss", "http://a.com/rss"),
        ("feed:https://b.com/rss", "https://b.com/rss"),
        ("feed:http://c.com/rss", "http://c.com/rss"),
        ("FEED://d.com/rss", "http://d.com/rss"),
    ],
)
def test_feed_scheme_urls_are_rewritten(given: str, expected: str) -> None:
    data = f'<opml><body><outline text="X" xmlUrl="{given}"/></body></opml>'.encode()
    assert parse_opml(data) == [OpmlEntry(title="X", url=expected)]


@pytest.mark.parametrize("encoding", ["gb2312", "gbk", "shift_jis", "euc-kr", "big5"])
def test_multibyte_encodings_are_decoded(encoding: str) -> None:
    text = {
        "gb2312": "中文",
        "gbk": "中文",
        "shift_jis": "日本語",
        "euc-kr": "한국어",
        "big5": "中文",
    }
    title = text[encoding]
    data = (
        f'<?xml version="1.0" encoding="{encoding}"?><opml><body>'
        f'<outline text="{title}" xmlUrl="https://a.com/rss"/></body></opml>'
    ).encode(encoding)
    assert parse_opml(data) == [OpmlEntry(title=title, url="https://a.com/rss")]
