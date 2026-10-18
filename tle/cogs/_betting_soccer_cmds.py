"""Soccer-only ``;bet`` commands and their implementation bodies.

Plain mixin (not a ``commands.Cog``); mixed into ``Betting``. The World Cup
game is the archived, fixed-odds half of the cog: its setup/inspection commands
(``here``/``notifyrole``/``notify``/``check``), the fixed-outcome picks
(``home``/``draw``/``away``), the fixed-odds-only admin commands
(``not``/``odds``), the soccer maintenance commands (``pending``/``correct``)
and the auto-open scheduler controls (``pause``/``resume``) live here. The
fixture-listing pair lives in ``_betting_soccer_open`` to keep each file under
500 lines.
"""
import logging
import time

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util import football_data
from tle.util import odds_api
from tle.cogs._betting_engine import BettingCogError
from tle.cogs._betting_group import BetGroupMixin, _bet_channel_is_set
from tle.cogs._betting_helpers import (
    normalized_market_odds, _COIN, _api_key,
    _football_data_key, _no_mentions, _short_error,
)

logger = logging.getLogger(__name__)

# The shared `;bet` command group (defined in ``_betting_group``), aliased to the
# same name the callbacks used when the whole tree lived in ``betting.py``.
bet = BetGroupMixin.bet

_CHANNEL_CONFIG_KEY = 'bet_channel'
_NOTIFY_ROLE_CONFIG_KEY = 'bet_notify_role'
_PAUSED_CONFIG_KEY = 'bet_paused'


