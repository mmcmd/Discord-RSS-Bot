from __future__ import annotations

import random
from pathlib import Path

import pytest

from rssbot import parse as parse_module
from rssbot.models import Item, ParsedFeed
from rssbot.parse import MAX_ITEMS, MAX_TEXT_CHARS, ParseError, parse_feed

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
BASE = "https://base.example/dir/feed.xml"
JAN_2_2024 = 1704164645  # 2024-01-02T03:04:05Z

FEED_FIXTURES = sorted(path.name for path in FIXTURES.glob("*.xml"))


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def parsed(name: str, url: str = BASE) -> ParsedFeed:
    return parse_feed(load(name), url)


def by_title(feed: ParsedFeed) -> dict[str, Item]:
    return {item.title: item for item in feed.items}


def rss(*items: str, title: str = "T") -> bytes:
    body = f"<rss version='2.0'><channel><title>{title}</title>{''.join(items)}</channel></rss>"
    return body.encode()


# Formats


def test_rss20() -> None:
    feed = parsed("rss20.xml")
    assert feed.title == "Example & Co News"
    assert feed.link == "https://example.com/"
    assert feed.image == "https://example.com/logo.png"
    first, second = feed.items

    assert first.title == "First bold story"
    assert first.link == "https://example.com/posts/1"
    assert first.summary == "Hello **world** [more](https://base.example/more)"
    assert first.content == ""
    assert first.description == first.summary
    assert first.author == "Ann Author"
    assert first.published == JAN_2_2024
    assert first.categories == ("News", "Tech")
    assert first.image == "https://base.example/img/one.png"

    assert second.title == "Second story"
    assert second.summary == "Plain text summary"
    assert second.author == "Bob Writer"
    assert second.published == 1704272400  # 10:00 +0100
    assert second.categories == ()
    assert second.image == ""


def test_atom10() -> None:
    feed = parsed("atom10.xml")
    assert feed.title == "Atom Example"
    assert feed.link == "https://atom.example.org/"  # the alternate link, not rel="self"
    assert feed.image == "https://atom.example.org/favicon.ico"
    first, second = feed.items

    assert first.title == "Atom one"
    assert first.link == "https://atom.example.org/one"
    assert first.summary == "Short summary"
    assert first.content == "Full **content** here."
    assert first.author == "Carla"
    assert first.published == 1706601600  # published wins over updated
    assert first.categories == ("python", "rss")
    assert first.image == "https://atom.example.org/pic.jpg"

    assert second.title == "Generics: List<String> explained"  # type="text" is not HTML
    assert second.summary == "Only content"
    assert second.content == ""
    assert second.published == 1705276800  # falls back to updated


def test_rdf() -> None:
    feed = parsed("rdf.xml")
    assert feed.title == "RDF Example"
    assert feed.link == "https://rdf.example.net/"
    first, second = feed.items
    assert first.title == "RDF item A"
    assert first.link == "https://rdf.example.net/a"
    assert first.summary == "About A"
    assert first.author == "Dana"
    assert first.published == 1703442600
    assert first.categories == ("Holidays",)
    assert second.title == "RDF item B"
    assert second.published is None


def test_youtube_style_atom() -> None:
    feed = parsed("youtube.xml")
    assert feed.title == "Some Channel"
    assert feed.link == "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"
    (item,) = feed.items
    assert item.title == "A video title"
    assert item.link == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert item.author == "Some Channel"
    assert item.published == 1710082801
    assert item.summary.startswith("First line of the description.\nSecond line")
    assert item.content == ""
    # The Flash media:content is not an image, so the thumbnail is used.
    assert item.image == "https://i1.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


def test_empty_channel_is_a_valid_feed() -> None:
    feed = parsed("empty_channel.xml")
    assert feed == ParsedFeed(
        title="Nothing yet", link="https://empty.example.com/", image="", items=()
    )


def test_default_url_is_empty() -> None:
    feed = parse_feed(load("rss20.xml"))
    assert feed.items[0].link == "https://example.com/posts/1"
    assert feed.items[0].image == ""  # a relative image with nothing to resolve it against


# Summary and content


def test_content_encoded() -> None:
    items = by_title(parsed("content_encoded.xml"))

    both = items["Both texts"]
    assert both.summary == "The teaser."
    assert both.content == "**Heading**\n\nThe *full* article."
    assert both.image == "https://base.example/media/full.png"

    only = items["Content only"]  # a single text goes in the summary
    assert only.summary == "Just the body."
    assert only.content == ""

    same = items["Identical"]
    assert same.summary == "Same text."
    assert same.content == ""


