"""Betting minigame: archived World Cup soccer + user-created markets.

`;meta config enable bet_archived` retires betting per guild: bare `;bet`
answers with a farewell notice, subcommands other than `leaderboard`,
`me`/`profile` and admin `profitadd` refuse (``cog_check``), the thread bet
listener goes quiet, and the scheduler/settlement passes skip the guild — no
odds/scores API traffic. `;meta config disable bet_archived` restores all.

`;meta config enable bet_custom` unlocks user-created parimutuel markets:
`;bet open <title> :: <choice> | <choice> [:: <duration>]` posts a market and
thread (creator-or-admin settles); winners split the pool by stake, sharing
the soccer wallet/ledger/leaderboards.

How the live soccer game worked: after `;prediction here`, ~6h before each
kickoff the bot froze The Odds API 1X2 odds, posted the market and opened a
thread; members bet via `;bet home|draw|away <amt>` or thread replies (`home
100`, `away all`). Betting closed at kickoff; full time auto-settled at stake
× odds. Wallets seeded 1000 coins, `;bet daily` +100. See the class body for
the full wallet/admin subcommand set.

Split across helper modules to stay under 500 lines/file: ``_betting_group``
(the ``bet`` group), ``_betting_soccer_cmds`` / ``_betting_soccer_open``
(soccer-only commands), ``_betting_commands`` (shared command bodies),
``_betting_wallet_cmds``, ``_betting_custom`` (custom markets),
``_betting_format``, ``_betting_engine``, ``_betting_settlement``,
``_betting_scheduler``. This file keeps the cog: the lifecycle, the thread
bet listener + background tasks, ``cog_check`` and the remaining
``@bet.command`` callbacks in one class body.
"""
import asyncio
import logging

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util import tasks

from tle.cogs._betting_commands import BetCommandImplMixin
from tle.cogs._betting_custom import BetCustomMixin
from tle.cogs._betting_engine import BetEngineMixin, BettingCogError
from tle.cogs._betting_format import BetFormatMixin
from tle.cogs._betting_group import (
    BetGroupMixin, _ARCHIVED_NOTICE, _bet_channel_is_set,  # noqa: F401
)
from tle.cogs._betting_remediation import BetRemediationMixin
from tle.cogs._betting_scheduler import BetSchedulerMixin
from tle.cogs._betting_settlement import BetSettlementMixin
from tle.cogs._betting_soccer_cmds import BetSoccerCmdMixin
from tle.cogs._betting_soccer_open import BetSoccerOpenMixin
from tle.cogs._betting_wallet_cmds import BetWalletCmdImplMixin
# Re-export the pure helpers so `from tle.cogs.betting import <helper>` keeps
# working for callers and tests.
from tle.cogs._betting_helpers import (  # noqa: F401
    extract_bet_tokens, is_due, is_remove_amount, normalize_event,
    normalize_pick, normalized_market_odds, outcome_from_score, parse_amount,
    parse_settle_arg, payout_amount, pick_is_negative, pick_wins, positive_pick,
    rank_line, resolve_bet_pick, resolve_pick, seconds_until_open,
    unknown_subcommand_token,
    _COIN, _api_key, _bot_prefix, _football_data_key, _is_archived,
    _no_mentions, _role_mentions, _short_error, _utc_today,
)

logger = logging.getLogger(__name__)

# The shared `;bet` group (defined in `_betting_group`), aliased to the same name
# the callbacks below used before the tree was split across modules.
bet = BetGroupMixin.bet

# Subcommands that still respond after `;meta config enable bet_archived`.
_ARCHIVE_ALLOWED = {'leaderboard', 'me', 'profitadd'}

# Precise per-fixture asyncio timers open each market at exactly kickoff −
# BET_OPEN_LEAD_SECONDS (mirroring rpoll's per-poll expiry timers). The safety
# net is only a coarse backstop: it re-arms timers for new fixtures and catches
# anything a missed timer / restart left in-window.
_SAFETY_NET_INTERVAL = 15 * 60
# Auto-settle poller cadence. Results come from football-data.org (free), so we
# can poll often; only hits the network when a market is actually past kickoff.
_SETTLE_INTERVAL = 60


