"""User-created parimutuel markets: commands, permissions, bet flow and rendering.

``BetCustomMixin`` inherits ``BetCustomFormatMixin`` (rendering) and is mixed
into ``Betting`` ahead of the soccer command/format mixins, so its per-kind
hooks — pick resolution, settlement and the three render hooks — delegate to
them via ``super()`` for soccer markets. Everything shared — bet placement
(``_execute_bet``), withdrawal, mybet, book, cancel, close, the thread
lifecycle — runs through the generic market access and the soccer paths, so
this mixin only carries what is genuinely custom: opening a market, listing
them, parimutuel settlement, the ``super()`` kind hooks, and the
feature-flag guard. The ``;bet open`` grammar / choice-slug parsing helpers
below are pure (no cog/DB state).
"""
import logging
import re
import time
import unicodedata

import discord

from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util.db.betting_market_db import parimutuel_odds
from tle.cogs._betting_engine import BettingCogError
from tle.cogs._betting_format import discord_ts, final_results_embed
from tle.cogs._betting_helpers import _COIN, _is_custom_enabled, _no_mentions

logger = logging.getLogger(__name__)

MAX_LISTED_MARKETS = 10
MAX_OPEN_MARKETS_PER_USER = 5
# Markets auto-close this long after opening unless the creator passes an
# explicit ``:: <duration>``; early close is ``;bet close``.
DEFAULT_CLOSE_SECONDS = 30 * 86400

MIN_CHOICES = 2
MAX_CHOICES = 10
MAX_CHOICE_WORDS = 3
MAX_TITLE_LEN = 100
MAX_CHOICE_LEN = 40

OPEN_USAGE = ('Use `;bet open <title> :: <choice> | <choice> [| ...] '
              '[:: <duration>]`, e.g. `;bet open Will it rain? :: Yes | No :: 2h`.')

_DURATION_RE = re.compile(r'^(\d+)\s*([mhd])$')
_DURATION_UNITS = {'m': 60, 'h': 3600, 'd': 86400}


def parse_duration(token):
    """Parse ``2h``/``45m``/``1d`` into seconds, or None."""
    match = _DURATION_RE.match((token or '').strip().lower())
    if match is None:
        return None
    value = int(match.group(1))
    if value <= 0:
        return None
    return value * _DURATION_UNITS[match.group(2)]


def choice_key(label):
    """Fold a choice label to a comparison key (accents stripped, lowercase,
    alphanumerics only)."""
    decomposed = unicodedata.normalize('NFKD', label or '')
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return ''.join(c for c in stripped.lower() if c.isalnum()) or 'choice'


def build_choices(labels):
    """Validate labels and return [(choice_key, label)]. Raises ValueError with
    a user-facing message on an empty/duplicate/oversized choice."""
    out = []
    seen = set()
    for label in labels:
        label = ' '.join(label.split())
        if not label:
            raise ValueError('Every choice needs a name.')
        if len(label) > MAX_CHOICE_LEN:
            raise ValueError(
                f'Choice “{label[:20]}…” is too long (max {MAX_CHOICE_LEN} '
                'characters).')
        if len(label.split()) > MAX_CHOICE_WORDS:
            raise ValueError(
                f'Choice “{label}” is too many words (max {MAX_CHOICE_WORDS}).')
        key = choice_key(label)
        if key in seen:
            raise ValueError(f'Duplicate choice “{label}”.')
        seen.add(key)
        out.append((key, label))
    return out


def parse_open_args(text):
    """Parse ``<title> :: <choice> | <choice> [:: <duration>]`` into
    (title, labels, duration_seconds|None), or None if malformed."""
    if not text:
        return None
    parts = text.split('::')
    if len(parts) < 2 or len(parts) > 3:
        return None
    title = ' '.join(parts[0].split())
    if not title or len(title) > MAX_TITLE_LEN:
        return None
    labels = [choice.strip() for choice in parts[1].split('|')]
    labels = [label for label in labels if label]
    if not MIN_CHOICES <= len(labels) <= MAX_CHOICES:
        return None
    duration = None
    if len(parts) == 3:
        duration = parse_duration(parts[2])
        if duration is None:
            return None
    return (title, labels, duration)


