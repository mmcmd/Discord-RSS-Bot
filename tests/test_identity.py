from __future__ import annotations

import asyncio

import pytest

from rssbot.identity import SiteIdentity, discover
from rssbot.models import ParsedFeed
from rssbot.ports import FetchError, FetchResult


class FakeFetcher:
    def __init__(
        self, body: str | bytes = b"", url: str | None = None, error: BaseException | None = None
    ) -> None:
        self.body = body.encode() if isinstance(body, str) else body
        self.url = url
        self.error = error
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return FetchResult(False, self.body, None, None, self.url or url)

    async def fetch_image(self, url: str, *, max_bytes: int = 0):  # pragma: no cover
        raise AssertionError("not used")


def feed(title: str = "Example", link: str = "https://example.com/", image: str = "") -> ParsedFeed:
    return ParsedFeed(title=title, link=link, image=image, items=())


def page(head: str, body: str = "<p>hi</p>") -> str:
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


FEED_URL = "https://example.com/feed.xml"


async def icon_of(head: str, **kwargs: str) -> str:
    fetcher = FakeFetcher(page(head), **kwargs)
    return (await discover(feed(), FEED_URL, fetcher)).icon


async def test_feed_image_used_without_fetching():
    fetcher = FakeFetcher(error=AssertionError("must not fetch"))
    got = await discover(feed(image="https://example.com/logo.png"), FEED_URL, fetcher)
    assert got == SiteIdentity("Example", "https://example.com/logo.png")
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "image", ["https://example.com/a.ico", "https://x.org/a.SVG?v=1", "/rel.png", "ftp://x/a.png"]
)
async def test_unusable_feed_image_falls_back_to_page(image):
    fetcher = FakeFetcher(page('<link rel="apple-touch-icon" href="/touch.png">'))
    got = await discover(feed(image=image), FEED_URL, fetcher)
    assert got.icon == "https://example.com/touch.png"
    assert fetcher.calls == ["https://example.com/"]


async def test_apple_touch_icon_beats_icon_and_og():
    head = (
        '<meta property="og:image" content="/og.png">'
        '<link rel="icon" type="image/png" href="/icon.png">'
        '<link rel="apple-touch-icon" href="/apple.png">'
    )
    assert await icon_of(head) == "https://example.com/apple.png"


async def test_apple_touch_icon_precomposed():
    assert await icon_of('<link rel="apple-touch-icon-precomposed" href="/p.png">') == (
        "https://example.com/p.png"
    )


async def test_icon_beats_og_image():
    head = (
        '<meta property="og:image" content="/og.png">'
        '<link rel="shortcut icon" type="image/png" href="/i">'
    )
    assert await icon_of(head) == "https://example.com/i"


@pytest.mark.parametrize("type_", ["image/png", "image/jpeg", "image/webp", "IMAGE/PNG; x=y"])
async def test_icon_accepted_by_type(type_):
    assert await icon_of(f'<link rel="icon" type="{type_}" href="/icon?v=2">') == (
        "https://example.com/icon?v=2"
    )


@pytest.mark.parametrize("name", ["a.png", "a.jpg", "a.jpeg", "a.webp", "A.PNG"])
async def test_icon_accepted_by_extension(name):
    assert await icon_of(f'<link rel="icon" href="/{name}">') == f"https://example.com/{name}"


async def test_icon_without_type_or_extension_is_ignored():
    assert await icon_of('<link rel="icon" href="/favicon">') == ""


async def test_og_image_when_no_icon_links():
    assert await icon_of('<meta property="og:image" content="/og.png">') == (
        "https://example.com/og.png"
    )


async def test_og_image_via_name_attribute():
    assert await icon_of('<meta name="og:image" content="/og.png">') == "https://example.com/og.png"


async def test_sizes_ordering_icons():
    head = (
        '<link rel="icon" sizes="16x16" href="/16.png">'
        '<link rel="icon" sizes="192x192" href="/192.png">'
        '<link rel="icon" sizes="32x32 64x64" href="/64.png">'
        '<link rel="icon" href="/none.png">'
    )
    assert await icon_of(head) == "https://example.com/192.png"


async def test_sizes_ordering_apple():
    head = (
        '<link rel="apple-touch-icon" sizes="120x120" href="/120.png">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/180.png">'
    )
    assert await icon_of(head) == "https://example.com/180.png"


async def test_equal_sizes_prefer_first():
    head = '<link rel="icon" href="/a.png"><link rel="icon" href="/b.png">'
    assert await icon_of(head) == "https://example.com/a.png"


async def test_largest_ignores_unusable_candidates():
    head = (
        '<link rel="apple-touch-icon" sizes="512x512" href="/big.svg">'
        '<link rel="apple-touch-icon" sizes="64x64" href="/small.png">'
    )
    assert await icon_of(head) == "https://example.com/small.png"


async def test_relative_href_resolved_against_final_url():
    head = '<link rel="apple-touch-icon" href="img/t.png">'
    got = await icon_of(head, url="https://blog.example.org/en/home/")
    assert got == "https://blog.example.org/en/home/img/t.png"


async def test_protocol_relative_and_absolute_hrefs():
    assert await icon_of('<link rel="icon" href="//cdn.example.net/i.png">') == (
        "https://cdn.example.net/i.png"
    )
    assert await icon_of('<link rel="icon" href="http://other.org/i.png">') == (
        "http://other.org/i.png"
    )


async def test_base_href_honoured():
    head = '<base href="https://static.example.com/assets/"><link rel="icon" href="i.png">'
    assert await icon_of(head) == "https://static.example.com/assets/i.png"