def test_text_fields_are_capped() -> None:
    long = "word " * 10_000
    body = rss(f"<item><guid>1</guid><title>{long}</title><description>{long}</description></item>")
    (item,) = parse_feed(body).items
    assert len(item.summary) <= MAX_TEXT_CHARS
    assert len(item.title) <= MAX_TEXT_CHARS
    assert item.summary.startswith("word word")


# Images


def test_images() -> None:
    items = by_title(parsed("media.xml"))
    assert parsed("media.xml").image == "https://media.example.com/cover.jpg"
    assert items["media content with type"].image == "https://media.example.com/photo"
    assert items["media content by extension"].image == "https://base.example/pics/large.PNG?w=800"
    assert items["thumbnail only"].image == "https://media.example.com/t/abc123"
    assert items["image enclosure"].image == "https://media.example.com/enc.webp"
    assert items["itunes image"].image == "https://media.example.com/episode.jpg"
    assert items["inline image"].image == "https://media.example.com/inline.png"
    assert items["unusable schemes"].image == ""
    assert items["no image"].image == ""


# Links


def test_relative_links_resolve_against_the_feed_url() -> None:
    feed = parsed("relative_links.xml")
    assert feed.link == "https://base.example/"
    assert feed.image == "https://base.example/static/logo.png"
    rooted, sibling = feed.items
    assert rooted.link == "https://base.example/posts/rooted"
    assert rooted.image == "https://base.example/dir/thumbs/rooted.jpg"
    assert rooted.summary == "[About](https://base.example/about)"
    assert sibling.link == "https://base.example/dir/sibling.html"
    assert sibling.image == "https://cdn.example.com/s.png"


def test_relative_links_without_a_feed_url_are_dropped() -> None:
    feed = parsed("relative_links.xml", url="")
    assert feed.link == ""
    assert feed.image == ""
    assert [item.link for item in feed.items] == ["", ""]


def test_missing_titles_and_links() -> None:
    feed = parsed("missing_fields.xml")
    assert feed.link == ""
    # The two entries with nothing to identify them are skipped; their neighbours stay.
    assert [(item.title, item.link) for item in feed.items] == [
        ("", "https://missing.example.com/untitled"),
        ("No link here", ""),
        ("Odd link", ""),  # javascript: is not a link
        ("Last one", "https://missing.example.com/last"),
    ]
    assert feed.items[0].summary == "No title here"


# Dates


def test_dates() -> None:
    items = by_title(parsed("dates.xml"))
    assert len(items) == 9
    assert items["rfc822"].published == JAN_2_2024
    assert items["iso with offset"].published == JAN_2_2024
    assert items["updated only"].published == JAN_2_2024
    assert items["garbage published, good updated"].published == JAN_2_2024
    assert items["date only"].published == 1704153600
    for title in ("garbage", "missing", "year one", "empty"):
        assert items[title].published is None, title


# Keys


def test_keys_are_32_hex_characters_and_unique() -> None:
    for name in FEED_FIXTURES:
        keys = [item.key for item in parsed(name).items]
        assert len(set(keys)) == len(keys), name
        for key in keys:
            assert len(key) == 32, name
            assert set(key) <= set("0123456789abcdef"), name


@pytest.mark.parametrize("name", FEED_FIXTURES)
def test_keys_are_identical_across_parses(name: str) -> None:
    first = parsed(name)
    assert first == parsed(name)
    # The Feed's address may change (a redirect); the keys must not.
    moved = parsed(name, url="https://elsewhere.example/other/path.rss")
    assert [item.key for item in moved.items] == [item.key for item in first.items]
    assert [item.key for item in parsed(name, url="").items] == [i.key for i in first.items]


def test_keys_do_not_change_when_an_unrelated_entry_is_added() -> None:
    old = [
        "<item><guid>42</guid><title>With guid</title><link>/a</link></item>",
        "<item><title>With link</title><link>https://e.example/b</link></item>",
        "<item><title>Dated</title><pubDate>Mon, 05 Feb 2024 09:00:00 GMT</pubDate></item>",
        "<item><title>Texty</title><description>Some text</description></item>",
    ]
    new = "<item><guid>99</guid><title>Brand new</title><link>https://e.example/n</link></item>"
    before = parse_feed(rss(*old), BASE)
    after = parse_feed(rss(new, *old), BASE)
    middle = parse_feed(rss(*old[:2], new, *old[2:]), BASE)

    keys = [item.key for item in before.items]
    assert len(keys) == 4
    assert [item.key for item in after.items][1:] == keys
    assert [item.key for item in middle.items if item.title != "Brand new"] == keys
    assert after.items[0].key not in keys


