"""How the container log is written: one format, in UTC, with web addresses cut down to their site.

A Feed's address can carry a private key in its path or query, and a Discord webhook address
carries its token. The code logs a Feed by id and name and an address by its site only, but
exception text from aiohttp or discord.py can embed a whole address, so every line also passes
through `RedactingFormatter`, which is the last line of defence.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # always UTC: the Z says so

_ADDRESS = re.compile(r"""(https?://)([^\s/?#"'<>\\]*)[^\s"'<>\\]*""", re.IGNORECASE)
_WEBHOOK_PATH = re.compile(r"/webhooks/(\d+)/[\w.-]+", re.IGNORECASE)


def site(url: str) -> str:
    """A web address cut down to its scheme and host, e.g. `https://example.com`.

    Anything that is not a web address with a host gives `(an address)`; this never raises.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return "(an address)"
    if parts.scheme.lower() not in ("http", "https") or not host:
        return "(an address)"
    return f"{parts.scheme.lower()}://{host}"


def redact(text: str) -> str:
    """`text` with every web address cut down to its site and every webhook token removed."""

    def keep_site(match: re.Match[str]) -> str:
        host = match.group(2).rsplit("@", 1)[-1]  # a username or password is not kept
        return f"{match.group(1)}{host}/…" if host else "(an address)"

    return _WEBHOOK_PATH.sub(r"/webhooks/\1/…", _ADDRESS.sub(keep_site, text))


class RedactingFormatter(logging.Formatter):
    """The container log's format, with timestamps in UTC and no full web addresses."""

    converter = time.gmtime

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT, LOG_DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str, stream: object) -> None:
    """Send the root logger's records to `stream` in the container log format."""
    handler = logging.StreamHandler(stream)  # type: ignore[arg-type]
    handler.setFormatter(RedactingFormatter())
    logging.basicConfig(level=level, handlers=[handler])
    # discord.py's DEBUG lines print request URLs, which carry webhook and interaction tokens.
    logging.getLogger("discord").setLevel(max(logging.INFO, logging.getLogger().level))
