"""HTTP fetching for feeds, pages and images, with a guard against internal addresses.

Managers type in feed URLs and feeds carry image URLs, so every URL here is
untrusted. Unless the Deployer allows private addresses, the bot must only ever
connect to the public internet, however the address is spelled or reached.

The guard has three layers, all using `is_public_address`:

1. `_parse_url` refuses literal IP hosts, in any notation, before anything is sent.
   This is the friendly early answer, not the enforcement.
2. `_GuardedResolver` wraps the resolver the connector uses, so the addresses that
   are checked are the addresses that are connected to. There is no second lookup
   for DNS rebinding to slip into.
3. `HttpFetcher._open_socket` is the connector's socket factory. Every outgoing
   socket is created there from the exact address it is about to connect to, so
   this holds even for hosts the connector decides are literals and never resolves.

Redirects are followed by hand, one request per hop, so each hop goes through all
three layers again.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from yarl import URL  # aiohttp's own URL type: parse once, with the parser that will be used

from .journal import quote
from .logsetup import site
from .models import MAX_COVER_IMAGE_BYTES
from .ports import FetchError, FetchResult, ImageData

log = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_S = 30.0
MAX_URL_LENGTH = 2048
MAX_RETRY_AFTER_S = 6 * 60 * 60.0
DEFAULT_USER_AGENT = "discord-rss-bot/0.1 (+https://github.com/mmcmd/Discord-RSS-Bot)"

REFUSED_MESSAGE = (
    "That address is private or internal. "
    "This bot is set up to fetch only from the public internet."
)
BAD_URL_MESSAGE = (
    "That is not a web address the bot can use. It must start with http:// or https://."
)
CREDENTIALS_MESSAGE = "Addresses with a username or password in them are not allowed."
TIMEOUT_MESSAGE = "The site took too long to answer."
NOT_FOUND_MESSAGE = "That address could not be found."
UNREACHABLE_MESSAGE = "The site could not be reached."
TLS_MESSAGE = "A secure connection to the site could not be made."
LOST_MESSAGE = "The connection to the site was lost before it finished answering."
UNREADABLE_MESSAGE = "The site sent an answer the bot could not read."
TOO_MANY_REDIRECTS_MESSAGE = "The site redirected too many times."
BAD_REDIRECT_MESSAGE = "The site sent a redirect the bot could not follow."
NOT_AN_IMAGE_MESSAGE = "That is not a PNG, JPEG, GIF or WebP image."
CLOSED_MESSAGE = "The bot is shutting down."
UNKNOWN_MESSAGE = "Something went wrong while fetching that address."

# Lower-case "feed" here is the document at the address, not the bot's Feed.
_STATUS_MESSAGES = {
    401: "The site refused to let the bot read that address (error 401).",
    403: (
        "The site is blocking automated readers such as this bot (error 403). "
        "The address may be fine, but the bot cannot read it."
    ),
    404: "The site has nothing at that address (error 404), so check the address.",
    410: "The site says the feed at that address is gone (error 410), so check the address.",
    429: "The site asked the bot to slow down (error 429).",
    503: "The site is temporarily unavailable (error 503).",
}
_RETRY_AFTER_STATUSES = frozenset({429, 503})

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
)
_IMAGE_ACCEPT = ", ".join(_IMAGE_EXTENSIONS)
_ACCEPT_ENCODING = "gzip, deflate"
# How a site that answers "slow down" says for how long, the standard header first.
# Reddit sends only the last one.
_WAIT_HEADERS = ("Retry-After", "RateLimit-Reset", "X-RateLimit-Reset")
# A reset header is either seconds to wait or the Unix time to wait for. No wait the
# bot honours is this long, and the Unix time has been past it since 1973.
_RESET_IS_A_TIME_FROM = 100_000_000.0
_CHUNK_BYTES = 64 * 1024

_SIX_TO_FOUR = ipaddress.IPv6Network("2002::/16")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class _RefusedAddress(Exception):
    """A connection to a non-public address was stopped.

    Deliberately not an OSError: the connector treats those as "try the next
    address" or wraps them, and a refusal must end the whole request.
    """


def _plain_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # is_global alone is not enough on 3.13: it is True for multicast (224.0.0.0/4,
    # ff0e::/16), for deprecated site-local fec0::/10 and for ::a.b.c.d.
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def is_public_address(address: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether a connection to this IP address stays on the public internet.

    Anything that cannot be parsed as an IP address is not public.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return _plain_public(ip)
    # IPv6 forms that carry an IPv4 address are judged by that address, whatever
    # this Python version's ipaddress properties say about the wrapper.
    if ip.ipv4_mapped is not None:  # ::ffff:a.b.c.d
        return _plain_public(ip.ipv4_mapped)
    if ip in _SIX_TO_FOUR:
        return _plain_public(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
    if ip in _NAT64:  # the well-known prefix; DNS64 networks reach all of IPv4 through it
        return _plain_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return _plain_public(ip)


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address a host string denotes if it is a literal, in any notation."""
    host = host.rstrip(".")
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        # What the OS accepts and ipaddress does not: 2130706433, 0x7f.1, 017700000001, 127.1
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