def resolve_custom_pick(text, choices):
    """Resolve a pick token against a market's choices: exact key/label match,
    else an unambiguous prefix (>=2 chars). ``choices`` are rows with
    ``choice_key``/``label``. Returns the choice key or None."""
    if not text:
        return None
    key = choice_key(text)
    if not key:
        return None
    for row in choices:
        if key == row.choice_key or key == choice_key(row.label):
            return row.choice_key
    if len(key) >= 2:
        matches = [row.choice_key for row in choices
                   if row.choice_key.startswith(key)
                   or choice_key(row.label).startswith(key)]
        if len(matches) == 1:
            return matches[0]
    return None


class BetCustomFormatMixin:
    # ── Kind dispatch (the only kind checks in the render path) ────────

    def _thread_name(self, market):
        if market.kind != 'custom':
            return super()._thread_name(market)
        return f'🎲 {market.title} — bets'[:100]

    def _thread_intro_embed(self, market):
        if market.kind != 'custom':
            return super()._thread_intro_embed(market)
        return self._custom_thread_intro_embed(market)

    def _market_embed(self, market, *, current_channel_id=None):
        if market.kind != 'custom':
            return super()._market_embed(
                market, current_channel_id=current_channel_id)
        return self._custom_market_embed(
            market, current_channel_id=current_channel_id)

    # ── Helpers ────────────────────────────────────────────────────────

    def _custom_odds_text(self, total, totals, key):
        odds = parimutuel_odds(total, totals.get(key, 0))
        return f'{odds:.2f}' if odds is not None else '—'

    def _custom_choice_odds(self, market):
        """(label, odds_text) per choice from a single pool-totals read."""
        db = cf_common.user_db
        total, totals = db.bet_market_pool_totals(market.market_id)
        return [(row.label, self._custom_odds_text(total, totals, row.choice_key))
                for row in db.bet_custom_market_choices(market.market_id)]

    def _custom_close_text(self, market):
        return f'Closes: {discord_ts(market.closes_at)}'

    # ── Embeds ─────────────────────────────────────────────────────────

    def _custom_market_embed(self, market, *, current_channel_id=None):
        db = cf_common.user_db
        lines = [f'• **{label}** — **{odds}**'
                 for label, odds in self._custom_choice_odds(market)]
        lines.append('')
        if market.status != 'open':
            lines.append('🔒 **Market settled.**')
            color = 0x95a5a6
        elif db.bet_market_bettable(market):
            lines.append(self._custom_close_text(market))
            if market.thread_id:
                if str(current_channel_id) == str(market.thread_id):
                    lines.append('Reply in this thread with `<choice> <amount>` '
                                 'to bet.')
                else:
                    lines.append(f'Betting thread: <#{market.thread_id}>')
            else:
                lines.append('Bet with `;bet pick <choice> <amount>`.')
            color = 0x2ecc71
        else:
            lines.append('🔒 **Betting ended** — awaiting the result.')
            color = 0xf1c40f
        embed = discord.Embed(title=f'🎲 {market.title}',
                              description='\n'.join(lines), color=color)
        return self._add_pool_field(embed, market)

    def _custom_thread_intro_embed(self, market):
        lines = ['Reply in this thread to bet — use the **choice name**:', '']
        for label, odds in self._custom_choice_odds(market):
            lines.append(f'• `{label} 100` — **{label}** @ **{odds}**')
        lines.extend([
            '',
            'Returns split the whole pool between the winning backers '
            "(parimutuel). Re-bet a choice to change it; bets can't be "
            'withdrawn.',
            self._custom_close_text(market),
        ])
        embed = discord.Embed(title='🎟️ Place your bets',
                              description='\n'.join(lines), color=0x2ecc71)
        return self._add_pool_field(embed, market)

    def _custom_settlement_embed(self, market, winning_key, rows, total,
                                 winning_total):
        label = self._pick_label(market, winning_key)
        head = [f'**Winning choice: {label}**', '']
        if winning_total == 0:
            head.append('Nobody backed the winning choice — all stakes '
                        'were refunded.')
            shown = None
        else:
            shown = [(user_id, self._pick_label(market, pick), stake, payout,
                      '')
                     for user_id, pick, stake, payout in rows]
        return final_results_embed(
            f'✅ {market.title} — final', head, shown,
            'No bets were placed.', 0x2ecc71)


