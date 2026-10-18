"""Soccer fixture listing and manual market opening for ``;bet``.

Plain mixin (not a ``commands.Cog``); mixed into ``Betting``. Holds the two
fixture-driven commands — ``;bet matches`` and the admin ``;bet openmatch`` —
and their implementation, split out of ``_betting_soccer_cmds`` to keep each
file under 500 lines.
"""
import logging
import time

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.cogs._betting_engine import BettingCogError
from tle.cogs._betting_group import BetGroupMixin

logger = logging.getLogger(__name__)

# The shared `;bet` command group (defined in ``_betting_group``), aliased to the
# same name the callbacks used when the whole tree lived in ``betting.py``.
bet = BetGroupMixin.bet

_MATCH_LIST_LIMIT = 15
_MATCH_CACHE_MAX_AGE = 10 * 60


class BetSoccerOpenMixin:
    # ── Matches / manual open ──────────────────────────────────────────

    @bet.command(name='matches', aliases=['games', 'fixtures'],
                 brief='List upcoming World Cup matches with odds',
                 usage='[query]')
    async def matches(self, ctx, *, query: str = None):
        """List upcoming World Cup matches (optionally filtered by team)."""
        await self._cmd_matches(ctx, query)

    @bet.command(name='openmatch', hidden=True,
                 brief='Manually open a World Cup fixture early (admin)',
                 usage='<number from ;bet matches | event_id>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def open_match(self, ctx, *, ref: str):
        """Open betting on a match from the last `;bet matches` list, early."""
        await self._cmd_open_market(ctx, ref)

    async def _cmd_matches(self, ctx, query):
        async with ctx.typing():
            events = await self._ensure_wc_events(_MATCH_CACHE_MAX_AGE)

        now = time.time()
        events = [e for e in events if e['commence_time'] > now]
        if query:
            q = query.strip().lower()
            events = [e for e in events
                      if q in e['home_team'].lower() or q in e['away_team'].lower()]
        events.sort(key=lambda e: e['commence_time'])
        if not events:
            raise BettingCogError(
                'No upcoming World Cup matches with odds found'
                + (f' for “{query}”.' if query else '.'))

        events = events[:_MATCH_LIST_LIMIT]
        self._match_cache[ctx.channel.id] = events
        lines = []
        for i, e in enumerate(events, 1):
            o = e['odds']
            ko = int(e['commence_time'])
            if (o.get('draw') or 0) > 1:
                odds_line = (f'1 **{o["home"]:.2f}** · X **{o["draw"]:.2f}** · '
                             f'2 **{o["away"]:.2f}**')
            else:
                odds_line = (f'to advance: 1 **{o["home"]:.2f}** · '
                             f'2 **{o["away"]:.2f}**')
            lines.append(
                f'**{i}.** {e["home_team"]} vs {e["away_team"]} — <t:{ko}:R>\n'
                f'    {odds_line}')
        embed = discord.Embed(title='⚽ Upcoming World Cup matches',
                              description='\n'.join(lines), color=0x3498db)
        embed.set_footer(text='Auto-opens ~6h before kickoff · '
                              'admins: ;bet open <number> to open early')
        await ctx.send(embed=embed)

    async def _cmd_open_market(self, ctx, ref):
        if cf_common.user_db.bet_market_get_active(
                ctx.guild.id, ctx.channel.id, kind=None):
            raise BettingCogError(
                'A market is already open in this channel. Settle or '
                '`;bet cancel` it first.')

        events = self._match_cache.get(ctx.channel.id)
        if not events:
            raise BettingCogError('Run `;bet matches` first, then '
                                  '`;bet open <number>`.')
        ref = ref.strip()
        if ref.isdigit() and 1 <= int(ref) <= len(events):
            event = events[int(ref) - 1]
        else:
            event = next((e for e in events if e['event_id'] == ref), None)
        if event is None:
            raise BettingCogError(
                f'`{discord.utils.escape_markdown(ref)}` is not in the current '
                'list. Run `;bet matches` again and use the row number.')

        if event['commence_time'] <= time.time():
            raise BettingCogError('That match has already kicked off.')
        if cf_common.user_db.bet_market_exists_open_for_event(
                ctx.guild.id, event['event_id']):
            raise BettingCogError('There is already an open market on that match.')
        duplicate = self._find_duplicate_match(ctx.guild.id, event)
        if duplicate is not None:
            raise BettingCogError(
                'There is already an open market on that match: '
                f'{self._market_place_ref(duplicate)}.')

        market_id = self._create_market(ctx.guild.id, ctx.channel.id, event)
        if market_id is None:
            raise BettingCogError('There is already an open market on that match.')
        suppress_mention = cf_common.user_db.bet_market_has_earlier_open_at_kickoff(
            ctx.guild.id, ctx.channel.id, event['commence_time'], market_id)
        try:
            msg = await ctx.send(
                **self._open_announcement_kwargs(
                    ctx.guild.id, event, suppress_mention=suppress_mention))
        except discord.HTTPException:
            cf_common.user_db.bet_void(ctx.guild.id, market_id, time.time())
            raise
        cf_common.user_db.bet_market_set_message(market_id, msg.id)
        market = cf_common.user_db.bet_market_get(market_id)
        thread = await self._create_thread(market_id, msg, market)
        if thread is None:
            await ctx.send(embed=discord_common.embed_alert(
                'Could not create a betting thread (missing "Create Public '
                'Threads" permission?). Bets can still be placed here with '
                '`;bet home/draw/away <amount>`.'))
        self._schedule_close(market)
        logger.info('Manually opened market %s (%s vs %s) in guild %s',
                    market_id, event['home_team'], event['away_team'], ctx.guild.id)
