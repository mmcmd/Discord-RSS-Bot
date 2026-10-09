"""Start-up: read the configuration, connect to Discord and run the Check loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sqlite3
import sys

import discord
from discord import app_commands

from . import __version__, commands
from .commands import _ui
from .config import Config, ConfigError, load_config
from .db import Database
from .deliver import DiscordDeliverer, DiscordNotifier
from .fetch import HttpFetcher
from .journal import Journal, quote
from .logsetup import setup_logging
from .models import PostAs
from .parse import parse_feed
from .render import render_default, render_item
from .scheduler import Scheduler, SystemClock
from .service import NO_SUCH_FEED, FeedService, ServiceError

log = logging.getLogger("rssbot")

IDENTITY_REFRESH_EVERY_S = 24 * 60 * 60
WEBHOOK_CLEANUP_EVERY_S = 6 * 60 * 60


class Bot(discord.Client):
    def __init__(self, config: Config) -> None:
        # Only the guilds intent: no member cache, no message content, nothing privileged.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            max_messages=None,
            chunk_guilds_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            allowed_mentions=discord.AllowedMentions.none(),
            # A 429 that asks for a longer wait raises RateLimited (a retry on the next Check)
            # instead of sleeping past the Item's allowance. discord.py's lowest value is 30 s.
            # Webhook sends are not covered: Webhook.partial sleeps through a 429 itself, and
            # cutting that short could post twice (docs/adr/0003).
            max_ratelimit_timeout=30.0,
        )
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.db = _open_database(config)
        self.clock = SystemClock()
        self.fetcher = HttpFetcher(allow_private=config.allow_private_urls)
        self.notifier = DiscordNotifier(self, self.db)
        self.deliverer = DiscordDeliverer(self, self.db, self.fetcher, self.notifier)
        self.journal = Journal(self.db, self.clock, self.notifier)
        self.service = FeedService(self.db, self.fetcher, self.clock, self.journal)
        self.scheduler = Scheduler(
            self.db,
            self.fetcher,
            self.deliverer,
            self.journal,
            self.clock,
            parse_feed,
            render_item,
            render_default,
        )
        self.deps = _ui.Deps(
            db=self.db,
            service=self.service,
            deliverer=self.deliverer,
            scheduler=self.scheduler,
            journal=self.journal,
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        commands.add_all(self.tree, self)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            # The commands from the last successful sync keep working.
            log.exception("Could not register the slash commands with Discord")
        self._tasks.append(asyncio.create_task(self._run_when_ready(), name="check-loop"))
        self._tasks.append(asyncio.create_task(self._refresh_identities(), name="site-identity"))
        self._tasks.append(asyncio.create_task(self._clean_webhooks(), name="webhook-cleanup"))

    async def _run_when_ready(self) -> None:
        # Checks need the channel cache, which is only filled once Discord reports ready.
        await self.wait_until_ready()
        await self.scheduler.run_supervised()

    async def _refresh_identities(self) -> None:
        """Keep the name and icon of "Post as: Site" Feeds current. Failures only cost an icon."""
        await self.wait_until_ready()
        while True:
            for guild in list(self.guilds):
                try:
                    feeds = self.db.list_feeds(guild.id)
                except Exception:
                    log.exception("Could not list the Feeds of Server %s", guild.id)
                    continue
                for feed in feeds:
                    if feed.post_as is not PostAs.SITE:
                        continue
                    try:
                        await self.service.refresh_site_identity(guild.id, feed.id)
                    except asyncio.CancelledError:
                        raise
                    except ServiceError as exc:
                        if str(exc) != NO_SUCH_FEED:  # removed meanwhile: nothing to update
                            log.exception(
                                "Could not update the Post as name and picture of Feed %s",
                                feed.id,
                            )
                    except Exception:
                        log.exception(
                            "Could not update the Post as name and picture of Feed %s", feed.id
                        )
            await asyncio.sleep(IDENTITY_REFRESH_EVERY_S)

    async def _clean_webhooks(self) -> None:
        """Delete the webhooks no Feed needs, retrying those Discord failed to delete."""
        await self.wait_until_ready()
        while True:
            await self.deliverer.cleanup_unused_webhooks()
            await asyncio.sleep(WEBHOOK_CLEANUP_EVERY_S)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        # Its Feeds are left alone: their next Check meets the missing channel and pauses
        # them as for any lost channel. Only the webhooks are cleaned up here.
        for gone in (channel, *getattr(channel, "threads", ())):
            await self.deliverer.cleanup_webhook(gone.id)

    async def on_thread_delete(self, thread: discord.Thread) -> None:
        # A thread's webhook lives in its parent channel, which survives.
        await self.deliverer.cleanup_webhook(thread.id)

    async def on_ready(self) -> None:
        present = {guild.id for guild in self.guilds}
        log.info(
            "ready bot=%s bot_id=%s servers=%d feeds=%s",
            quote(str(self.user)),
            getattr(self.user, "id", 0),
            len(present),
            self._feed_count(present),
        )
        for server_id in present:
            self._returned(server_id)
        # Discord only reports a removal while the bot is connected, so one that happened
        # while it was offline is found here. `guilds` also holds the Servers that are
        # merely unavailable (an outage at Discord), so those are not taken for removed.
        try:
            now = self.clock.now()
            for server_id in self.db.present_server_ids():
                if server_id not in present:
                    log.info("server.removed server=%s noticed=on_start", server_id)
                    self.db.mark_server_removed(server_id, now)
        except Exception:
            log.exception("Could not work out which Servers the bot was removed from")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("server.joined server=%s name=%s", guild.id, quote(guild.name))
        self._returned(guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Checking stops at once; the Server's data is deleted 30 days later by the Check loop.
        log.info("server.removed server=%s name=%s", guild.id, quote(guild.name))
        try:
            self.db.mark_server_removed(guild.id, self.clock.now())
        except Exception:
            log.exception("Could not mark Server %s as removed", guild.id)

    def _feed_count(self, server_ids: set[int]) -> int | str:
        try:
            return sum(len(self.db.list_feeds(server_id)) for server_id in server_ids)
        except Exception:
            log.warning("Could not count the Feeds for the start-up line", exc_info=True)
            return "unknown"

    def _returned(self, server_id: int) -> None:
        try:
            self.db.mark_server_returned(server_id)
        except Exception:
            log.exception("Could not mark Server %s as present", server_id)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        # Reports already on their way go out while Discord is still connected; each is bounded.
        with contextlib.suppress(Exception):
            await self.journal.drain()
        await super().close()
        with contextlib.suppress(Exception):
            await self.fetcher.close()
        with contextlib.suppress(Exception):
            self.db.close()


def _open_database(config: Config) -> Database:
    try:
        return Database(config.db_path)
    except (OSError, sqlite3.Error) as error:
        raise ConfigError(
            f"The database at {config.db_path} could not be opened ({error}). "
            f"The user the bot runs as must be able to write to {config.data_dir}: "
            "check DATA_DIR and who owns the folder or volume mounted there."
        ) from error


async def run(config: Config) -> int:
    bot = Bot(config)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Docker stops a container with SIGTERM; shut down cleanly instead of being killed.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(bot.start(config.token), name="discord")
    stopper = asyncio.create_task(stop.wait(), name="stop-signal")
    try:
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        log.info("shutdown reason=%s", "signal" if stop.is_set() else "connection_lost")
        if runner.done() and (error := runner.exception()) is not None:
            if isinstance(error, discord.LoginFailure):
                log.error("Discord refused the bot token. Check DISCORD_TOKEN.")
            else:
                log.error("The connection to Discord failed: %s", error)
            return 1
        return 0
    finally:
        stopper.cancel()
        await bot.close()
        if not runner.done():
            runner.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await runner


def main() -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(f"Configuration problem: {error}", file=sys.stderr)
        return 2
    setup_logging(config.log_level, sys.stdout)
    # discord.py's DEBUG lines print request URLs, which carry webhook and interaction tokens:
    # setup_logging keeps the "discord" logger at INFO or above, whatever LOG_LEVEL is.
    log.info(
        "start version=%s log_level=%s data_dir=%s allow_private_urls=%s",
        __version__,
        config.log_level,
        quote(str(config.data_dir)),
        "yes" if config.allow_private_urls else "no",
    )
    try:
        return asyncio.run(run(config))
    except ConfigError as error:
        print(f"Configuration problem: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
