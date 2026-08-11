"""Pre-command cross-guild handle sync hook.

``maybe_sync_handle`` runs via ``bot.before_invoke`` before every command.
If the invoker has no Codeforces or AtCoder handle in the current guild but
has an active one in some other guild, it registers the most recently set one
per platform. Codeforces sync additionally (on guilds that enabled automatic
role updates) best-effort assigns the matching rank role; AtCoder sync only
registers the handle.

The hook must never raise: a ``before_invoke`` failure would abort the
command. All failures are logged and swallowed. Handle-linking groups
(``;handle``, ``;atcoder``) are skipped entirely so ``;handle identify`` and
``;atcoder identify`` can always run and override a synced row.
"""
import logging

from tle.util import codeforces_common as cf_common
from tle.util.db.user_db_conn import UniqueConstraintFailed
from tle.cogs._handles_rankup import RankUpMixin

logger = logging.getLogger(__name__)


_SKIPPED_GROUPS = ('handle', 'atcoder')


def _is_handle_command(ctx):
    command = ctx.command
    if command is None:
        return False
    names = [command.name] + [parent.name for parent in command.parents]
    return any(name in _SKIPPED_GROUPS for name in names)


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

    await _maybe_sync_cf_handle(ctx)
    await _maybe_sync_atcoder_handle(ctx)


async def _maybe_sync_cf_handle(ctx):
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


async def _maybe_sync_atcoder_handle(ctx):
    user_db = cf_common.user_db
    if user_db.get_atcoder_handle(ctx.author.id, ctx.guild.id):
        return

    handle = user_db.get_other_guild_atcoder_handle(ctx.author.id, ctx.guild.id)
    if handle is None:
        return

    try:
        user_db.set_atcoder_handle(ctx.author.id, ctx.guild.id, handle,
                                   synced=True)
    except UniqueConstraintFailed:
        logger.debug('Not syncing AtCoder handle %s of user %s to guild %s: '
                     'taken', handle, ctx.author.id, ctx.guild.id)
        return
    logger.info('Synced AtCoder handle %s of user %s to guild %s',
                handle, ctx.author.id, ctx.guild.id)
