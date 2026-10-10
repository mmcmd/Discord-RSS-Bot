"""Interfaces between the Check loop and the outside world.

The Check loop is given these, so tests can drive it with fakes.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .models import (
    MAX_COVER_IMAGE_BYTES,
    Actor,
    Feed,
    Item,
    LogEntry,
    OutgoingMessage,
    ParsedFeed,
)


class FetchError(Exception):
    """A fetch failed. The message is safe to show to a Manager."""

    def __init__(
        self,
        message: str,
        *,
        permanent: bool = False,
        retry_after: float | None = None,
        slow_down: bool = False,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent  # retrying cannot help, e.g. a refused address
        self.retry_after = retry_after  # seconds the site asked the bot to wait, if it said
        self.slow_down = slow_down  # the site is fine and asked for fewer requests (429)


@dataclass(frozen=True, slots=True)
class FetchResult:
    not_modified: bool  # the source says nothing changed; body is empty
    body: bytes
    etag: str | None
    last_modified: str | None
    url: str  # after redirects
    # The Content-Type header, so that its charset reaches the parser. Not part of equality.
    content_type: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class ImageData:
    data: bytes
    filename: str
    content_type: str


class Fetcher(Protocol):
    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        """Fetch a feed or page. Raises FetchError."""
        ...

    async def fetch_image(self, url: str, *, max_bytes: int = MAX_COVER_IMAGE_BYTES) -> ImageData:
        """Download an image. Raises FetchError, including when it is too large or not an image."""
        ...


class DeliveryOutcome(enum.Enum):
    DELIVERED = "delivered"
    RETRY = "retry"  # a temporary failure; try again on the next Check
    REJECTED = "rejected"  # Discord refused this message's content; resending it cannot help
    LOST_CHANNEL = "lost_channel"  # the channel is gone or the bot may not post there
    NEEDS_TAG = "needs_tag"  # the forum requires a tag and the message has none
    # The request may have reached Discord and there was no answer to say whether it took the
    # message (connection lost, timeout). Sending again could post twice: the Item is skipped.
    UNKNOWN = "unknown"


class Deliverer(Protocol):
    async def deliver(self, feed: Feed, message: OutgoingMessage) -> DeliveryOutcome:
        """Post one message. Reports failures as an outcome; does not raise for them."""
        ...


class Notifier(Protocol):
    async def notify(self, server_id: int, text: str) -> None:
        """Post a note to the Server's Logs channel if it has one. Never raises."""
        ...

    async def announce(self, server_id: int, entries: Sequence[LogEntry], actor: Actor) -> None:
        """Report Log entries in the Server's Logs channel if it has one. Never raises.

        The entries are one report: several of one kind (an OPML import) are listed
        together. `actor` is who did it; Actor.bot() for the bot's own reports.
        """
        ...


class Clock(Protocol):
    def now(self) -> int:
        """Current time in Unix seconds."""
        ...

    async def sleep(self, seconds: float) -> None: ...


# (body, feed url, content type); raises ParseError
ParseFn = Callable[[bytes, str, str], ParsedFeed]
RenderFn = Callable[[Feed, Item], OutgoingMessage]
