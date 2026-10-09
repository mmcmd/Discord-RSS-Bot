"""The container log's format, and that no address with a secret in it reaches it."""

from __future__ import annotations

import io
import logging
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from rssbot.fetch import HttpFetcher
from rssbot.logsetup import LOG_DATE_FORMAT, RedactingFormatter, redact, setup_logging, site
from rssbot.ports import FetchError


def test_site_keeps_the_scheme_and_host_and_nothing_else():
    assert site("https://Example.com/feed?key=SECRET#x") == "https://example.com"
    assert site("http://user:pass@example.com:8080/a") == "http://example.com"
    assert site("not an address") == "(an address)"
    assert site("ftp://example.com/x") == "(an address)"
    assert site("http://[::1") == "(an address)"


@pytest.mark.parametrize(
    "text",
    [
        "cannot connect to https://example.com/feed?key=SECRET",
        "Cannot connect to host example.com:443 ssl:default [x] for URL('https://ex.com/a/SECRET')",
        "HTTP 404 on https://user:SECRET@example.com/",
        "POST /api/webhooks/123456/SECRET-token_abc.def failed",
        "https://discord.com/api/webhooks/123456/SECRET-token?wait=true",
    ],
)
def test_redact_removes_secrets_from_addresses_and_webhooks(text):
    assert "SECRET" not in redact(text)


def test_redact_leaves_plain_text_alone():
    text = 'check feed=1 name="BBC News" server=2 posted=0'
    assert redact(text) == text


def test_the_formatter_writes_utc_timestamps_and_redacts_tracebacks():
    try:
        raise RuntimeError("failed for https://example.com/feed?key=SECRET")
    except RuntimeError:
        info = sys.exc_info()
        record = logging.LogRecord("rssbot.x", logging.WARNING, __file__, 1, "msg", (), info)
    record.created = 0  # 1970-01-01T00:00:00Z
    line = RedactingFormatter().format(record)
    assert line.startswith("1970-01-01T00:00:00Z WARNING rssbot.x: msg")
    assert "Traceback" in line and "SECRET" not in line
    assert LOG_DATE_FORMAT.endswith("Z")


def test_setup_logging_sets_the_level_and_keeps_discord_py_at_info(monkeypatch):
    discord_log = logging.getLogger("discord")
    monkeypatch.setattr(discord_log, "level", discord_log.level)
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", root.level)
    stream = io.StringIO()

    setup_logging("DEBUG", stream)

    assert root.level == logging.DEBUG
    assert discord_log.level == logging.INFO  # its DEBUG lines print tokens
    logging.getLogger("rssbot.t").warning("hello %s", "https://e.com/x?k=SECRET")
    assert "WARNING rssbot.t: hello https://e.com/…" in stream.getvalue()
    assert "SECRET" not in stream.getvalue()


async def test_a_failing_fetch_of_an_address_with_a_key_logs_no_key(caplog):
    caplog.set_level(logging.DEBUG)
    async with HttpFetcher(allow_private=False) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch("http://127.0.0.1/feed?key=SECRET")  # refused as private
    async with HttpFetcher(allow_private=True, timeout_s=5) as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch("https://example.com:1/feed?key=SECRET")  # nothing listens
    _assert_clean(caplog)


async def test_fetches_are_logged_at_debug_with_the_site_only(caplog):
    async def ok(request: web.Request) -> web.Response:
        return web.Response(body=b"<rss/>", headers={"ETag": '"v1"'})

    async def missing(request: web.Request) -> web.Response:
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/missing", missing)
    caplog.set_level(logging.INFO, logger="rssbot.fetch")
    async with TestServer(app) as server, HttpFetcher(allow_private=True) as fetcher:
        base = f"http://127.0.0.1:{server.port}"
        await fetcher.fetch(f"{base}/ok?key=SECRET")
        assert caplog.records == []  # a fetch is nothing to say at INFO

        caplog.set_level(logging.DEBUG, logger="rssbot.fetch")
        await fetcher.fetch(f"{base}/ok?key=SECRET", etag='"v1"')
        with pytest.raises(FetchError):
            await fetcher.fetch(f"{base}/missing?key=SECRET")

    first, second = [r.getMessage() for r in caplog.records]
    assert first.startswith('fetch host="http://127.0.0.1" status=200 bytes=6 took_ms=')
    assert first.endswith("conditional=yes")
    assert "status=404" in second and 'error="' in second
    _assert_clean(caplog)


def _assert_clean(caplog: pytest.LogCaptureFixture) -> None:
    formatter = RedactingFormatter()
    assert caplog.records
    for record in caplog.records:
        assert "SECRET" not in record.getMessage()
        assert "SECRET" not in formatter.format(record)