def test_key_follows_the_guid_not_the_rest() -> None:
    one = parse_feed(rss("<item><guid>g</guid><title>Old title</title><link>/1</link></item>"))
    two = parse_feed(rss("<item><guid>g</guid><title>New title</title><link>/2</link></item>"))
    assert one.items[0].key == two.items[0].key


def test_no_guids() -> None:
    feed = parsed("no_guids.xml")
    assert [item.title for item in feed.items] == [
        "Has a link",
        "Title and date",
        "Title and date",  # same title and text, another date: another Item
        "Title and summary",
        "",
    ]
    assert len({item.key for item in feed.items}) == 5


def test_duplicate_guids_keep_the_first() -> None:
    feed = parsed("duplicate_guids.xml")
    assert [item.title for item in feed.items] == ["Original", "Other"]


# Broken feeds


def test_truncated_feed_keeps_what_it_has() -> None:
    feed = parsed("truncated.xml")
    assert feed.title == "Cut short"
    assert [item.link for item in feed.items] == [
        "https://cut.example.com/1",
        "https://cut.example.com/2",
        "https://cut.example.com/3",
    ]
    assert feed.items[0].title == "Whole one"
    # The complete entries have the same keys as they would in the whole feed.
    whole = parse_feed(load("truncated.xml") + b"em</title></item></channel></rss>", BASE)
    assert [item.key for item in whole.items] == [item.key for item in feed.items]


def test_wrong_declared_encoding() -> None:
    feed = parsed("wrong_encoding.xml")  # says us-ascii, is UTF-8
    assert feed.title == "Café Zürich"
    (item,) = feed.items
    assert item.title == "Crème brûlée — naïve"
    assert item.summary == "日本語 and “quotes”"


def test_latin1_bytes_declared_as_utf8() -> None:
    text = (
        '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Café</title>'
        "<item><title>naïve</title><link>https://e.example/1</link></item></channel></rss>"
    )
    feed = parse_feed(text.encode("latin-1"), BASE)
    assert feed.title == "Café"
    assert feed.items[0].title == "naïve"


def test_feed_without_a_root_element_but_with_a_title_parses() -> None:
    feed = parse_feed(b"<channel><title>Bare</title></channel>")
    assert feed.title == "Bare"
    assert feed.items == ()


def test_one_unbuildable_entry_does_not_lose_its_neighbours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = parse_module.to_markdown

    def fragile(html: str, base_url: str = "") -> str:
        if "boom" in html:
            raise RuntimeError("cannot convert")
        return real(html, base_url)

    monkeypatch.setattr(parse_module, "to_markdown", fragile)
    body = rss(
        "<item><guid>1</guid><title>Before</title><description>fine</description></item>",
        "<item><guid>2</guid><title>Broken</title><description>boom</description></item>",
        "<item><guid>3</guid><title>After</title><description>fine too</description></item>",
    )
    assert [item.title for item in parse_feed(body, BASE).items] == ["Before", "After"]


def test_entries_of_the_wrong_shape_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    good = {"id": "1", "title": "Good", "tags": "nonsense", "content": 7, "published_parsed": (1,)}

    def fake_parse(*args: object, **kwargs: object) -> dict[str, object]:
        return {"version": "rss20", "feed": None, "entries": [None, 3, good, {"id": 5}]}

    monkeypatch.setattr(parse_module.feedparser, "parse", fake_parse)
    feed = parse_feed(b"<rss/>")
    assert [item.title for item in feed.items] == ["Good"]
    assert feed.items[0].published is None
    assert feed.items[0].categories == ()


