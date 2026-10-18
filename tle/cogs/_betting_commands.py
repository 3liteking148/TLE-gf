"""Implementation bodies for the shared ``;bet`` subcommands.

Plain mixin (not a ``commands.Cog``); mixed into ``Betting``. These commands
work on either market kind — settle/cancel/close dispatch per kind through the
``_settle_market`` hook and ``_require_market_moderator``, book renders both
kinds via the DB pick helpers. Soccer-only bodies live in
``_betting_soccer_cmds``; the command *callbacks* live in ``betting.py`` and
delegate here so each file stays under 500 lines.
"""
import logging
import time

import discord

from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.cogs._betting_engine import BettingCogError
from tle.cogs._betting_helpers import _COIN, _no_mentions

logger = logging.getLogger(__name__)


class BetCommandImplMixin:
    # ── Settle / cancel ────────────────────────────────────────────────

    async def _cmd_settle(self, ctx, result):
        """Settle the market found here — admin for soccer, creator-or-admin
        for custom markets. The result parsing and payout math are dispatched
        per kind through ``_settle_market``."""
        market = self._find_market(ctx, require_unambiguous=True)
        if market is None:
            raise BettingCogError('No open market here to settle.')
        self._require_market_moderator(ctx, market)
        await self._settle_market(ctx, market, result)

    async def _settle_market(self, ctx, market, result):
        """Soccer settlement: parse a scoreline/outcome and pay fixed odds.
        ``BetCustomMixin`` overrides this for custom markets."""
        parsed = self._parse_result(market, result)
        if parsed is None:
            raise BettingCogError(
                'Give the result as `home`, `draw`, `away`, a scoreline like '
                '`2-1`, or the winning team name.')
        outcome, home_score, away_score = parsed
        await self._do_settle(market, outcome, home_score, away_score,
                             source='manual')

    async def _cmd_cancel(self, ctx):
        market = self._find_market(ctx, require_unambiguous=True)
        if market is None:
            raise BettingCogError('No open market here to cancel.')
        self._require_market_moderator(ctx, market)
        refunds = cf_common.user_db.bet_void(
            market.guild_id, market.market_id, time.time())
        if refunds is None:
            raise BettingCogError('That market was just settled or cancelled.')
        total = sum(stake for _, stake in refunds)
        await ctx.send(embed=discord_common.embed_success(
            f'Market **{cf_common.user_db.bet_market_title(market)}** cancelled. '
            f'Refunded **{total}** {_COIN} across **{len(refunds)}** bet(s).'))
        await self._archive_thread(market)
        logger.info('Cancelled market %s in guild %s (%s refunds)',
                    market.market_id, ctx.guild.id, len(refunds))

    # ── Book / close ───────────────────────────────────────────────────

    async def _cmd_book(self, ctx):
        market = self._find_market(ctx, require_unambiguous=True)
        if market is None:
            raise BettingCogError('No open market here.')
        wagers = cf_common.user_db.bet_get_wagers(market.market_id)
        if not wagers:
            await ctx.send(embed=discord_common.embed_neutral('No bets placed yet.'))
            return
        by_pick = {}
        for w in wagers:
            by_pick.setdefault(w.pick, []).append(w)
        lines = []
        for pick in cf_common.user_db.bet_market_picks(market):
            ws = by_pick.get(pick) or []
            if not ws:
                continue
            odds = self._pick_odds(market, pick)
            total = sum(w.stake for w in ws)
            odds_text = f' @ {odds:.2f}' if odds is not None else ''
            lines.append(f'__{self._pick_label(market, pick)}{odds_text}__ — '
                         f'{len(ws)} bet(s), {total} {_COIN} staked')
            for w in sorted(ws, key=lambda x: x.stake, reverse=True)[:15]:
                payout = cf_common.user_db.bet_pick_potential(
                    market, pick, w.stake)
                payout = payout if payout is not None else 0
                lines.append(f'• <@{w.user_id}> {w.stake} → {payout} {_COIN}')
        embed = discord.Embed(
            title=f'📒 Book — {cf_common.user_db.bet_market_title(market)}',
            description='\n'.join(lines), color=0x3498db)
        await ctx.send(embed=embed, allowed_mentions=_no_mentions())

    async def _cmd_close(self, ctx):
        market = self._find_market(ctx, require_unambiguous=True)
        if market is None:
            raise BettingCogError('No open market here.')
        self._require_market_moderator(ctx, market)
        if cf_common.user_db.bet_market_close_betting(market.market_id):
            fresh = cf_common.user_db.bet_market_get(market.market_id)
            await self._announce_betting_closed(fresh, automatic=False)
            await ctx.send(embed=discord_common.embed_success(
                'Betting **closed early** — no more bets. The market still '
                'settles at full time.'))
        else:
            await ctx.send(embed=discord_common.embed_alert(
                'Betting was already closed on this market.'))