# ── Cog ────────────────────────────────────────────────────────────────────

class Betting(BetCustomMixin, BetSoccerCmdMixin, BetSoccerOpenMixin,
              BetWalletCmdImplMixin, BetCommandImplMixin, BetFormatMixin,
              BetEngineMixin, BetSettlementMixin, BetSchedulerMixin,
              BetRemediationMixin, BetGroupMixin,
              commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # channel_id -> events shown by the last `;bet matches` (for `;bet open <n>`)
        self._match_cache = {}
        # Shared cache of the last World Cup odds fetch (schedule + frozen-able
        # odds), reused by the scheduler, open timers and `;bet matches`.
        self._wc_events = None
        self._wc_fetched_at = None
        # Cache of the football-data fixture list, which carries each match's
        # tournament stage (group vs knockout) — used to shape new markets.
        self._fd_matches = None
        self._fd_fetched_at = None
        # fixture_key -> asyncio.Task: precise per-fixture "open at kickoff − 6h"
        # timers. Provider event ids can drift, so timers use canonical fixtures.
        self._open_timers = {}
        # market_id -> asyncio.Task: edit/announce exactly when betting closes.
        self._close_timers = {}
        # market_id -> asyncio.Task: coalesced thread intro pool refresh.
        self._pool_refresh_timers = {}
        # market_id -> asyncio.Task: lock a settled market's thread 12h after
        # full time; re-armed from DB on restart (timers don't survive one).
        self._lock_timers = {}
        # market_id -> (outcome, home_score, away_score) or None: the
        # beyond-regulation football-data result seen last poll. Such games settle
        # once two consecutive polls agree, filtering the feed's transient
        # (sometimes wrong) shootout readings. In-memory: a restart just re-waits
        # one round, which is safe.
        self._fd_pending_confirm = {}

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self):
        # user_db may be set after cog listeners run — wait briefly (as rpoll
        # does) before arming timers.
        for _ in range(30):
            if cf_common.user_db is not None:
                break
            await asyncio.sleep(1)
        if cf_common.user_db is None:
            logger.warning('betting: user_db still None after waiting; skipping')
            return
        await self._refresh_schedule()   # arm open timers + catch in-window games
        await self._arm_close_timers()   # restore close timers after restart
        try:
            await self._arm_lock_timers()  # restore delayed thread-lock timers
        except Exception:  # never abort startup before the settle task starts
            logger.warning('bet lock timer restore failed', exc_info=True)
        await self._run_draw_refixture()  # one-time: fix mislabelled no-draw markets
        self._safety_net_task.start()
        self._settle_task.start()

    async def cog_unload(self):
        await self._safety_net_task.stop()
        await self._settle_task.stop()
        for timers in (self._open_timers, self._close_timers,
                       self._pool_refresh_timers, self._lock_timers):
            for task in list(timers.values()):
                if not task.done():
                    task.cancel()
            timers.clear()

    # ── Listener / background tasks ────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message):
        """Treat a plain `pick amount` message inside a betting thread as a
        bet — soccer (team/outcome) or custom (choice name). Cheap pre-filters
        keep this off the DB for ordinary chatter."""
        if message.author.bot or message.guild is None:
            return
        content = message.content or ''
        if content.startswith(_bot_prefix()):
            return  # a command — let the command system handle it
        tokens = extract_bet_tokens(content)
        if tokens is None:
            return
        if cf_common.user_db is None:
            return  # startup window — DB not initialized yet
        if _is_archived(message.guild.id):
            return  # betting retired here — thread bets are dead
        market = cf_common.user_db.bet_market_get_active_by_thread(
            message.guild.id, message.channel.id, kind=None)
        if market is None:
            return  # not a betting thread — ignored on purpose
        pick_text, amount_str = tokens
        pick = self._resolve_market_pick(market, pick_text)
        if pick is None:
            return  # not a recognizable choice — ignore (avoid chat noise)
        try:
            status, data = await self._execute_bet(
                message.guild.id, market, message.author, pick, amount_str)
        except Exception:
            logger.warning('thread bet failed in market %s', market.market_id,
                           exc_info=True)
            return
        if status in ('ok', 'removed', 'unchanged'):
            await self._react(message, '✅')
            if status != 'unchanged':
                self._schedule_pool_refresh(market.market_id)
        elif status == 'closed':
            await self._react(message, '🔒')
        elif status == 'insufficient':
            await self._react(message, '❌')
        else:  # invalid
            await self._react(message, '❓')

    @tasks.task_spec(name='BetSafetyNet',
                     waiter=tasks.Waiter.fixed_delay(_SAFETY_NET_INTERVAL))
    async def _safety_net_task(self, _):
        # Backstop only: arm timers for newly-listed fixtures and catch any
        # game a missed timer / restart left inside the window. The on-time
        # opening itself is done by the per-fixture timers, not this sweep.
        if cf_common.user_db is None:
            return
        try:
            await self._refresh_schedule()
        except Exception:
            logger.warning('bet schedule refresh failed', exc_info=True)
        try:
            await self._arm_close_timers()
        except Exception:
            logger.warning('bet close timer refresh failed', exc_info=True)
        try:
            await self._arm_lock_timers()
        except Exception:
            logger.warning('bet lock timer refresh failed', exc_info=True)

    @tasks.task_spec(name='BetSettle',
                     waiter=tasks.Waiter.fixed_delay(_SETTLE_INTERVAL))
    async def _settle_task(self, _):
        if cf_common.user_db is None:
            return
        try:
            await self._settle_pending()
        except Exception:
            logger.warning('bet auto-settle pass failed', exc_info=True)

    # ── Group ──────────────────────────────────────────────────────────

    async def cog_check(self, ctx):
        """In archived guilds only _ARCHIVE_ALLOWED subcommands still run."""
        if ctx.guild is None or not _is_archived(ctx.guild.id):
            return True
        command = ctx.command
        if command is None or command.qualified_name == 'bet' \
                or command.name in _ARCHIVE_ALLOWED:
            return True
        raise BettingCogError(_ARCHIVED_NOTICE)

    # ── Matches / manual open ──────────────────────────────────────────

    @bet.command(name='open',
                 brief='Open your own market (needs `;meta config enable bet_custom`)',
                 usage='<title> :: <choice> | <choice> [| ...] [:: 2h]')
    async def open_market(self, ctx, *, text: str):
        """Open a custom parimutuel market, e.g.
        `;bet open Will it rain? :: Yes | No :: 2h`. Markets close 30 days
        after opening unless you pass a shorter `:: <duration>`; settle with
        `;bet settle <choice>`."""
        await self._cmd_custom_open(ctx, text)

    @bet.command(name='pick',
                 brief='Bet on a pick of the active market',
                 usage='<choice | team | home/draw/away> <amount | 50% | all | 0 to remove>')
    async def pick(self, ctx, *, text: str):
        """Place (or with `0` remove) a bet on the active market's pick —
        choice names on custom markets, teams/outcomes on soccer."""
        await self._cmd_pick(ctx, text)

    @bet.command(name='list',
                 brief='List open custom markets in this server')
    async def list_markets(self, ctx):
        await self._cmd_custom_list(ctx)

    # ── Placing bets ───────────────────────────────────────────────────

    @bet.command(name='mybet', aliases=['mybets'], brief='Show your active bet')
    async def mybet(self, ctx):
        await self._cmd_mybet(ctx)

    @bet.command(name='withdraw', aliases=['clear', 'removeall', 'unbet', 'cancelbets'],
                 brief='Remove all your bets on the active match')
    async def withdraw(self, ctx):
        await self._withdraw_match(ctx)

    @bet.command(name='for', aliases=['forcebet', 'betfor', 'placefor'], hidden=True,
                 brief='Place a bet on behalf of a user (admin)',
                 usage='@user <choice | home|draw|away|team> <amount | 50% | all | 0 to remove>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def bet_for(self, ctx, member: discord.Member, *, text: str):
        """Place (or with `0` remove) a bet for an absent member. Spends their
        own wallet; you're recorded as the actor in the wallet history."""
        await self._cmd_place_for(ctx, member, text)

    # ── Wallet ─────────────────────────────────────────────────────────

    @bet.command(name='balance', aliases=['bal', 'wallet'], brief='Show a balance',
                 usage='[@user]')
    async def balance(self, ctx, member: discord.Member = None):
        await self._cmd_balance(ctx, member)

    @bet.command(name='me', aliases=['profile', 'summary'],
                 brief='Show your betting summary')
    async def me(self, ctx):
        await self._cmd_me(ctx)

    @bet.command(name='daily', aliases=['claim'], brief='Claim the daily allowance')
    async def daily(self, ctx):
        await self._cmd_daily(ctx)

    @bet.command(name='transfer', aliases=['send', 'pay'], hidden=True,
                 brief='Move coins from one user to another (admin)',
                 usage='@from @to <amount|all|percent>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def transfer(self, ctx, from_member: discord.Member,
                       to_member: discord.Member, amount: str):
        await self._cmd_transfer(ctx, from_member, to_member, amount)

    @bet.command(name='history', aliases=['walletlog', 'ledger'], hidden=True,
                 brief='Show wallet audit history', usage='[@user]')
    async def history(self, ctx, member: discord.Member = None):
        await self._cmd_history(ctx, member)

    # ── Leaderboard ────────────────────────────────────────────────────

    @bet.command(name='leaderboard', aliases=['lb', 'board', 'top'],
                 brief='Wallet leaderboard (add `profit` for net profit)',
                 usage='[profit]')
    async def leaderboard(self, ctx, mode: str = None):
        await self._cmd_leaderboard(ctx, mode)

    # ── Settle / cancel / pending / correct ────────────────────────────

    @bet.command(name='settle',
                 brief='Settle the active market (admin; custom: creator)',
                 usage='<home|draw|away|2-1 | choice>')
    async def settle(self, ctx, *, result: str):
        await self._cmd_settle(ctx, result)

    @bet.command(name='cancel', aliases=['void'],
                 brief='Cancel the active market and refund (admin; custom: creator)')
    async def cancel(self, ctx):
        await self._cmd_cancel(ctx)

    @bet.command(name='grant',
                 brief='Give a user coins; a negative amount takes (admin)',
                 usage='@user <amount | -amount>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def grant(self, ctx, member: discord.Member, amount: int):
        await self._cmd_grant(ctx, member, amount)

    @bet.command(name='profitadd', aliases=['addprofit'],
                 brief='Credit coins that also count as profit (admin)',
                 usage='@user <amount | -amount>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def profitadd(self, ctx, member: discord.Member, amount: int):
        """For winning bets the bot never recorded (e.g. placed at the last
        second): credit coins that also count in `;bet leaderboard profit`.
        Negative reverts a mistaken add. Works while archived."""
        await self._cmd_profitadd(ctx, member, amount)

    @bet.command(name='setbalance', aliases=['setbal'], hidden=True,
                 brief='Set a user\'s balance (admin)', usage='@user <amount>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def setbalance(self, ctx, member: discord.Member, amount: int):
        await self._cmd_setbalance(ctx, member, amount)

    @bet.command(name='grantall', aliases=['granteveryone', 'allgrant'],
                 brief='Give every wallet coins; a negative amount reverts (admin)',
                 usage='<amount | -amount>')
    @commands.has_any_role(*constants.TLE_ADMIN)
    async def grantall(self, ctx, amount: int):
        await self._cmd_grant_all(ctx, amount)

    @bet.command(name='book', hidden=True,
                 brief='Show all bets on the active market')
    async def book(self, ctx):
        await self._cmd_book(ctx)

    @bet.command(name='close',
                 brief='Close betting early (admin; custom: creator)')
    async def close(self, ctx):
        await self._cmd_close(ctx)

    @discord_common.send_error_if(BettingCogError)
    async def cog_command_error(self, ctx, error):
        pass


async def setup(bot):
    await bot.add_cog(Betting(bot))