# Not feeds


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(load("page.html"), id="html"),
        pytest.param(load("data.json"), id="json"),
        pytest.param(b"", id="empty"),
        pytest.param(b" \r\n\t ", id="whitespace"),
        pytest.param(random.Random(7).randbytes(4096), id="random"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4, id="png"),
        pytest.param(b"\x00" * 512, id="nulls"),
        pytest.param(b"Just some text, nothing more.", id="text"),
        pytest.param(b'<?xml version="1.0"?><root><a>1</a></root>', id="other-xml"),
        pytest.param(b"<html><body><item><title>z</title></item></body></html>", id="html-item"),
    ],
)
def test_not_a_feed(body: bytes) -> None:
    with pytest.raises(ParseError) as caught:
        parse_feed(body, BASE)
    message = str(caught.value)
    assert message.endswith(".")
    assert "\n" not in message
    assert message.count(".") == 1  # one plain sentence


def test_only_parse_error_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RecursionError("deep")

    monkeypatch.setattr(parse_module.feedparser, "parse", explode)
    with pytest.raises(ParseError):
        parse_feed(b"<rss/>")


def test_arbitrary_bytes_never_raise_anything_else() -> None:
    rng = random.Random(1234)
    seeds = [load(name) for name in FEED_FIXTURES]
    for _ in range(150):
        data = bytearray(rng.choice(seeds))
        for _ in range(rng.randint(1, 12)):  # flip, cut and splice
            position = rng.randrange(len(data))
            action = rng.randrange(3)
            if action == 0:
                data[position] = rng.randrange(256)
            elif action == 1:
                del data[position : position + rng.randint(1, 40)]
            else:
                data[position:position] = rng.randbytes(rng.randint(1, 8))
        try:
            feed = parse_feed(bytes(data), BASE)
        except ParseError:
            continue
        assert isinstance(feed, ParsedFeed)
        for item in feed.items:
            assert item.key
            assert item.link == "" or item.link.startswith(("http://", "https://"))
            assert item.image == "" or item.image.startswith(("http://", "https://"))


# Bodies built to cost the parser time or memory, and ordinary ones that look like them

REFUSED = "That feed is built in a way this bot refuses to read."
XXE_EXAMPLE = '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'


def test_an_entity_declaration_quoted_in_an_item_is_only_text() -> None:
    body = rss(
        f"<item><guid>1</guid><description><![CDATA[<pre>{XXE_EXAMPLE}</pre>]]></description>"
        "</item>",
        "<item><guid>2</guid><description>&lt;!ENTITY a &quot;b&quot;&gt;</description></item>",
        "<item><guid>3</guid><!-- <!ENTITY c 'd'> --><title>Third</title></item>",
    )
    assert len(parse_feed(body, BASE).items) == 3


def test_a_long_utf16_feed_parses() -> None:
    items = "".join(
        f"<item><guid>g{n}</guid><title>Item {n}</title><link>https://example.com/{n}</link>"
        f"<description>Text<br/>{n}</description></item>"
        for n in range(400)
    )
    text = (
        '<?xml version="1.0" encoding="utf-16"?>'
        f'<rss version="2.0"><channel><title>T</title>{items}</channel></rss>'
    )
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be"):
        feed = parse_feed(text.replace("utf-16", encoding).encode(encoding), BASE)
        assert len(feed.items) == 400, encoding
        assert feed.items[399].title == "Item 399"


def test_unclosed_html_tags_in_a_description_are_not_nesting() -> None:
    body = rss("<item><guid>1</guid><description>" + "line<br>" * 2500 + "</description></item>")
    (item,) = parse_feed(body, BASE).items
    assert item.summary.startswith("line")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<rss><channel>" + b"<a>" * 20_000, id="unclosed"),
        pytest.param(
            b"<rss><channel>" + b"<a>" * 20_000 + b"</a>" * 20_000 + b"</channel></rss>",
            id="well-formed",
        ),
        pytest.param(b"</a>" * 20_000 + b"<rss><channel>" + b"<a>" * 20_000, id="closed-first"),
        pytest.param(("<rss><channel>" + "<a>" * 20_000).encode("utf-16"), id="utf-16"),
    ],
)
def test_very_deep_nesting_is_refused(body: bytes) -> None:
    with pytest.raises(ParseError) as caught:
        parse_feed(body, BASE)
    assert str(caught.value) == REFUSED


def _entity_bomb(prolog: str) -> str:
    return f'{prolog}<rss version="2.0"><channel><title>&a;&a;&a;&a;</title></channel></rss>'


