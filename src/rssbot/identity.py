"""Finding the name and picture a Feed's messages appear under when Post as is Site."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .models import ParsedFeed
from .ports import Fetcher

MAX_PAGE_BYTES = 512 * 1024

_BAD_EXTENSIONS = (".ico", ".svg")  # Discord cannot use these as a picture
_PICTURE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
_PICTURE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_APPLE_RELS = frozenset({"apple-touch-icon", "apple-touch-icon-precomposed"})
# Bounded on purpose: this runs on the event loop against a page anyone can write.
_SIZE = re.compile(r"(?<!\d)(\d{1,5})\s{0,3}x\s{0,3}\d", re.IGNORECASE)
_MAX_SIZES_CHARS = 100


@dataclass(frozen=True, slots=True)
class SiteIdentity:
    name: str
    icon: str  # a URL, or ""


def _usable_image(url: str) -> bool:
    """An absolute http(s) URL that does not point at an .ico or .svg file."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return False
    return not parts.path.lower().endswith(_BAD_EXTENSIONS)


def _host(url: str) -> str:
    try:
        parts = urlparse(url)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    return host.removeprefix("www.")


def _home_page(parsed: ParsedFeed, feed_url: str) -> str:
    """The site's home page, or "" when neither the feed nor its URL tell us."""
    if _host(parsed.link):
        return parsed.link
    try:
        parts = urlparse(feed_url)
    except ValueError:
        return ""
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/"
    return ""


def _size(sizes: str) -> int:
    """The largest width in a `sizes` attribute; 0 when there is none."""
    return max((int(m.group(1)) for m in _SIZE.finditer(sizes[:_MAX_SIZES_CHARS])), default=0)


class _HeadScanner(HTMLParser):
    """Collects icon links, og:image and <base href> from the <head>; stops at <body>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base: str | None = None
        self.apple: list[tuple[int, str]] = []
        self.icons: list[tuple[int, str]] = []
        self.og_images: list[str] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        a = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag == "body":
            self.done = True
        elif tag == "base":
            if self.base is None and a.get("href"):
                self.base = a["href"]
        elif tag == "link":
            self._link(a)
        elif tag == "meta":
            kind = (a.get("property") or a.get("name") or "").lower()
            if kind == "og:image" and a.get("content"):
                self.og_images.append(a["content"])

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.done = True

    def _link(self, a: dict[str, str]) -> None:
        href = a.get("href", "")
        if not href:
            return
        rels = set(a.get("rel", "").lower().split())
        size = _size(a.get("sizes", ""))
        if rels & _APPLE_RELS:
            self.apple.append((size, href))
        elif "icon" in rels:
            kind = a.get("type", "").lower().split(";")[0].strip()
            if kind in _PICTURE_TYPES or _path(href).endswith(_PICTURE_EXTENSIONS):
                self.icons.append((size, href))


def _path(href: str) -> str:
    try:
        return urlparse(href).path.lower()
    except ValueError:
        return ""


def _best(candidates: list[tuple[int, str]], base: str) -> str:
    """The largest usable candidate (the first of equals), resolved against base."""
    best = ""
    best_size = -1
    for size, href in candidates:
        url = _resolve(base, href)
        if url and size > best_size:
            best, best_size = url, size
    return best


def _resolve(base: str, href: str) -> str:
    if href.lower().startswith("data:"):
        return ""
    try:
        url = urljoin(base, href)
    except ValueError:
        return ""
    return url if _usable_image(url) else ""


def _scan(body: bytes, page_url: str) -> tuple[_HeadScanner, str]:
    """What the page's <head> holds, and the address its links are relative to."""
    scanner = _HeadScanner()
    try:
        scanner.feed(body[:MAX_PAGE_BYTES].decode("utf-8", errors="replace"))
        scanner.close()
    except Exception:  # noqa: BLE001 - broken markup: use whatever was found before it
        pass

    base = page_url
    if scanner.base:
        with contextlib.suppress(ValueError):
            base = urljoin(page_url, scanner.base)
    return scanner, base


def _og_image(scanner: _HeadScanner, base: str) -> str:
    for href in scanner.og_images:
        image = _resolve(base, href)
        if image:
            return image
    return ""


def _icon_from_page(body: bytes, page_url: str) -> str:
    scanner, base = _scan(body, page_url)
    icon = _best(scanner.apple, base) or _best(scanner.icons, base)
    if icon:
        return icon
    return _og_image(scanner, base)


async def article_image(link: str, fetcher: Fetcher) -> str:
    """The og:image of an Item's page, for an Item whose Feed gave no image.

    Never raises (other than cancellation): a failure only means no image.
    """
    try:
        result = await fetcher.fetch(link)
        if result.not_modified or not result.body:
            return ""
        scanner, base = _scan(result.body, result.url or link)
        return _og_image(scanner, base)
    except Exception:  # noqa: BLE001 - FetchError or anything unexpected
        return ""


async def discover(parsed: ParsedFeed, feed_url: str, fetcher: Fetcher) -> SiteIdentity:
    """Never raises (other than cancellation): a failure only means a missing icon."""
    name = parsed.title.strip() or _host(parsed.link) or _host(feed_url)

    if _usable_image(parsed.image.strip()):
        return SiteIdentity(name, parsed.image.strip())

    try:
        page = _home_page(parsed, feed_url)
        if not page:
            return SiteIdentity(name, "")
        result = await fetcher.fetch(page)
        if result.not_modified or not result.body:
            return SiteIdentity(name, "")
        return SiteIdentity(name, _icon_from_page(result.body, result.url or page))
    except Exception:  # noqa: BLE001 - FetchError or anything unexpected
        return SiteIdentity(name, "")