async def test_relative_base_href_resolved_against_final_url():
    head = '<base href="/assets/"><link rel="icon" href="i.png">'
    assert await icon_of(head, url="https://example.com/a/b") == "https://example.com/assets/i.png"


async def test_only_ico_gives_empty_icon():
    head = (
        '<link rel="icon" href="/favicon.ico">'
        '<link rel="shortcut icon" type="image/png" href="/f.ICO">'
    )
    assert await icon_of(head) == ""


async def test_svg_and_data_urls_ignored():
    head = (
        '<link rel="apple-touch-icon" href="data:image/png;base64,AAAA">'
        '<link rel="icon" type="image/png" href="/i.svg">'
        '<meta property="og:image" content="data:image/png;base64,AAAA">'
    )
    assert await icon_of(head) == ""


async def test_unusable_og_skipped_for_next_one():
    head = '<meta property="og:image" content="/a.svg"><meta property="og:image" content="/b.png">'
    assert await icon_of(head) == "https://example.com/b.png"


async def test_links_after_head_ignored():
    html = '<html><head></head><body><link rel="icon" href="/late.png"></body></html>'
    got = await discover(feed(), FEED_URL, FakeFetcher(html))
    assert got.icon == ""


async def test_broken_markup():
    html = (
        '<html><head><title>x<link rel="icon" href="/a.png"'
        '<meta property="og:image" content="/og.png"><<<>>> <link rel=icon href=/c.png sizes=9x'
    )
    got = await discover(feed(), FEED_URL, FakeFetcher(html))
    assert isinstance(got.icon, str)  # must not raise


async def test_unquoted_attributes_and_case():
    got = await discover(
        feed(), FEED_URL, FakeFetcher("<HEAD><LINK REL=ICON TYPE=image/png HREF=/x.png>")
    )
    assert got.icon == "https://example.com/x.png"


async def test_invalid_utf8_body():
    body = b'\xff\xfe<html><head><link rel="icon" href="/ok.png"></head>'
    got = await discover(feed(), FEED_URL, FakeFetcher(body))
    assert got.icon == "https://example.com/ok.png"


async def test_only_first_512_kb_scanned():
    padding = "<!--" + "x" * (512 * 1024) + "-->"
    html = padding + page('<link rel="icon" href="/a.png">')
    got = await discover(feed(), FEED_URL, FakeFetcher(html))
    assert got.icon == ""


async def test_binary_garbage_does_not_raise():
    got = await discover(feed(), FEED_URL, FakeFetcher(bytes(range(256)) * 100))
    assert got.icon == ""


async def test_home_page_is_feed_link():
    fetcher = FakeFetcher(page(""))
    parsed = feed(link="https://www.example.com/blog/")
    await discover(parsed, "https://feeds.example.net/x", fetcher)
    assert fetcher.calls == ["https://www.example.com/blog/"]


async def test_home_page_from_feed_url_when_no_link():
    fetcher = FakeFetcher(page(""))
    await discover(feed(link=""), "http://feeds.example.net:8080/a/b.xml?x=1", fetcher)
    assert fetcher.calls == ["http://feeds.example.net:8080/"]


async def test_fetch_error_gives_empty_icon():
    got = await discover(feed(), FEED_URL, FakeFetcher(error=FetchError("boom", permanent=True)))
    assert got == SiteIdentity("Example", "")


async def test_unexpected_exception_gives_empty_icon():
    got = await discover(feed(), FEED_URL, FakeFetcher(error=RuntimeError("bug")))
    assert got == SiteIdentity("Example", "")


async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await discover(feed(), FEED_URL, FakeFetcher(error=asyncio.CancelledError()))


async def test_not_modified_result_gives_empty_icon():
    class NotModified(FakeFetcher):
        async def fetch(self, url, *, etag=None, last_modified=None):
            return FetchResult(True, b"", None, None, url)

    got = await discover(feed(), FEED_URL, NotModified())
    assert got.icon == ""


@pytest.mark.parametrize("link, feed_url", [("", "not a url"), ("", ""), ("mailto:a@b.c", "ftp://x/y")])
async def test_no_usable_url_means_no_fetch(link, feed_url):
    fetcher = FakeFetcher(error=AssertionError("must not fetch"))
    got = await discover(feed(title="T", link=link), feed_url, fetcher)
    assert got == SiteIdentity("T", "")
    assert fetcher.calls == []


async def test_invalid_url_does_not_raise():
    got = await discover(feed(link="http://[bad"), "http://[also", FakeFetcher())
    assert got.icon == ""


async def test_name_is_feed_title():
    got = await discover(feed(title="  My Blog "), FEED_URL, FakeFetcher())
    assert got.name == "My Blog"


async def test_name_falls_back_to_link_host_without_www():
    got = await discover(feed(title="", link="https://www.example.org/x"), FEED_URL, FakeFetcher())
    assert got.name == "example.org"


async def test_name_falls_back_to_feed_url_host():
    got = await discover(
        feed(title=" ", link=""), "https://www.news.example.net/rss", FakeFetcher()
    )
    assert got.name == "news.example.net"


async def test_name_empty_when_nothing_known():
    got = await discover(feed(title="", link=""), "", FakeFetcher())
    assert got == SiteIdentity("", "")


async def test_name_keeps_other_subdomains():
    got = await discover(feed(title="", link="https://blog.example.org/"), FEED_URL, FakeFetcher())
    assert got.name == "blog.example.org"
