from __future__ import annotations

import asyncio
import gzip
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from email.utils import formatdate

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer
from yarl import URL

from rssbot import fetch as fetch_module
from rssbot.fetch import (
    BAD_REDIRECT_MESSAGE,
    BAD_URL_MESSAGE,
    MAX_FEED_BYTES,
    REFUSED_MESSAGE,
    HttpFetcher,
    is_public_address,
)
from rssbot.ports import FetchError, FetchResult

FEED_BODY = b"<rss><channel><title>Example</title></channel></rss>"
ETAG = '"v1"'
LAST_MODIFIED = "Wed, 07 Oct 2026 10:00:00 GMT"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF\x40\x00\x00\x00WEBPVP8 " + b"\x00" * 64
IMAGES = {"png": PNG, "jpeg": JPEG, "gif": GIF, "webp": WEBP}


class MapResolver(AbstractResolver):
    """Resolves made-up hostnames to chosen addresses, and nothing else."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = mapping

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host not in self.mapping:
            raise OSError("unknown host")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in self.mapping[host]
        ]

    async def close(self) -> None:
        return None


class Site:
    """A local web server that counts the requests reaching it."""

    def __init__(self) -> None:
        self.hits = 0
        self.seen_headers: list[dict[str, str]] = []
        self.server: TestServer | None = None

    @property
    def port(self) -> int:
        assert self.server is not None and self.server.port is not None
        return self.server.port

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    @web.middleware
    async def count(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        self.hits += 1
        self.seen_headers.append(dict(request.headers))
        return await handler(request)


async def feed(request: web.Request) -> web.Response:
    headers = {"ETag": ETAG, "Last-Modified": LAST_MODIFIED}
    if request.headers.get("If-None-Match") == ETAG:
        return web.Response(status=304, headers=headers)
    return web.Response(body=FEED_BODY, headers=headers, content_type="application/rss+xml")


async def bare_304(request: web.Request) -> web.Response:
    return web.Response(status=304)


async def big_with_length(request: web.Request) -> web.Response:
    return web.Response(body=b"a" * (MAX_FEED_BYTES + 1))


async def exact_limit(request: web.Request) -> web.Response:
    return web.Response(body=b"a" * MAX_FEED_BYTES)


async def endless(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse()  # chunked, no Content-Length
    await response.prepare(request)
    try:
        while True:
            await response.write(b"a" * 65536)
    except (ConnectionError, asyncio.CancelledError):
        pass
    return response


async def bomb(request: web.Request) -> web.Response:
    # About 50 kB on the wire, 50 MB once inflated.
    body = gzip.compress(b"\x00" * (50 * 1024 * 1024))
    return web.Response(body=body, headers={"Content-Encoding": "gzip"})


async def gzipped(request: web.Request) -> web.Response:
    return web.Response(body=gzip.compress(FEED_BODY), headers={"Content-Encoding": "gzip"})


async def long_header(request: web.Request) -> web.Response:
    # Sites do send a Content-Security-Policy this long.
    headers = {"Content-Security-Policy": "default-src " + "a" * 20_000}
    return web.Response(body=FEED_BODY, headers=headers, content_type="application/rss+xml")


async def slow(request: web.Request) -> web.Response:
    await asyncio.sleep(30)
    return web.Response(body=FEED_BODY)


async def drop(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Length": "100000"})
    await response.prepare(request)
    await response.write(b"<rss>")
    assert request.transport is not None
    request.transport.close()
    return response


async def status(request: web.Request) -> web.Response:
    headers = {"Retry-After": request.query["wait"]} if "wait" in request.query else {}
    if "reset" in request.query:
        headers[request.query.get("header", "X-RateLimit-Reset")] = request.query["reset"]
    return web.Response(
        status=int(request.match_info["code"]), text="internal detail 10.1.2.3", headers=headers
    )


async def loop(request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "/loop"})


async def hop(request: web.Request) -> web.Response:
    left = int(request.match_info["left"])
    if left == 0:
        return web.Response(body=FEED_BODY)
    return web.Response(status=302, headers={"Location": f"../hop/{left - 1}"})


async def redirect_to(request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": request.query["url"]})


async def redirect_nowhere(request: web.Request) -> web.Response:
    return web.Response(status=302)


async def image(request: web.Request) -> web.Response:
    kind = request.match_info["kind"]
    declared = request.query.get("type", f"image/{kind}")
    return web.Response(body=IMAGES[kind], headers={"Content-Type": declared})


async def image_big(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Type": "image/png"})
    await response.prepare(request)
    await response.write(PNG + b"\x00" * 4096)
    return response


async def html_as_png(request: web.Request) -> web.Response:
    return web.Response(body=b"<html><script>alert(1)</script></html>", content_type="image/png")


async def svg(request: web.Request) -> web.Response:
    return web.Response(
        body=b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"
    )


@pytest.fixture
async def site() -> AsyncIterator[Site]:
    site = Site()
    app = web.Application(middlewares=[site.count])
    app.add_routes(
        [
            web.get("/feed", feed),
            web.get("/bare304", bare_304),
            web.get("/big", big_with_length),
            web.get("/exact", exact_limit),
            web.get("/endless", endless),
            web.get("/bomb", bomb),
            web.get("/gzipped", gzipped),
            web.get("/long-header", long_header),
            web.get("/slow", slow),
            web.get("/drop", drop),
            web.get("/status/{code}", status),
            web.get("/loop", loop),
            web.get("/hop/{left}", hop),
            web.get("/to", redirect_to),
            web.get("/nowhere", redirect_nowhere),
            web.get("/img/{kind}", image),
            web.get("/imgbig", image_big),
            web.get("/html.png", html_as_png),
            web.get("/pic.svg", svg),
            web.get("/files/{tail:.*}", image_big),
        ]
    )
    site.server = TestServer(app, host="127.0.0.1")
    await site.server.start_server()
    try:
        yield site
    finally:
        await site.server.close()


@pytest.fixture
async def open_fetcher() -> AsyncIterator[HttpFetcher]:
    """A fetcher that may reach the local test server."""
    async with HttpFetcher(allow_private=True, timeout_s=10) as fetcher:
        yield fetcher


@pytest.fixture
async def guarded() -> AsyncIterator[HttpFetcher]:
    """A fetcher as deployed by default, using the real OS resolver."""
    async with HttpFetcher(allow_private=False, timeout_s=10) as fetcher:
        yield fetcher


async def raw_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def assert_refused(error: FetchError) -> None:
    assert error.permanent
    assert str(error) == REFUSED_MESSAGE
    assert "private" in str(error)


# --- fetch: the happy path and conditional requests ---


async def test_fetch_returns_body_validators_and_url(site: Site, open_fetcher: HttpFetcher) -> None:
    result = await open_fetcher.fetch(site.url("/feed"))
    assert result == FetchResult(
        not_modified=False,
        body=FEED_BODY,
        etag=ETAG,
        last_modified=LAST_MODIFIED,
        url=site.url("/feed"),
    )
    assert "If-None-Match" not in site.seen_headers[0]
    assert site.seen_headers[0]["User-Agent"].startswith("discord-rss-bot")


async def test_conditional_fetch_gives_not_modified(site: Site, open_fetcher: HttpFetcher) -> None:
    result = await open_fetcher.fetch(site.url("/feed"), etag=ETAG, last_modified=LAST_MODIFIED)
    assert result.not_modified
    assert result.body == b""
    assert result.etag == ETAG
    assert result.last_modified == LAST_MODIFIED
    assert site.seen_headers[0]["If-None-Match"] == ETAG
    assert site.seen_headers[0]["If-Modified-Since"] == LAST_MODIFIED


async def test_not_modified_keeps_previous_validators_when_the_site_sends_none(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    result = await open_fetcher.fetch(site.url("/bare304"), etag='"old"', last_modified="then")
    assert result.not_modified
    assert (result.etag, result.last_modified) == ('"old"', "then")


async def test_unasked_304_is_an_error(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError, match="304"):
        await open_fetcher.fetch(site.url("/bare304"))


async def test_a_response_with_a_very_long_header_is_read(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    result = await open_fetcher.fetch(site.url("/long-header"))
    assert result.body == FEED_BODY


async def test_validator_with_a_line_break_is_not_sent(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    result = await open_fetcher.fetch(site.url("/feed"), etag='"x"\r\nX-Evil: 1')
    assert result.body == FEED_BODY
    assert "X-Evil" not in site.seen_headers[0]
    assert "If-None-Match" not in site.seen_headers[0]


@pytest.mark.parametrize("status_line", [b"200 OK", b"304 Not Modified"])
async def test_validators_that_are_not_text_are_dropped(
    open_fetcher: HttpFetcher, status_line: bytes
) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 " + status_line + b'\r\nETag: "caf\xe9"\r\n')
        writer.write(b"Last-Modified: \xff\xfe\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server, port = await raw_server(handler)
    async with server:
        result = await open_fetcher.fetch(f"http://127.0.0.1:{port}/", etag='"old"')
    if result.not_modified:
        assert (result.etag, result.last_modified) == ('"old"', None)
    else:
        assert (result.etag, result.last_modified) == (None, None)
    for value in (result.etag, result.last_modified):
        (value or "").encode()  # what the database will do with it


async def test_validator_that_is_not_text_is_not_sent(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    result = await open_fetcher.fetch(site.url("/feed"), etag='"caf\udce9"')
    assert result.body == FEED_BODY
    assert "If-None-Match" not in site.seen_headers[0]


async def test_gzip_body_is_decoded(site: Site, open_fetcher: HttpFetcher) -> None:
    assert (await open_fetcher.fetch(site.url("/gzipped"))).body == FEED_BODY


async def test_custom_user_agent(site: Site) -> None:
    async with HttpFetcher(allow_private=True, user_agent="custom/1") as fetcher:
        await fetcher.fetch(site.url("/feed"))
    assert site.seen_headers[0]["User-Agent"] == "custom/1"


async def test_environment_proxy_is_ignored(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    async with HttpFetcher(allow_private=True) as fetcher:
        assert (await fetcher.fetch(site.url("/feed"))).body == FEED_BODY


# --- fetch: limits and failures ---


async def test_body_of_exactly_the_limit_is_accepted(site: Site, open_fetcher: HttpFetcher) -> None:
    assert len((await open_fetcher.fetch(site.url("/exact"))).body) == MAX_FEED_BYTES


@pytest.mark.parametrize("path", ["/big", "/endless", "/bomb"])
async def test_oversize_body_is_refused(site: Site, open_fetcher: HttpFetcher, path: str) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url(path))
    assert str(caught.value) == "The feed is larger than 5 MB."
    assert not caught.value.permanent


async def test_slow_site_hits_the_time_limit(site: Site) -> None:
    async with HttpFetcher(allow_private=True, timeout_s=0.2) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(site.url("/slow"))
    assert str(caught.value) == "The site took too long to answer."
    assert not caught.value.permanent


async def test_endless_body_cannot_outlast_the_time_limit(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_module, "MAX_FEED_BYTES", 10**15)
    async with HttpFetcher(allow_private=True, timeout_s=0.3) as fetcher:
        with pytest.raises(FetchError, match="too long"):
            await fetcher.fetch(site.url("/endless"))


async def test_connection_dropped_mid_body(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url("/drop"))
    assert "lost" in str(caught.value)
    assert not caught.value.permanent


@pytest.mark.parametrize("code", [204, 400, 500, 502])
async def test_other_statuses_are_errors_naming_the_status(
    site: Site, open_fetcher: HttpFetcher, code: int
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url(f"/status/{code}"))
    assert str(caught.value) == f"The site answered with error {code}."
    assert "10.1.2.3" not in str(caught.value)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (401, "The site refused to let the bot read that address (error 401)."),
        (
            403,
            "The site is blocking automated readers such as this bot (error 403). "
            "The address may be fine, but the bot cannot read it.",
        ),
        (404, "The site has nothing at that address (error 404), so check the address."),
        (410, "The site says the feed at that address is gone (error 410), so check the address."),
        (429, "The site asked the bot to slow down (error 429)."),
        (503, "The site is temporarily unavailable (error 503)."),
    ],
)
async def test_common_statuses_are_explained_in_plain_words(
    site: Site, open_fetcher: HttpFetcher, code: int, message: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url(f"/status/{code}"))
    assert str(caught.value) == message
    assert not caught.value.permanent
    assert caught.value.retry_after is None


@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize(
    ("wait", "seconds"),
    [
        ("120", 120.0),
        ("0", 0.0),
        ("999999999999999999999", 21600.0),  # capped at six hours
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),  # in the past
    ],
)
async def test_retry_after_in_seconds_is_passed_on(
    site: Site, open_fetcher: HttpFetcher, code: int, wait: str, seconds: float | None
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url(f"/status/{code}"))
    assert caught.value.retry_after is None
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(str(URL(site.url(f"/status/{code}")).with_query(wait=wait)))
    assert caught.value.retry_after == seconds


async def test_retry_after_as_a_date_is_passed_on(site: Site, open_fetcher: HttpFetcher) -> None:
    for ahead, low, high in ((600, 590.0, 600.0), (10 * 86400, 21600.0, 21600.0)):
        wait = formatdate(time.time() + ahead, usegmt=True)
        with pytest.raises(FetchError) as caught:
            await open_fetcher.fetch(str(URL(site.url("/status/503")).with_query(wait=wait)))
        assert caught.value.retry_after is not None
        assert low <= caught.value.retry_after <= high


@pytest.mark.parametrize("header", ["X-RateLimit-Reset", "RateLimit-Reset"])
@pytest.mark.parametrize(
    ("reset", "seconds"),
    [
        ("31", 31.0),  # how Reddit says it
        ("31.5", 31.5),
        ("0", 0.0),
        ("999999", 21600.0),  # capped at six hours
        ("-5", None),
        ("soon", None),
        ("", None),
        ("1445412480", None),  # a Unix time in the past
    ],
)
async def test_a_rate_limit_reset_in_seconds_is_passed_on(
    site: Site, open_fetcher: HttpFetcher, header: str, reset: str, seconds: float | None
) -> None:
    url = URL(site.url("/status/429")).with_query(reset=reset, header=header)
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(str(url))
    assert caught.value.retry_after == seconds


async def test_a_rate_limit_reset_as_a_unix_time_is_passed_on(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    url = URL(site.url("/status/429")).with_query(reset=str(int(time.time()) + 600))
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(str(url))
    assert caught.value.retry_after is not None
    assert 590.0 <= caught.value.retry_after <= 600.0


async def test_retry_after_wins_over_a_rate_limit_reset(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    url = URL(site.url("/status/429")).with_query(wait="120", reset="31")
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(str(url))
    assert caught.value.retry_after == 120.0


@pytest.mark.parametrize(("code", "slow_down"), [(429, True), (503, False), (403, False)])
async def test_only_429_is_a_request_to_slow_down(
    site: Site, open_fetcher: HttpFetcher, code: int, slow_down: bool
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url(f"/status/{code}"))
    assert caught.value.slow_down is slow_down


async def test_headers_are_sent_in_the_order_browsers_use(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    # Reddit answers 403 when Accept-Encoding comes before Accept.
    await open_fetcher.fetch(site.url("/feed"), etag=ETAG)
    sent = [name for name in site.seen_headers[0] if name != "Host"]
    assert sent[:3] == ["User-Agent", "Accept", "Accept-Encoding"]
    assert site.seen_headers[0]["Accept-Encoding"] == "gzip, deflate"


async def test_retry_after_is_ignored_on_other_statuses(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url("/status/500") + "?wait=120")
    assert caught.value.retry_after is None


async def test_five_redirects_are_followed(site: Site, open_fetcher: HttpFetcher) -> None:
    result = await open_fetcher.fetch(site.url("/hop/5"))
    assert result.body == FEED_BODY
    assert result.url == site.url("/hop/0")
    assert site.hits == 6


async def test_six_redirects_are_too_many(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError, match="redirected too many times"):
        await open_fetcher.fetch(site.url("/hop/6"))
    assert site.hits == 6


async def test_redirect_loop_ends(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError, match="redirected too many times"):
        await open_fetcher.fetch(site.url("/loop"))
    assert site.hits == 6


async def test_redirect_without_a_destination(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError, match="redirect"):
        await open_fetcher.fetch(site.url("/nowhere"))


@pytest.mark.parametrize(
    "target", ["file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/"]
)
async def test_redirect_to_a_bad_url_is_refused(
    site: Site, open_fetcher: HttpFetcher, target: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url("/to") + "?url=" + target)
    assert caught.value.permanent
    assert site.hits == 1


async def test_garbage_instead_of_http(open_fetcher: HttpFetcher) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"\x00\xff garbage \r\n:::\r\n\x01\x02\r\n\r\n")
        await writer.drain()
        writer.close()

    server, port = await raw_server(handler)
    async with server:
        with pytest.raises(FetchError):
            await open_fetcher.fetch(f"http://127.0.0.1:{port}/")


async def test_garbage_headers(open_fetcher: HttpFetcher) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: banana\r\nX-\x00Bad: " + b"a" * 100000)
        writer.write(b"\r\n\r\nbody")
        await writer.drain()
        writer.close()

    server, port = await raw_server(handler)
    async with server:
        with pytest.raises(FetchError):
            await open_fetcher.fetch(f"http://127.0.0.1:{port}/")


async def test_connection_closed_without_an_answer(open_fetcher: HttpFetcher) -> None:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.close()

    server, port = await raw_server(handler)
    async with server:
        with pytest.raises(FetchError) as caught:
            await open_fetcher.fetch(f"http://127.0.0.1:{port}/")
    assert not caught.value.permanent


async def test_site_that_never_answers() -> None:
    release = asyncio.Event()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()
        writer.close()

    server, port = await raw_server(handler)
    async with server, HttpFetcher(allow_private=True, timeout_s=0.2) as fetcher:
        with pytest.raises(FetchError, match="too long"):
            await fetcher.fetch(f"http://127.0.0.1:{port}/")
        release.set()


async def test_nothing_listening(open_fetcher: HttpFetcher) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(f"http://127.0.0.1:{port}/")
    assert str(caught.value) == "The site could not be reached."
    assert "127.0.0.1" not in str(caught.value)


async def test_unknown_hostname() -> None:
    async with HttpFetcher(allow_private=False, resolver=MapResolver({})) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch("http://no-such-site.test/feed")
    assert str(caught.value) == "That address could not be found."
    assert not caught.value.permanent


async def test_cancellation_passes_through(site: Site, open_fetcher: HttpFetcher) -> None:
    task = asyncio.ensure_future(open_fetcher.fetch(site.url("/slow")))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_fetch_after_close_is_a_fetch_error(site: Site) -> None:
    fetcher = HttpFetcher(allow_private=True)
    await fetcher.fetch(site.url("/feed"))
    await fetcher.close()
    await fetcher.close()  # closing twice is fine
    with pytest.raises(FetchError):
        await fetcher.fetch(site.url("/feed"))


# --- URLs that are refused before anything is sent ---


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/feed",
        "//example.com/feed",
        "ftp://example.com/feed",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "data:text/plain,hello",
        "http://",
        "http:///feed",
        "http://:80/feed",
        "https://?x=1",
        "http://example.com:99999/",
        "http://example.com:port/",
        "http://exa mple.com/",
        "http://example.com/a b",
        "http://example.com/\r\nHost: evil",
        "http://example.com\\@127.0.0.1/",
        "http://[::1/",
        "http://example.com/" + "a" * 3000,
    ],
)
@pytest.mark.parametrize("allow_private", [True, False])
async def test_unusable_urls_are_permanent_errors(url: str, allow_private: bool) -> None:
    async with HttpFetcher(allow_private=allow_private, resolver=MapResolver({})) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(url)
        assert caught.value.permanent
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch_image(url)
        assert caught.value.permanent


@pytest.mark.parametrize("url", ["http://xn--/", "https://xn--.example/feed"])
@pytest.mark.parametrize("allow_private", [True, False])
async def test_host_that_is_not_a_valid_name_is_a_bad_address(
    url: str, allow_private: bool
) -> None:
    async with HttpFetcher(allow_private=allow_private, resolver=MapResolver({})) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(url)
    assert str(caught.value) == BAD_URL_MESSAGE
    assert caught.value.permanent


@pytest.mark.parametrize(
    "authority", ["user:pw@127.0.0.1", "user@127.0.0.1", ":pw@127.0.0.1", "@127.0.0.1"]
)
async def test_urls_with_credentials_are_refused(
    site: Site, open_fetcher: HttpFetcher, authority: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(f"http://{authority}:{site.port}/feed")
    assert caught.value.permanent
    assert "username or password" in str(caught.value)
    assert site.hits == 0


# --- the address decision ---


@pytest.mark.parametrize(
    "address",
    [
        "1.1.1.1",
        "8.8.8.8",
        "93.184.216.34",
        "100.63.255.255",  # just below carrier-grade NAT
        "100.128.0.0",  # just above it
        "172.15.255.255",
        "172.32.0.0",
        "169.253.255.255",
        "192.167.255.255",
        "223.255.255.254",
        "2606:4700:4700::1111",
        "2001:4860:4860::8888",
        "2a00:1450:4001:81b::200e",
        "::ffff:8.8.8.8",
        "::ffff:808:808",
        "2002:808:808::1",  # 6to4 around 8.8.8.8
        "64:ff9b::808:808",  # NAT64 around 8.8.8.8
    ],
)
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        # loopback
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        # private ranges
        "10.0.0.5",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "fc00::1",
        "fd12:3456:789a::1",
        # link-local, including cloud metadata
        "169.254.169.254",
        "169.254.0.1",
        "fe80::1",
        "fe80::1%eth0",
        "febf::1",
        # carrier-grade NAT
        "100.64.0.1",
        "100.127.255.255",
        # multicast
        "224.0.0.1",
        "239.255.255.250",
        "ff02::1",
        "ff0e::1",
        # reserved, unspecified, broadcast, "this network"
        "240.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "0.1.2.3",
        "::",
        # protocol assignments, benchmarking, documentation
        "192.0.0.8",
        "198.18.0.1",
        "192.0.2.1",
        "198.51.100.1",
        "203.0.113.5",
        "2001:db8::1",
        # other IPv6 that must not be reached
        "fec0::1",  # site-local; is_global says True on 3.13
        "100::1",  # discard-only
        "2001::1",  # Teredo
        "64:ff9b:1::a00:5",  # local-use NAT64
        # IPv4-mapped
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::ffff:10.0.0.5",
        "::ffff:169.254.169.254",
        "::ffff:100.64.0.1",
        "::ffff:224.0.0.1",
        "::ffff:0.0.0.0",
        # IPv4-compatible
        "::127.0.0.1",
        "::10.0.0.5",
        # 6to4 around a non-public address
        "2002:7f00:1::",
        "2002:a00:5::1",
        "2002:a9fe:a9fe::1",
        "2002:c0a8:101::1",
        "2002:6440:1::1",
        "2002:e000:1::1",
        # NAT64 around a non-public address
        "64:ff9b::7f00:1",
        "64:ff9b::a00:5",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b::c0a8:101",
        "64:ff9b::6440:1",
        "64:ff9b::e000:1",
        # not addresses at all
        "",
        "example.com",
        "localhost",
        "127.0.0.1:80",
        "2130706433",
        "0x7f.1",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("017700000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("127.0.0.1.", "127.0.0.1"),
        ("0", "0.0.0.0"),
        ("::1", "::1"),
        ("::ffff:7f00:1", "::ffff:127.0.0.1"),
        ("example.com", None),
        ("localhost", None),
        ("deadbeef", None),
    ],
)
def test_literal_ip_notations(host: str, expected: str | None) -> None:
    literal = fetch_module._literal_ip(host)
    assert (str(literal) if literal is not None else None) == expected


# --- the guard, end to end ---


async def test_local_server_is_reachable_when_private_is_allowed(site: Site) -> None:
    async with HttpFetcher(allow_private=True) as fetcher:
        assert (await fetcher.fetch(site.url("/feed"))).body == FEED_BODY
        assert (await fetcher.fetch(site.url("/feed", host="localhost"))).body == FEED_BODY
    assert site.hits == 2


async def test_local_server_is_refused_by_default(site: Site, guarded: HttpFetcher) -> None:
    with pytest.raises(FetchError) as caught:
        await guarded.fetch(site.url("/feed"))
    assert_refused(caught.value)
    with pytest.raises(FetchError) as caught:
        await guarded.fetch_image(site.url("/img/png"))
    assert_refused(caught.value)
    assert site.hits == 0


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "2130706433",
        "0x7f.1",
        "0x7f000001",
        "017700000001",
        "0177.0.0.1",
        "127.0.0.1.",
        "0",
        "0.0.0.0",
        "[::1]",
        "[0:0:0:0:0:0:0:1]",
        "[::ffff:127.0.0.1]",
        "[::ffff:7f00:1]",
        "[::127.0.0.1]",
        "[::]",
        "[2002:7f00:1::]",
        "[64:ff9b::7f00:1]",
        "１２７.0.0.1",  # full-width digits, folded to ASCII by the URL parser
        "127。0。0。1",  # ideographic full stops, likewise
        "localhost",
        "LOCALHOST.",
    ],
)
async def test_literal_and_local_hosts_are_refused(
    site: Site, guarded: HttpFetcher, host: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await guarded.fetch(f"http://{host}:{site.port}/feed")
    assert_refused(caught.value)
    assert site.hits == 0


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.1", "2130706433", "0x7f.1", "017700000001", "[::1]", "[::ffff:127.0.0.1]"],
)
async def test_connector_refuses_literals_without_the_url_check(
    site: Site, guarded: HttpFetcher, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Take the early URL check away. The resolver and the socket factory must hold alone.
    monkeypatch.setattr(fetch_module, "_literal_ip", lambda host: None)
    with pytest.raises(FetchError) as caught:
        await guarded.fetch(f"http://{host}:{site.port}/feed")
    assert caught.value.permanent
    assert site.hits == 0


async def test_socket_factory_refuses_when_the_resolver_is_not_guarded(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Take the first two layers away. The socket factory sees the address and refuses.
    monkeypatch.setattr(fetch_module, "_literal_ip", lambda host: None)
    monkeypatch.setattr(fetch_module, "_GuardedResolver", lambda inner, is_allowed: inner)
    resolver = MapResolver({"feeds.example.com": ["127.0.0.1"]})
    async with HttpFetcher(allow_private=False, resolver=resolver) as fetcher:
        for host in ("feeds.example.com", "127.0.0.1"):
            with pytest.raises(FetchError) as caught:
                await fetcher.fetch(site.url("/feed", host=host))
            assert_refused(caught.value)
    assert site.hits == 0


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["169.254.169.254"],
        ["192.168.1.1"],
        ["100.64.0.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["fe80::1"],
        ["fd00::1"],
        ["64:ff9b::7f00:1"],
        ["93.184.216.34", "127.0.0.1"],  # some public, one not: all refused
        ["127.0.0.1", "93.184.216.34"],
        ["2606:4700:4700::1111", "::1"],
    ],
)
async def test_hostname_resolving_to_a_non_public_address_is_refused(
    site: Site, addresses: list[str]
) -> None:
    resolver = MapResolver({"feeds.example.com": addresses})
    async with HttpFetcher(allow_private=False, resolver=resolver, timeout_s=10) as fetcher:
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(site.url("/feed", host="feeds.example.com"))
        assert_refused(caught.value)
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch_image(site.url("/img/png", host="feeds.example.com"))
        assert_refused(caught.value)
    assert site.hits == 0
    for address in addresses:
        assert address not in str(caught.value)


async def test_same_hostname_is_reachable_when_private_is_allowed(site: Site) -> None:
    resolver = MapResolver({"feeds.example.com": ["127.0.0.1"]})
    async with HttpFetcher(allow_private=True, resolver=resolver) as fetcher:
        result = await fetcher.fetch(site.url("/feed", host="feeds.example.com"))
    assert result.body == FEED_BODY


def allow_test_server(fetcher: HttpFetcher) -> None:
    """Keep the guard on, but let 127.0.0.1 stand in for one public site."""
    fetcher._is_allowed = lambda address: address == "127.0.0.1" or is_public_address(address)


async def test_guarded_fetcher_reaches_an_allowed_address(site: Site) -> None:
    resolver = MapResolver({"public.example.com": ["127.0.0.1"]})
    async with HttpFetcher(allow_private=False, resolver=resolver) as fetcher:
        allow_test_server(fetcher)
        result = await fetcher.fetch(site.url("/hop/2", host="public.example.com"))
        image = await fetcher.fetch_image(site.url("/img/png", host="public.example.com"))
    assert result.body == FEED_BODY
    assert image.data == PNG
    assert site.hits == 4


@pytest.mark.parametrize(
    "target",
    [
        "http://internal.example.com:{port}/feed",  # resolves to 10.0.0.5
        "http://loop6.example.com:{port}/feed",  # resolves to ::1
        "http://mapped.example.com:{port}/feed",  # resolves to ::ffff:127.0.0.1
        "http://mixed.example.com:{port}/feed",  # one allowed address, one not
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]:{port}/feed",
        "http://[::ffff:127.0.0.1]:{port}/feed",
        "http://2130706434:{port}/feed",  # 127.0.0.2; the test exemption covers only 127.0.0.1
        "http://0x7f.2:{port}/feed",
        "http://127.0.0.2:{port}/feed",
        "//192.168.1.1/",
    ],
)
async def test_redirect_from_an_allowed_address_to_a_refused_one(site: Site, target: str) -> None:
    resolver = MapResolver(
        {
            "public.example.com": ["127.0.0.1"],
            "internal.example.com": ["10.0.0.5"],
            "loop6.example.com": ["::1"],
            "mapped.example.com": ["::ffff:127.0.0.1"],
            "mixed.example.com": ["127.0.0.1", "10.0.0.5"],
        }
    )
    start = site.url("/to", host="public.example.com") + "?url=" + target.format(port=site.port)
    async with HttpFetcher(allow_private=False, resolver=resolver, timeout_s=10) as fetcher:
        allow_test_server(fetcher)
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(start)
        assert_refused(caught.value)
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch_image(start)
        assert_refused(caught.value)
    assert site.hits == 2  # only the two first hops


async def test_rebinding_resolver_cannot_swap_the_address(site: Site) -> None:
    """A name that answers public first and private later is refused when it turns private."""

    class Rebinding(MapResolver):
        def __init__(self) -> None:
            super().__init__({})
            self.calls = 0

        async def resolve(
            self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
        ) -> list[ResolveResult]:
            self.calls += 1
            self.mapping = {host: ["127.0.0.1"] if self.calls == 1 else ["10.0.0.5"]}
            return await super().resolve(host, port, family)

    resolver = Rebinding()
    async with HttpFetcher(allow_private=False, resolver=resolver) as fetcher:
        allow_test_server(fetcher)
        # One lookup per connection, and that lookup's answer is what gets dialled.
        result = await fetcher.fetch(site.url("/feed", host="rebind.example.com"))
        assert result.body == FEED_BODY
        assert resolver.calls == 1
        assert fetcher._session is not None and fetcher._session.connector is not None
        fetcher._session.connector.clear_dns_cache()  # type: ignore[attr-defined]
        await fetcher._session.connector.close()
        fetcher._session = None
        with pytest.raises(FetchError) as caught:
            await fetcher.fetch(site.url("/feed", host="rebind.example.com"))
        assert_refused(caught.value)
    assert site.hits == 1


# --- fetch_image ---


@pytest.mark.parametrize(
    ("kind", "content_type", "filename"),
    [
        ("png", "image/png", "image.png"),
        ("jpeg", "image/jpeg", "image.jpg"),
        ("gif", "image/gif", "image.gif"),
        ("webp", "image/webp", "image.webp"),
    ],
)
async def test_fetch_image_accepts_each_type(
    site: Site, open_fetcher: HttpFetcher, kind: str, content_type: str, filename: str
) -> None:
    image = await open_fetcher.fetch_image(site.url(f"/img/{kind}"))
    assert image.data == IMAGES[kind]
    assert image.content_type == content_type
    assert image.filename == filename


async def test_fetch_image_ignores_content_type_parameters_and_case(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    image = await open_fetcher.fetch_image(site.url("/img/png") + "?type=IMAGE/PNG;%20charset=x")
    assert image.content_type == "image/png"


async def test_fetch_image_goes_by_the_bytes_between_accepted_types(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    # A PNG served as image/jpeg is a common mistake; it is still a picture, and a PNG.
    image = await open_fetcher.fetch_image(site.url("/img/png") + "?type=image/jpeg")
    assert (image.content_type, image.filename) == ("image/png", "image.png")


@pytest.mark.parametrize(
    "path",
    [
        "/feed",  # application/rss+xml
        "/pic.svg",  # an image type that is not accepted
        "/img/png?type=text/html",
        "/img/png?type=application/octet-stream",
        "/img/png?type=image/png2",
        "/html.png",  # says image/png, is HTML
    ],
)
async def test_fetch_image_refuses_what_is_not_an_accepted_image(
    site: Site, open_fetcher: HttpFetcher, path: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch_image(site.url(path))
    assert str(caught.value) == "That is not a PNG, JPEG, GIF or WebP image."


@pytest.mark.parametrize("path", ["/img/png", "/imgbig"])  # with and without Content-Length
async def test_fetch_image_refuses_oversize(
    site: Site, open_fetcher: HttpFetcher, path: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch_image(site.url(path), max_bytes=32)
    assert str(caught.value) == "The image is larger than 32 bytes."


async def test_fetch_image_limit_message_and_default_limit(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch_image(site.url("/imgbig"), max_bytes=2048)
    assert str(caught.value) == "The image is larger than 2 kB."
    image = await open_fetcher.fetch_image(site.url("/imgbig"))
    assert len(image.data) == len(PNG) + 4096


async def test_fetch_image_filename_takes_nothing_from_the_url(
    site: Site, open_fetcher: HttpFetcher
) -> None:
    image = await open_fetcher.fetch_image(site.url("/files/..%2f..%2fetc%2fpasswd.exe"))
    assert image.filename == "image.png"
    image = await open_fetcher.fetch_image(site.url("/img/gif") + "?name=../../evil.exe")
    assert image.filename == "image.gif"


async def test_fetch_image_non_200(site: Site, open_fetcher: HttpFetcher) -> None:
    with pytest.raises(FetchError, match="error 404"):
        await open_fetcher.fetch_image(site.url("/status/404"))


async def test_fetch_image_follows_redirects(site: Site, open_fetcher: HttpFetcher) -> None:
    image = await open_fetcher.fetch_image(site.url("/to") + "?url=/img/webp")
    assert image.content_type == "image/webp"


# --- review fixes ---


async def test_fetch_reports_the_content_type(site: Site, open_fetcher: HttpFetcher) -> None:
    result = await open_fetcher.fetch(site.url("/feed"))
    assert result.content_type == "application/rss+xml"


@pytest.mark.parametrize(
    "target", ["file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/"]
)
async def test_redirect_to_a_bad_url_gets_the_redirect_message(
    site: Site, open_fetcher: HttpFetcher, target: str
) -> None:
    with pytest.raises(FetchError) as caught:
        await open_fetcher.fetch(site.url("/to") + "?url=" + target)
    assert str(caught.value) == BAD_REDIRECT_MESSAGE
    assert caught.value.permanent


@pytest.mark.parametrize("declared", ["image/jpg", "image/pjpeg", "IMAGE/JPG"])
async def test_fetch_image_accepts_jpeg_aliases(
    site: Site, open_fetcher: HttpFetcher, declared: str
) -> None:
    image = await open_fetcher.fetch_image(site.url("/img/jpeg") + "?type=" + declared)
    assert (image.content_type, image.filename) == ("image/jpeg", "image.jpg")