class BetSoccerCmdMixin:
    # ── Commands ───────────────────────────────────────────────────────

    @bet.command(name='here',
                 brief='Set this channel for auto-opened World Cup markets (admin)',
                 extras={'help_hidden_when': _bet_channel_is_set})
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def here(self, ctx):
        """Designate this channel as where the bot auto-posts markets."""
        await self._cmd_here(ctx)

    @bet.command(name='notifyrole', aliases=['pingrole'],
                 brief='Set/clear the role pinged when a market opens (admin)',
                 usage='[@role | off]')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def notifyrole(self, ctx, role: str = None):
        await self._cmd_notifyrole(ctx, role)

    @bet.command(name='notify', aliases=['notifications'],
                 brief='Toggle betting notifications for yourself')
    async def notify(self, ctx):
        await self._cmd_notify(ctx)

    @bet.command(name='check', hidden=True,
                 brief='Check betting API keys without exposing secrets (admin)')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def check(self, ctx):
        """Verify that the betting API keys are configured and usable."""
        await self._cmd_check(ctx)

    @bet.command(name='home', aliases=['1'], brief='Bet on the home win',
                 usage='<amount | 50% | all | 0 to remove>')
    async def bet_home(self, ctx, amount: str):
        await self._place_outcome(ctx, 'home', amount)

    @bet.command(name='draw', aliases=['x', 'tie'], brief='Bet on a draw',
                 usage='<amount | 50% | all | 0 to remove>')
    async def bet_draw(self, ctx, amount: str):
        await self._place_outcome(ctx, 'draw', amount)

    @bet.command(name='away', aliases=['2'], brief='Bet on the away win',
                 usage='<amount | 50% | all | 0 to remove>')
    async def bet_away(self, ctx, amount: str):
        await self._place_outcome(ctx, 'away', amount)

    @bet.command(name='not', aliases=['no'], hidden=True,
                 brief='Bet that an outcome will not happen',
                 usage='<team|home|draw|away> <amount | 50% | all | 0 to remove>')
    async def bet_not(self, ctx, *, text: str):
        await self._cmd_bet_not(ctx, text)

    @bet.command(name='pending', aliases=['stuck'], hidden=True,
                 brief='List open markets past kickoff awaiting a result')
    async def pending(self, ctx):
        """Markets past kickoff but unsettled (e.g. a scores-API gap); stakes
        stay escrowed until an admin `;bet settle`s or `;bet cancel`s."""
        await self._cmd_pending(ctx)

    @bet.command(name='correct', aliases=['fix', 'resettle'], hidden=True,
                 brief='Fix a wrongly-settled result (admin)',
                 usage='<home|draw|away|2-1|team>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def correct(self, ctx, *, result: str):
        """Re-settle the most recently settled market here with the corrected
        result, reversing the wrong payouts and applying the right ones."""
        await self._cmd_correct(ctx, result)

    @bet.command(name='pause', hidden=True,
                 brief='Stop auto-opening new markets (admin)')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def pause(self, ctx):
        cf_common.user_db.set_guild_config(ctx.guild.id, _PAUSED_CONFIG_KEY, '1')
        await ctx.send(embed=discord_common.embed_success(
            'Auto-open **paused** — no new markets will open. Existing markets '
            'still settle. `;bet resume` to re-enable.'))

    @bet.command(name='resume', aliases=['unpause'], hidden=True,
                 brief='Resume auto-opening markets (admin)')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def resume(self, ctx):
        cf_common.user_db.set_guild_config(ctx.guild.id, _PAUSED_CONFIG_KEY, '0')
        await ctx.send(embed=discord_common.embed_success(
            'Auto-open **resumed** — markets will open ~6h before kickoff again.'))

    @bet.command(name='odds', hidden=True,
                 brief='Re-line a market before any bets (admin)',
                 usage='<home> <draw> <away>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def setodds(self, ctx, home: float, draw: float, away: float):
        await self._cmd_setodds(ctx, home, draw, away)

    # ── Setup / config ─────────────────────────────────────────────────

    async def _cmd_here(self, ctx):
        cf_common.user_db.set_guild_config(
            ctx.guild.id, _CHANNEL_CONFIG_KEY, str(ctx.channel.id))
        note = ('' if _api_key() else
                '\n⚠️ No `ODDS_API_KEY` is set, so nothing will auto-open until '
                'one is configured.')
        await ctx.send(embed=discord_common.embed_success(
            f'World Cup markets will auto-open in {ctx.channel.mention} ~6h '
            f'before each kickoff, with a thread for bets.{note}'))
        # Arm timers now (and open anything already inside the 6h window) so we
        # don't wait for the next safety-net sweep.
        if _api_key():
            try:
                await self._refresh_schedule()
            except Exception:
                logger.warning('schedule refresh after `;prediction here` '
                               'failed', exc_info=True)

    async def _cmd_notifyrole(self, ctx, role):
        # `role` arrives as a raw string from the command (or None). `off`/`none`
        # /`clear` removes the configured role; no arg shows the current one;
        # anything else resolves to a Role. Tests pass a Role object directly,
        # so a non-str value is taken as an already-resolved role.
        if isinstance(role, str):
            token = role.strip()
            if token.lower() in ('off', 'none', 'clear', 'remove'):
                cf_common.user_db.delete_guild_config(
                    ctx.guild.id, _NOTIFY_ROLE_CONFIG_KEY)
                await ctx.send(embed=discord_common.embed_success(
                    'Betting notification role cleared.'))
                return
            try:
                role = await commands.RoleConverter().convert(ctx, token)
            except commands.BadArgument:
                raise BettingCogError(
                    f'No role matching “{discord.utils.escape_markdown(token)}”. '
                    'Use `;bet notifyrole @role` to set one or '
                    '`;bet notifyrole off` to clear it.')
        if role is None:
            role_id = self._configured_notify_role_id(ctx.guild.id)
            if role_id is None:
                await ctx.send(embed=discord_common.embed_neutral(
                    'No betting notification role is configured. Set one with '
                    '`;bet notifyrole @role`.'))
            else:
                await ctx.send(embed=discord_common.embed_neutral(
                    f'Betting notification role: <@&{role_id}>. '
                    'Clear it with `;bet notifyrole off`.'))
            return
        self._validate_notify_role(ctx, role)
        cf_common.user_db.set_guild_config(
            ctx.guild.id, _NOTIFY_ROLE_CONFIG_KEY, str(role.id))
        await ctx.send(embed=discord_common.embed_success(
            f'Betting markets will ping {role.mention} when they open.'),
            allowed_mentions=_no_mentions())

    async def _cmd_notify(self, ctx):
        role_id = self._configured_notify_role_id(ctx.guild.id)
        if role_id is None:
            raise BettingCogError(
                'No betting notification role is configured yet.')
        role = self._configured_notify_role(ctx.guild)
        if role is None:
            raise BettingCogError(
                'The configured betting notification role no longer exists.')
        try:
            if self._member_has_role(ctx.author, role_id):
                await ctx.author.remove_roles(role, reason='Betting notifications off')
                await ctx.send(embed=discord_common.embed_success(
                    f'Removed {role.mention} from you.'),
                    allowed_mentions=_no_mentions())
            else:
                await ctx.author.add_roles(role, reason='Betting notifications on')
                await ctx.send(embed=discord_common.embed_success(
                    f'Added {role.mention} to you.'),
                    allowed_mentions=_no_mentions())
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            raise BettingCogError(
                'I could not update that role. Check my role permissions.')

    async def _cmd_check(self, ctx):
        lines = ['Betting API check:']
        api_key = _api_key()
        if not api_key:
            lines.append('❌ `ODDS_API_KEY` is not set.')
        else:
            try:
                sports, quota = await odds_api.fetch_sports(
                    api_key, with_quota=True)
            except odds_api.OddsApiError as e:
                lines.append(f'❌ `ODDS_API_KEY` failed: `{_short_error(e)}`')
            else:
                wc = next((s for s in sports or []
                           if s.get('key') == odds_api.WORLD_CUP_SPORT_KEY), None)
                if wc is None:
                    lines.append(
                        f'⚠️ `ODDS_API_KEY` works, but '
                        f'`{odds_api.WORLD_CUP_SPORT_KEY}` is not listed as active.')
                else:
                    title = wc.get('title') or odds_api.WORLD_CUP_SPORT_KEY
                    lines.append(f'✅ `ODDS_API_KEY` works; `{title}` is active.')
                remaining = quota.get('remaining')
                if remaining is not None:
                    used = quota.get('used')
                    usage = f' ({used} used)' if used is not None else ''
                    lines.append(
                        f'📊 Odds API: **{remaining}** request(s) remaining '
                        f'this month{usage}.')

        fd_key = _football_data_key()
        if not fd_key:
            lines.append('❌ `FOOTBALL_DATA_API_KEY` is not set.')
        else:
            try:
                matches = await football_data.fetch_wc_matches(fd_key)
            except football_data.FootballDataError as e:
                lines.append(
                    f'❌ `FOOTBALL_DATA_API_KEY` failed: `{_short_error(e)}`')
            else:
                lines.append(
                    f'✅ `FOOTBALL_DATA_API_KEY` works; '
                    f'{len(matches)} World Cup match(es) returned.')

        lines.append('\nOdds check uses The Odds API `/sports` endpoint '
                     '(quota-free); the remaining count is read from its '
                     'response headers at no cost.')
        await ctx.send(embed=discord_common.embed_neutral('\n'.join(lines)))

    # ── Fixed-outcome picks ────────────────────────────────────────────

    async def _place_outcome(self, ctx, pick, amount):
        """Place a fixed 1X2 pick (``;bet home/draw/away``) — soccer only; a
        custom market has no free-standing home/draw/away outcomes."""
        market = self._find_market(ctx, require_unambiguous=True, bettable_only=True)
        if market is None:
            raise BettingCogError('No open market here.')
        if not cf_common.user_db.bet_market_has_fixed_odds(market):
            raise BettingCogError(
                '`;bet home/draw/away` only works on soccer markets. '
                'Use `;bet pick <choice>` for custom markets.')
        await self._place(ctx, pick, amount)

    # ── Settlement maintenance ─────────────────────────────────────────

    async def _cmd_pending(self, ctx):
        now = time.time()
        markets = [m for m in cf_common.user_db.bet_markets_open(ctx.guild.id)
                   if m.closes_at <= now]
        if not markets:
            await ctx.send(embed=discord_common.embed_neutral(
                'No markets are stuck — every open market is still pre-kickoff.'))
            return
        lines = []
        for m in markets:
            ch = f'<#{m.thread_id}>' if m.thread_id else f'<#{m.channel_id}>'
            lines.append(
                f'• **{m.home_team} vs {m.away_team}** — kicked off '
                f'<t:{int(m.closes_at)}:R> · {ch}')
        embed = discord.Embed(
            title='⏳ Markets awaiting a result',
            description='\n'.join(lines)
            + '\n\nAn admin can `;bet settle <home|draw|away|2-1>` or `;bet cancel` '
            'in each market\'s channel/thread.',
            color=0xf1c40f)
        await ctx.send(embed=embed, allowed_mentions=_no_mentions())

    async def _cmd_correct(self, ctx, result):
        market = (cf_common.user_db.bet_market_get_latest_settled_by_thread(
                      ctx.guild.id, ctx.channel.id)
                  or cf_common.user_db.bet_market_get_latest_settled_by_channel(
                      ctx.guild.id, ctx.channel.id))
        if market is None:
            raise BettingCogError(
                'No settled market here to correct. Run this in the match\'s '
                'thread or channel.')
        parsed = self._parse_result(market, result)
        if parsed is None:
            raise BettingCogError(
                'Give the corrected result as `home`/`draw`/`away`, a scoreline '
                'like `2-1`, or the winning team name.')
        outcome, home_score, away_score = parsed
        if not self._pick_allowed(market, outcome):
            raise BettingCogError('That result is not available for this market.')
        rows = cf_common.user_db.bet_resettle(
            market.guild_id, market.market_id, outcome, home_score, away_score,
            time.time())
        if rows is None:
            raise BettingCogError('That market is no longer in a settled state.')
        label = self._pick_label(market, outcome)
        adjusted = [r for r in rows if r[5] != 0]
        head = (f'{market.home_team} {home_score}–{away_score} {market.away_team}'
                if home_score is not None else f'winner: **{label}**')
        lines = [f'Corrected result: {head}']
        if adjusted:
            lines.append('')
            for user_id, pick, stake, odds, new_pay, delta in adjusted:
                sign = '+' if delta > 0 else ''
                lines.append(f'<@{user_id}> **{sign}{delta}** {_COIN}')
        else:
            lines.append('\nNo payouts changed.')
        embed = discord.Embed(
            title=f'🔧 Correction — {market.home_team} vs {market.away_team}',
            description='\n'.join(lines), color=0xe67e22)
        await ctx.send(embed=embed, allowed_mentions=_no_mentions())
        logger.info('Corrected market %s → %s by %s',
                    market.market_id, outcome, ctx.author.id)

    async def _cmd_setodds(self, ctx, home, draw, away):
        market = self._find_market(ctx, require_unambiguous=True)
        if market is None:
            raise BettingCogError('No open market here.')
        if not cf_common.user_db.bet_market_has_fixed_odds(market):
            raise BettingCogError('Odds can only be set on soccer markets.')
        if cf_common.user_db.bet_market_count_wagers(market.market_id) > 0:
            raise BettingCogError(
                'Bets are already placed — re-lining would be unfair to them. '
                '`;bet cancel` to refund and reopen instead.')
        if self._market_allows_draw(market):
            if not (home > 1 and draw > 1 and away > 1):
                raise BettingCogError('Odds must be decimal and greater than 1.0.')
            fair = normalized_market_odds(
                {'home': home, 'draw': draw, 'away': away}, knockout=False)
        else:
            if not (home > 1 and away > 1):
                raise BettingCogError('Home/away odds must be decimal and greater than 1.0.')
            fair = normalized_market_odds(
                {'home': home, 'draw': 0.0, 'away': away}, knockout=True)
        cf_common.user_db.bet_market_set_odds(
            market.market_id, fair['home'], fair['draw'], fair['away'])
        await ctx.send(embed=discord_common.embed_success(
            f'Odds re-lined: **1** {fair["home"]:.2f} · '
            + (f'**X** {fair["draw"]:.2f} · ' if fair['draw'] > 1 else '')
            + f'**2** {fair["away"]:.2f}.'))
