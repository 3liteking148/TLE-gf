"""Pre-command cross-guild handle sync hook.

``maybe_sync_handle`` runs via ``bot.before_invoke`` before every command.
If the invoker has no handle in the current guild but has an active handle in
some other guild, it registers the most recently set one and (on guilds that
enabled automatic role updates) best-effort assigns the matching rank role.

The hook must never raise: a ``before_invoke`` failure would abort the
command. All failures are logged and swallowed. ``;handle``-group commands are
skipped entirely so ``;handle identify`` can always run and override a
synced row.
"""
import logging

from tle.util import codeforces_common as cf_common
from tle.util.db.user_db_conn import UniqueConstraintFailed
from tle.cogs._handles_rankup import RankUpMixin

logger = logging.getLogger(__name__)


def _is_handle_command(ctx):
    command = ctx.command
    if command is None:
        return False
    if command.name == 'handle':
        return True
    return any(parent.name == 'handle' for parent in command.parents)


async def maybe_sync_handle(ctx):
    """Register a cross-guild handle for the invoker before a command runs."""
    try:
        await _maybe_sync_handle(ctx)
    except Exception:
        logger.exception('Handle sync hook failed for command %r',
                         ctx.command.qualified_name if ctx.command else None)


async def _maybe_sync_handle(ctx):
    if ctx.guild is None or ctx.author.bot or _is_handle_command(ctx):
        return

    user_db = cf_common.user_db
    if user_db.get_handle(ctx.author.id, ctx.guild.id):
        return

    handle = user_db.get_other_guild_handle(ctx.author.id, ctx.guild.id)
    if handle is None:
        return

    try:
        user_db.set_handle(ctx.author.id, ctx.guild.id, handle, synced=True)
    except UniqueConstraintFailed:
        logger.debug('Not syncing handle %s of user %s to guild %s: taken',
                     handle, ctx.author.id, ctx.guild.id)
        return
    logger.info('Synced handle %s of user %s to guild %s',
                handle, ctx.author.id, ctx.guild.id)

    if not user_db.has_auto_role_update_enabled(ctx.guild.id):
        return
    member = ctx.guild.get_member(ctx.author.id)
    user = user_db.fetch_cf_user(handle)
    if member is None or user is None:
        return
    roles = [role for role in ctx.guild.roles if role.name == user.rank.title]
    if not roles:
        logger.debug('Rank role %s missing in guild %s; skipping role sync',
                     user.rank.title, ctx.guild.id)
        return
    await RankUpMixin.update_member_rank_role(
        member, roles[0], reason='Handle synced from another server')