class _GuardedResolver(AbstractResolver):
    """Resolves with the wrapped resolver and refuses non-public answers.

    If a name resolves to several addresses and any one of them is not public, the
    whole name is refused rather than filtered: a public name has no business
    pointing inside, and a mixed answer is what a rebinding attempt looks like.
    """

    def __init__(self, inner: AbstractResolver, is_allowed: Callable[[str], bool]) -> None:
        self._inner = inner
        self._is_allowed = is_allowed

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if not results:
            raise OSError("no addresses")
        for result in results:
            if not self._is_allowed(result["host"]):
                raise _RefusedAddress
        return results

    async def close(self) -> None:
        # The wrapped resolver belongs to whoever made it.
        return None


def _has_refusal(exc: BaseException) -> bool:
    """Whether a refusal is this exception or hides in its causes."""
    seen: set[int] = set()
    todo: list[BaseException | None] = [exc]
    while todo and len(seen) < 50:
        current = todo.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, _RefusedAddress):
            return True
        todo.append(current.__cause__)
        todo.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            todo.extend(current.exceptions)
    return False


def _to_fetch_error(exc: Exception) -> FetchError:
    """A message for the Manager. Never passes on the library's own text."""
    if _has_refusal(exc):
        return FetchError(REFUSED_MESSAGE, permanent=True)
    if isinstance(exc, TimeoutError):
        return FetchError(TIMEOUT_MESSAGE)
    if isinstance(exc, aiohttp.InvalidURL):
        return FetchError(BAD_URL_MESSAGE, permanent=True)
    if isinstance(exc, aiohttp.ClientConnectorDNSError):
        return FetchError(NOT_FOUND_MESSAGE)
    if isinstance(exc, aiohttp.ClientSSLError):
        return FetchError(TLS_MESSAGE)
    if isinstance(exc, aiohttp.ClientConnectorError):
        return FetchError(UNREACHABLE_MESSAGE)
    if isinstance(exc, aiohttp.ClientResponseError):
        return FetchError(UNREADABLE_MESSAGE)
    if isinstance(
        exc,
        aiohttp.ClientPayloadError
        | aiohttp.ServerDisconnectedError
        | aiohttp.ClientOSError
        | ConnectionError,
    ):
        return FetchError(LOST_MESSAGE)
    if isinstance(exc, aiohttp.ClientError | OSError):
        return FetchError(UNREACHABLE_MESSAGE)
    return FetchError(UNKNOWN_MESSAGE)


def _took_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _size_label(size: int) -> str:
    megabyte = 1024 * 1024
    if size >= megabyte and size % megabyte == 0:
        return f"{size // megabyte} MB"
    if size >= 1024:
        return f"{size // 1024} kB"
    return f"{size} bytes"


def _sniff_image(data: bytes) -> str | None:
    """The image type the first bytes belong to, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _header_safe(value: str | None) -> bool:
    # Surrogates are how aiohttp hands over header bytes that are not UTF-8. They can be
    # neither stored nor sent back.
    return bool(value) and all(
        " " <= ch != "\x7f" and not "\ud800" <= ch <= "\udfff" for ch in value
    )


def _validator(value: str | None) -> str | None:
    """An ETag or Last-Modified worth keeping, or None."""
    return value if _header_safe(value) else None


def _retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header, which is a number of seconds or a date."""
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), MAX_RETRY_AFTER_S)
    try:
        seconds = parsedate_to_datetime(value).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    return min(seconds, MAX_RETRY_AFTER_S) if seconds >= 0 else None


def _reset_after(value: str | None) -> float | None:
    """Seconds to wait from a rate limit's reset header: seconds, or the Unix time it ends."""
    if not value:
        return None
    value = value.strip()
    if not value.isascii() or not value.replace(".", "", 1).isdigit():
        return None
    seconds = float(value)
    if seconds >= _RESET_IS_A_TIME_FROM:
        seconds -= time.time()
    return min(seconds, MAX_RETRY_AFTER_S) if seconds >= 0 else None


def _asked_wait(headers: Mapping[str, str]) -> float | None:
    for name in _WAIT_HEADERS:
        parse = _retry_after if name == "Retry-After" else _reset_after
        seconds = parse(headers.get(name))
        if seconds is not None:
            return seconds
    return None


