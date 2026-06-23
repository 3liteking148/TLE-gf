"""Starboard migration cog — crawls an old bot's pillboard channel and
re-posts everything into TLE-gf's starboard system.

Flow:
  1. ;migrate start #old_channel #new_channel :main_emoji: :alias_emoji:
  2. ;migrate status
  3. ;migrate complete #new_channel
  4. ;migrate resume  (retry after failure)
  5. ;migrate cancel

The first emoji is the main emoji; subsequent emojis are aliases that get
merged into the main emoji during posting and registered as aliases on complete.

The crawl/post/export/complete/command logic lives in mixin modules
(``_migrate_phases``, ``_migrate_export``, ``_migrate_complete``,
``_migrate_commands``) to keep each file small. Tunables defined here are read
dynamically by the mixins so tests can monkeypatch ``tle.cogs.migrate._RATE_DELAY``
etc.
"""
import logging
import pathlib

import discord
from discord.ext import commands

from tle import constants
from tle.util import discord_common
# Re-exported for callers/tests importing from tle.cogs.migrate.
from tle.cogs._starboard_helpers import _emoji_str  # noqa: F401
from tle.cogs._migrate_helpers import (  # noqa: F401
    parse_old_bot_message,
    serialize_embed_fallback,
    build_fallback_message,
)
from tle.cogs.starboard import Starboard, _starboard_content  # noqa: F401
from tle.cogs._migrate_retry import discord_retry, RetryExhaustedError  # noqa: F401
from tle.cogs._migrate_phases import MigratePhasesMixin
from tle.cogs._migrate_export import MigrateExportMixin
from tle.cogs._migrate_complete import MigrateCompleteMixin
from tle.cogs._migrate_commands import (
    MigrateCommandsMixin, _pause_kvs_key, _paginate,  # noqa: F401
)
from tle.util.discord_common import requires_guild_feature

logger = logging.getLogger(__name__)

# Rate limit delay between Discord API calls during crawl/post
_RATE_DELAY = 0.5

# Retry parameters for Discord API calls
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 2.0
_EXPORT_DIR = pathlib.Path('extra') / 'pillboard_exports'
_EXPORT_PROGRESS_INTERVAL = 250
_EXPORT_CONTEXT_LIMIT_MAX = 50


class Migrate(MigratePhasesMixin, MigrateExportMixin, MigrateCompleteMixin,
              MigrateCommandsMixin, commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tasks = {}   # guild_id -> asyncio.Task
        self._paused = {}  # guild_id -> asyncio.Event (clear = paused)

    def _launch(self, guild_id, migration, emoji_set):
        """Create and register a background migration task for this guild."""
        import asyncio
        task = asyncio.create_task(
            self._run_migration(
                guild_id,
                int(migration.old_channel_id),
                int(migration.new_channel_id),
                emoji_set
            )
        )
        self._tasks[guild_id] = task
        return task

    def _task_running(self, guild_id):
        task = self._tasks.get(guild_id)
        return task is not None and not task.done()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name='migrate', invoke_without_command=True)
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    @requires_guild_feature('migration_ops')
    async def migrate(self, ctx):
        """Starboard migration commands."""
        await ctx.send_help(ctx.command)

    @migrate.command(name='start')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def start(self, ctx, old_channel: discord.TextChannel,
                    new_channel: discord.TextChannel, *emojis: str):
        """Start migrating from an old bot's starboard channel.

        The first emoji is the main emoji. Any additional emojis are treated
        as aliases — they'll be crawled separately but merged into the main
        emoji during posting.

        Usage: ;migrate start #old-pillboard #new-pillboard :pill: :chocolate_bar:
        """
        await self._impl_start(ctx, old_channel, new_channel, emojis)

    @migrate.command(name='status')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def status(self, ctx):
        """Check the progress of the current migration."""
        await self._impl_status(ctx)

    @migrate.command(name='export')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def export_pillboard(self, ctx, old_channel: discord.TextChannel,
                               *args: str):
        """Export old pillboard posts and linked original messages to JSON.

        Usage:
          ;migrate export #old-pillboard :pill:
          ;migrate export #old-pillboard :pill: :chocolate_bar:
          ;migrate export #old-pillboard +limit=100 :pill:
          ;migrate export #old-pillboard +limit=1 +context=10
        """
        await self._cmd_pillboard_export(ctx, old_channel, *args)

    @commands.command(name='pillboard-export')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def pillboard_export(self, ctx, pillboard_channel: discord.TextChannel,
                               *args: str):
        """Export pillboard posts and linked original messages to JSON.

        Usage:
          ;pillboard-export #pillboard
          ;pillboard-export #pillboard +limit=100
          ;pillboard-export #pillboard +limit=1 +context=10
        """
        await self._cmd_pillboard_export(ctx, pillboard_channel, *args)

    @migrate.command(name='complete')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def complete(self, ctx, new_channel: discord.TextChannel):
        """Finalize migration: create emoji configs and activate live tracking.

        Usage: ;migrate complete #new-pillboard
        """
        await self._impl_complete(ctx, new_channel)

    @migrate.command(name='resume')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def resume(self, ctx):
        """Resume a failed migration. Retries any post_failed entries.

        Usage: ;migrate resume
        """
        await self._impl_resume(ctx)

    @migrate.command(name='show-deleted')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def show_deleted(self, ctx):
        """List deleted/inaccessible messages found during migration.

        Shows links to the old bot's starboard posts so you can verify
        which messages were lost. If already posted, also links the new post.

        Usage: ;migrate show-deleted
        """
        await self._impl_show_deleted(ctx)

    @migrate.command(name='retry-failed')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def retry_failed(self, ctx):
        """Retry messages that failed after all retry attempts.

        Resets retry_exhausted entries and re-runs the post phase for them.

        Usage: ;migrate retry-failed
        """
        await self._impl_retry_failed(ctx)

    @migrate.command(name='view-failed')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def view_failed(self, ctx):
        """List messages that failed after all retry attempts.

        Shows links to the old bot's starboard posts and the error message.

        Usage: ;migrate view-failed
        """
        await self._impl_view_failed(ctx)

    @migrate.command(name='restart-post')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def restart_post(self, ctx):
        """Delete all posted messages from the new channel and re-post everything.

        Keeps crawl data intact — only the post phase is re-run.

        Usage: ;migrate restart-post
        """
        await self._impl_restart_post(ctx)

    @migrate.command(name='pause')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def pause(self, ctx):
        """Pause the running migration after the current message finishes.

        Usage: ;migrate pause
        """
        await self._impl_pause(ctx)

    @migrate.command(name='unpause')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def unpause(self, ctx):
        """Resume a paused migration.

        Usage: ;migrate unpause
        """
        await self._impl_unpause(ctx)

    @migrate.command(name='cancel')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def cancel(self, ctx):
        """Cancel the current migration and clean up."""
        await self._impl_cancel(ctx)

    # ------------------------------------------------------------------
    # Resume on restart
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        """Resume any in-progress migrations after bot restart."""
        await self._impl_resume_on_ready()


async def setup(bot):
    await bot.add_cog(Migrate(bot))