class BetCustomMixin(BetCustomFormatMixin):
    # ── Guards ─────────────────────────────────────────────────────────

    def _require_custom_enabled(self, ctx):
        if not _is_custom_enabled(ctx.guild.id):
            raise BettingCogError(
                'Custom markets are not enabled here. An admin can run '
                '`;meta config enable bet_custom`.')

    # ── Kind dispatch (custom branch; soccer via super()) ──────────────

    def _resolve_market_pick(self, market, text):
        if market.kind != 'custom':
            return super()._resolve_market_pick(market, text)
        choices = cf_common.user_db.bet_custom_market_choices(market.market_id)
        return resolve_custom_pick(text, choices)

    async def _settle_market(self, ctx, market, result):
        if market.kind != 'custom':
            return await super()._settle_market(ctx, market, result)
        return await self._custom_settle(ctx, market, result)

    # ── Commands ───────────────────────────────────────────────────────

    async def _cmd_custom_open(self, ctx, text):
        self._require_custom_enabled(ctx)
        parsed = parse_open_args(text)
        if parsed is None:
            raise BettingCogError(OPEN_USAGE)
        title, labels, duration = parsed
        try:
            choices = build_choices(labels)
        except ValueError as e:
            raise BettingCogError(str(e))
        db = cf_common.user_db
        if db.bet_market_get_active(ctx.guild.id, ctx.channel.id, kind=None):
            raise BettingCogError(
                'A market is already open in this channel. Settle or cancel '
                'it first.')
        open_count = db.bet_custom_open_count_for_user(
            ctx.guild.id, ctx.author.id)
        if open_count >= MAX_OPEN_MARKETS_PER_USER:
            raise BettingCogError(
                f'You already have {MAX_OPEN_MARKETS_PER_USER} open custom '
                'markets. Settle or cancel one before opening another.')
        now = time.time()
        closes_at = now + (duration or DEFAULT_CLOSE_SECONDS)
        market_id = db.bet_custom_market_create(
            ctx.guild.id, ctx.channel.id, title, ctx.author.id, now, closes_at)
        db.bet_custom_market_add_choices(market_id, choices)
        market = db.bet_market_get(market_id)
        try:
            msg = await ctx.send(embed=self._custom_market_embed(market))
        except discord.HTTPException:
            db.bet_void(ctx.guild.id, market_id, now)
            raise
        db.bet_market_set_message(market_id, msg.id)
        market = db.bet_market_get(market_id)
        thread = await self._create_thread(market_id, msg, market)
        if thread is None:
            await ctx.send(embed=discord_common.embed_alert(
                'Could not create a betting thread (missing "Create Public '
                'Threads" permission?). Bets can still be placed with '
                '`;bet pick <choice> <amount>`.'))
        self._schedule_close(market)
        logger.info('Opened custom market %s (%s) in guild %s',
                    market_id, title, ctx.guild.id)

    async def _cmd_custom_list(self, ctx):
        self._require_custom_enabled(ctx)
        db = cf_common.user_db
        markets = db.bet_markets_open(ctx.guild.id, kind='custom')
        if not markets:
            await ctx.send(embed=discord_common.embed_neutral(
                'No open custom markets here. Open one with '
                '`;bet open <title> :: <choice> | <choice>`.'))
            return
        lines = []
        for market in markets[:MAX_LISTED_MARKETS]:
            choices = ' · '.join(
                f'{label} **{odds}**'
                for label, odds in self._custom_choice_odds(market))
            ref = f'<#{market.thread_id}>' if market.thread_id \
                else f'<#{market.channel_id}>'
            lines.append(f'• **{market.title}** — {choices}\n'
                         f'    {ref} · {self._custom_close_text(market)}')
        embed = discord.Embed(
            title='🎲 Open custom markets',
            description='\n'.join(lines), color=0x3498db)
        await ctx.send(embed=embed, allowed_mentions=_no_mentions())

    async def _custom_settle(self, ctx, market, result):
        """Parimutuel settlement (permission already enforced by
        ``_cmd_settle``): winners split the whole pool by stake."""
        db = cf_common.user_db
        choices = db.bet_custom_market_choices(market.market_id)
        key = resolve_custom_pick(result, choices)
        if key is None:
            names = ' | '.join(row.label for row in choices)
            raise BettingCogError(
                f'Pick the winning choice ({names}) or its name.')
        outcome = db.bet_custom_settle(
            ctx.guild.id, market.market_id, key, time.time())
        if outcome is None:
            raise BettingCogError('That market is no longer open.')
        rows, total, winning_total = outcome
        embed = self._custom_settlement_embed(
            market, key, rows, total, winning_total)
        await self._announce_settlement(
            market, embed, 'Settled custom market %s → %s by %s',
            market.market_id, key, ctx.author.id)