BILLION_LAUGHS = (
    '<!DOCTYPE r [<!ENTITY a0 "ha">'
    + "".join(f'<!ENTITY a{n} "{f"&a{n - 1};" * 10}">' for n in range(1, 10))
    + '<!ENTITY a "&a9;">]>'
)
BIG = '<!ENTITY a "' + "a" * 1000 + '">'


@pytest.mark.parametrize(
    "prolog",
    [
        pytest.param(BILLION_LAUGHS, id="billion-laughs"),
        pytest.param(f"<!DOCTYPE r [{BIG}]>", id="one-big-entity"),
        pytest.param(f"<!DOCTYPE r [\n<!-- <a> -->\n{BIG}\n]>", id="after-a-comment"),
        # Three shapes that feedparser's rewriting of the DOCTYPE turns into real declarations.
        pytest.param(f"\n{BIG}\n<!DOCTYPE r>", id="outside-the-doctype"),
        pytest.param(f"oops\n{BIG}\n<!DOCTYPE r>", id="after-stray-text"),
        pytest.param(f"\n<!ENTITY b>\n<!DOCTYPE r [<!-- <a -->{BIG}]>", id="behind-a-false-start"),
        pytest.param('<!DOCTYPE r [<!ENTITY % p "x">]>', id="parameter-entity"),
        pytest.param(XXE_EXAMPLE, id="external"),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "cp037"])
def test_entity_declarations_before_the_root_are_refused(prolog: str, encoding: str) -> None:
    text = f'<?xml version="1.0" encoding="{encoding}"?>' + _entity_bomb(prolog)
    with pytest.raises(ParseError) as caught:
        parse_feed(text.encode(encoding), BASE)
    assert str(caught.value) == REFUSED


@pytest.mark.parametrize(
    "prolog",
    [
        pytest.param(f"<!DOCTYPE r>\n<!--\n{BIG}\n-->", id="inside-a-comment"),
        pytest.param(f"<!DOCTYPE b><!DOCTYPE r [<!-- <a -->{BIG}]>", id="second-doctype"),
    ],
)
def test_entity_text_that_declares_nothing_is_not_expanded(prolog: str) -> None:
    body = ('<?xml version="1.0"?>' + _entity_bomb(prolog)).encode()
    try:
        title = parse_feed(body, BASE).title
    except ParseError:
        return  # refusing it is as good
    assert len(title) < 100


def test_a_doctype_without_entities_is_fine() -> None:
    body = _entity_bomb('<!DOCTYPE rss PUBLIC "-//Netscape//DTD RSS 0.91//EN" "x.dtd">')
    assert parse_feed(body.replace("&a;", "T").encode(), BASE).title == "TTTT"


# More Items than are kept


def _listing(dates: list[str | None]) -> bytes:
    return rss(
        *(
            f"<item><guid>g{n}</guid>" + (f"<pubDate>{date}</pubDate>" if date else "") + "</item>"
            for n, date in enumerate(dates)
        )
    )


def _day(n: int) -> str:
    return f"{2000 + n // 300}-{1 + n % 300 // 25:02}-{1 + n % 25:02}T00:00:00Z"


def _guids(feed: ParsedFeed) -> list[int]:
    keys = {parse_feed(rss(f"<item><guid>g{n}</guid></item>")).items[0].key: n for n in range(700)}
    return [keys[item.key] for item in feed.items]


def test_a_long_listing_oldest_first_keeps_its_newest_items() -> None:
    kept = _guids(parse_feed(_listing([_day(n) for n in range(700)])))
    assert kept == list(range(700 - MAX_ITEMS, 700))  # still in the order the source used


def test_a_long_listing_newest_first_keeps_its_first_items() -> None:
    kept = _guids(parse_feed(_listing([_day(700 - n) for n in range(700)])))
    assert kept == list(range(MAX_ITEMS))


def test_a_long_listing_without_dates_keeps_its_first_items() -> None:
    kept = _guids(parse_feed(_listing([None] * 700)))
    assert kept == list(range(MAX_ITEMS))


def test_undated_items_in_a_long_listing_stay_with_their_neighbours() -> None:
    dates: list[str | None] = [_day(n) for n in range(700)]
    dates[0] = dates[100] = dates[650] = dates[699] = None
    kept = _guids(parse_feed(_listing(dates)))
    assert kept == list(range(700 - MAX_ITEMS, 700))


def test_a_file_name_as_body_is_not_opened() -> None:
    with pytest.raises(ParseError):
        parse_feed(str(FIXTURES / "rss20.xml").encode(), BASE)