def _status_error(response: aiohttp.ClientResponse) -> FetchError:
    status = response.status
    message = _STATUS_MESSAGES.get(status, f"The site answered with error {status}.")
    if status in _RETRY_AFTER_STATUSES:
        return FetchError(
            message, retry_after=_asked_wait(response.headers), slow_down=status == 429
        )
    return FetchError(message)


async def _read_limited(response: aiohttp.ClientResponse, limit: int, too_large: str) -> bytes:
    """Read the decoded body, giving up as soon as it passes the limit."""
    # Content-Length is only a shortcut for honest servers. The count below is the limit.
    declared = response.content_length
    if declared is not None and declared > limit:
        raise FetchError(too_large)
    body = bytearray()
    # Chunks are already decompressed, and aiohttp inflates in bounded steps, so a
    # small compressed body cannot expand past the limit before it is counted here.
    async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
        if len(body) + len(chunk) > limit:
            raise FetchError(too_large)
        body += chunk
    return bytes(body)


class HttpFetcher:
    """The Fetcher used in production. One per Instance; close it on shutdown."""

    def __init__(
        self,
        *,
        allow_private: bool,
        user_agent: str = DEFAULT_USER_AGENT,
        resolver: AbstractResolver | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self._allow_private = allow_private
        self._user_agent = user_agent
        self._resolver = resolver
        self._own_resolver: AbstractResolver | None = None
        self._timeout_s = timeout_s
        self._session: aiohttp.ClientSession | None = None
        self._closed = False
        # The one address decision, shared by all three layers of the guard.
        self._is_allowed: Callable[[str], bool] = is_public_address

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        resolver, self._own_resolver = self._own_resolver, None
        if resolver is not None:
            await resolver.close()

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        headers = self._headers(_FEED_ACCEPT)
        # Validators come from an earlier response; never let one smuggle a header in.
        if etag is not None and _header_safe(etag):
            headers["If-None-Match"] = etag
        if last_modified is not None and _header_safe(last_modified):
            headers["If-Modified-Since"] = last_modified
        conditional = "If-None-Match" in headers or "If-Modified-Since" in headers

        seen: dict[str, int] = {}  # the status and size, for the DEBUG line

        async def handle(response: aiohttp.ClientResponse, final_url: str) -> FetchResult:
            seen["status"] = response.status
            if response.status == 304 and conditional:
                return FetchResult(
                    not_modified=True,
                    body=b"",
                    etag=_validator(response.headers.get("ETag")) or _validator(etag),
                    last_modified=_validator(response.headers.get("Last-Modified"))
                    or _validator(last_modified),
                    url=final_url,
                )
            if response.status != 200:
                raise _status_error(response)
            body = await _read_limited(
                response, MAX_FEED_BYTES, f"The feed is larger than {_size_label(MAX_FEED_BYTES)}."
            )
            seen["bytes"] = len(body)
            return FetchResult(
                not_modified=False,
                body=body,
                etag=_validator(response.headers.get("ETag")),
                last_modified=_validator(response.headers.get("Last-Modified")),
                url=final_url,
            )

        started = time.monotonic()
        try:
            result = await self._request(url, headers, handle)
        except FetchError as exc:
            # The Check that asked logs the failure for the Feed; this is the detail. Only
            # the site is named: the address can hold a private key. The message is fixed text.
            log.debug(
                "fetch host=%s status=%s took_ms=%d conditional=%s error=%s",
                quote(site(url)),
                seen.get("status", "none"),
                _took_ms(started),
                "yes" if conditional else "no",
                quote(str(exc)),
            )
            raise
        log.debug(
            "fetch host=%s status=%s bytes=%d took_ms=%d conditional=%s",
            quote(site(url)),
            seen.get("status", "none"),
            seen.get("bytes", 0),
            _took_ms(started),
            "yes" if conditional else "no",
        )
        return result

    async def fetch_image(self, url: str, *, max_bytes: int = MAX_COVER_IMAGE_BYTES) -> ImageData:
        async def handle(response: aiohttp.ClientResponse, final_url: str) -> ImageData:
            if response.status != 200:
                raise FetchError(f"The site answered with error {response.status}.")
            if response.content_type not in _IMAGE_EXTENSIONS:
                raise FetchError(NOT_AN_IMAGE_MESSAGE)
            data = await _read_limited(
                response, max_bytes, f"The image is larger than {_size_label(max_bytes)}."
            )
            # The header is the server's claim; the bytes decide. Otherwise a Manager
            # could have the bot upload any fetched content as a "picture".
            content_type = _sniff_image(data)
            if content_type is None:
                raise FetchError(NOT_AN_IMAGE_MESSAGE)
            # A fixed name: nothing from the URL or the response reaches the filename.
            filename = f"image.{_IMAGE_EXTENSIONS[content_type]}"
            return ImageData(data=data, filename=filename, content_type=content_type)

        return await self._request(url, self._headers(_IMAGE_ACCEPT), handle)

    def _headers(self, accept: str) -> dict[str, str]:
        # Accept before Accept-Encoding, the order browsers send them in. Reddit's bot
        # filter answers 403 to the other order, whatever the User-Agent.
        return {
            "User-Agent": self._user_agent,
            "Accept": accept,
            "Accept-Encoding": _ACCEPT_ENCODING,
        }

    async def _request[T](
        self,
        url: str,
        headers: dict[str, str],
        handle: Callable[[aiohttp.ClientResponse, str], Awaitable[T]],
    ) -> T:
        """Run one fetch under the time limit and turn every failure into FetchError."""
        try:
            async with asyncio.timeout(self._timeout_s):
                return await self._follow(url, headers, handle)
        except FetchError:
            raise
        except Exception as exc:  # cancellation is a BaseException and passes through
            raise _to_fetch_error(exc) from exc

    async def _follow[T](
        self,
        url: str,
        headers: dict[str, str],
        handle: Callable[[aiohttp.ClientResponse, str], Awaitable[T]],
    ) -> T:
        session = self._get_session()
        for hop in range(MAX_REDIRECTS + 1):
            target = self._parse_url(url)
            # Redirects are never left to aiohttp: each hop is parsed and checked here.
            async with session.get(target, headers=headers, allow_redirects=False) as response:
                if response.status not in _REDIRECT_STATUSES:
                    return await handle(response, str(target))
                location = response.headers.get("Location")
                if not location:
                    raise FetchError(BAD_REDIRECT_MESSAGE)
                if hop == MAX_REDIRECTS:
                    break
                try:
                    url = str(target.join(URL(location)))
                except (ValueError, TypeError):
                    raise FetchError(BAD_REDIRECT_MESSAGE) from None
        raise FetchError(TOO_MANY_REDIRECTS_MESSAGE)

    def _parse_url(self, url: str) -> URL:
        """Check a URL, from a Manager or from a redirect, and parse it for aiohttp."""
        bad = FetchError(BAD_URL_MESSAGE, permanent=True)
        if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
            raise bad
        # Whitespace, control characters and backslashes are where URL parsers disagree.
        if "\\" in url or any(ch <= " " or ch == "\x7f" for ch in url):
            raise bad
        try:
            parsed = URL(url)
            netloc = urlsplit(url).netloc
            parsed.port  # noqa: B018  (raises on a bad port)
            parsed.host  # noqa: B018  (raises on a host that is not a valid name: xn--)
        except (ValueError, TypeError):
            raise bad from None
        if parsed.scheme not in ("http", "https") or not parsed.raw_host:
            raise bad
        if "@" in netloc or parsed.raw_user is not None or parsed.raw_password is not None:
            raise FetchError(CREDENTIALS_MESSAGE, permanent=True)
        if not self._allow_private:
            literal = _literal_ip(parsed.host or "")
            if literal is not None and not self._is_allowed(str(literal)):
                raise FetchError(REFUSED_MESSAGE, permanent=True)
        return parsed

    def _open_socket(self, addr_info: tuple[Any, ...]) -> socket.socket:
        """The connector's socket factory: the last check, on the address being dialled."""
        family, type_, proto, _, sockaddr = addr_info
        if not self._is_allowed(str(sockaddr[0])):
            raise _RefusedAddress
        return socket.socket(family=family, type=type_, proto=proto)

    def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise FetchError(CLOSED_MESSAGE)
        if self._session is None:
            resolver = self._resolver
            if resolver is None:
                # getaddrinfo in a thread: the same answers the OS would give.
                resolver = self._own_resolver = aiohttp.ThreadedResolver()
            if self._allow_private:
                connector = aiohttp.TCPConnector(resolver=resolver)
            else:
                connector = aiohttp.TCPConnector(
                    resolver=_GuardedResolver(resolver, lambda address: self._is_allowed(address)),
                    socket_factory=self._open_socket,
                )
            self._session = aiohttp.ClientSession(
                connector=connector,
                trust_env=False,  # no proxies or netrc from the environment
                cookie_jar=aiohttp.DummyCookieJar(),  # nothing carries over between sites
                timeout=aiohttp.ClientTimeout(total=None),  # _request holds the one time limit
            )
        return self._session
