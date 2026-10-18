"""The ``bet`` command group and its bare-invocation handler.

Held in its own leaf module so the per-area command mixins
(``_betting_soccer_cmds``, ``_betting_soccer_open``, …) can attach their
subcommands with ``@BetGroupMixin.bet.command(...)`` without importing
``betting`` (which would be circular). ``betting.py`` mixes this in and
re-exports the module-level symbols callers import from it.
"""
import discord
from discord.ext import commands

from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.cogs._betting_engine import BettingCogError
from tle.cogs._betting_helpers import (
    unknown_subcommand_token, _COIN, _is_archived,
)


_ARCHIVED_NOTICE = ('World cup has ended, congrats to the best bettors! '
                    'The leaderboard is archived.')

_CHANNEL_CONFIG_KEY = 'bet_channel'


def _bet_channel_is_set(ctx):
    """Help predicate: hide `;bet here` from `;help bet` once a channel is
    already configured (setup is done — only an unconfigured guild needs it)."""
    try:
        return bool(ctx and ctx.guild and cf_common.user_db is not None
                    and cf_common.user_db.get_guild_config(
                        ctx.guild.id, _CHANNEL_CONFIG_KEY))
    except Exception:
        return False


class BetGroupMixin:
    @commands.group(name='bet',
                    aliases=['betting', 'prediction', 'pred', 'wager'],
                    brief='Betting', invoke_without_command=True)
    async def bet(self, ctx):
        """Show the active market here and your balance."""
        if _is_archived(ctx.guild.id):
            await ctx.send(embed=discord_common.embed_neutral(_ARCHIVED_NOTICE))
            return
        # `invoke_without_command=True` routes `;bet <unknown>` here too;
        # discord.py wipes `ctx.subcommand_passed` first, so recover the token
        # from the raw message and error instead of acting like a bare `;bet`.
        attempted = unknown_subcommand_token(ctx)
        if attempted:
            raise BettingCogError(
                f'`{discord.utils.escape_markdown(attempted)}` isn\'t a `;bet` '
                'command. See `;help bet` for the full list.')
        balance = cf_common.user_db.bet_ensure_wallet(
            ctx.guild.id, ctx.author.id, self._bet_start_balance(ctx.guild.id))
        market = self._find_market(ctx)
        if market is None:
            configured = cf_common.user_db.get_guild_config(
                ctx.guild.id, _CHANNEL_CONFIG_KEY)
            hint = ('Markets auto-open ~6h before each World Cup kickoff'
                    if configured else
                    'An admin can run `;prediction here` to start auto-opening '
                    'World Cup markets in a channel')
            await ctx.send(embed=discord_common.embed_neutral(
                f'No open market here. You have **{balance}** {_COIN}.\n'
                f'{hint}. See `;help bet`.'))
            return
        embed = self._market_embed(market, current_channel_id=ctx.channel.id)
        embed.set_footer(text=f'Your balance: {balance} coins')
        await ctx.send(embed=embed)
