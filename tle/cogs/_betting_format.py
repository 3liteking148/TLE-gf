"""Embed/label/notify-role formatting helpers for the betting cog.

Plain mixin (not a ``commands.Cog``); mixed into ``Betting`` so these read-only
presentation methods live off the command file.
"""
import time

import discord

from tle.util import codeforces_common as cf_common
from tle.cogs._betting_helpers import (
    _COIN, _odds_allow_draw, _role_mentions,
)

_NOTIFY_ROLE_CONFIG_KEY = 'bet_notify_role'


def bettor_results_lines(rows, *, limit=20):
    """Render the shared 'Bettor results (net)' block both settlement embeds use.

    ``rows`` is an iterable of ``(user_id, label, stake, payout, detail)`` where
    ``detail`` is an optional per-row suffix. Rows sort by net (then payout)
    descending. Returns ``(lines, total_staked, total_paid)``; ``lines`` is
    empty when there are no rows.
    """
    results = []
    total_staked = 0
    total_paid = 0
    for user_id, label, stake, payout, detail in rows:
        stake = int(stake or 0)
        payout = int(payout or 0)
        total_staked += stake
        total_paid += payout
        results.append((user_id, label, stake, payout, payout - stake, detail))
    results.sort(key=lambda r: (r[4], r[3]), reverse=True)
    lines = []
    if not results:
        return lines, total_staked, total_paid
    lines.append('**Bettor results (net):**')
    for user_id, label, stake, payout, net, detail in results[:limit]:
        sign = '+' if net > 0 else ''
        lines.append(f'<@{user_id}> **{sign}{net}** {_COIN} — {label} '
                     f'(staked {stake}{detail})')
    if len(results) > limit:
        lines.append(f'…and {len(results) - limit} more.')
    player_net = total_paid - total_staked
    sign = '+' if player_net > 0 else ''
    lines.append(
        f'\nTotal staked: **{total_staked}** {_COIN} · '
        f'paid out: **{total_paid}** {_COIN} · '
        f'player net: **{sign}{player_net}** {_COIN}.')
    return lines, total_staked, total_paid


def discord_ts(ts):
    """Absolute + relative Discord timestamp (``<t:F> (<t:R>)``), one place
    for the kickoff/close lines both market kinds render."""
    return f'<t:{int(ts)}:F> (<t:{int(ts)}:R>)'


def final_results_embed(title, head, rows, empty_text, color, footer=None):
    """Shared settlement-embed shell for both market kinds: pre-rendered
    headline lines plus the ``bettor_results_lines`` block (or ``empty_text``
    when there is nothing to show). ``rows=None`` renders the headline alone —
    a notice with no block, e.g. the full-refund case. Callers pass
    kind-specific words; no kind logic lives here."""
    lines = list(head)
    if rows is None:
        pass
    elif rows:
        block, _staked, _paid = bettor_results_lines(rows)
        lines.extend(block if block else [empty_text])
    else:
        lines.append(empty_text)
    embed = discord.Embed(title=title, description='\n'.join(lines), color=color)
    if footer is not None:
        embed.set_footer(text=footer)
    return embed


class BetFormatMixin:
    # ── Pick labels / odds (per kind, resolved in the DB layer) ────────

    def _pick_label(self, market, pick):
        return cf_common.user_db.bet_pick_label(market, pick)

    def _pick_odds(self, market, pick):
        return cf_common.user_db.bet_pick_odds(market, pick)

    def _pick_allowed(self, market, pick):
        return cf_common.user_db.bet_pick_allowed(market, pick)

    def _market_allows_draw(self, market):
        return cf_common.user_db.bet_market_allows_draw(market)

    def _remove_allowed(self, market):
        return cf_common.user_db.bet_market_allows_remove(market)

    def _not_odds_line(self, market):
        picks = ['home']
        if self._market_allows_draw(market):
            picks.append('draw')
        picks.append('away')
        parts = []
        for pick in picks:
            neg = f'not_{pick}'
            odds = self._pick_odds(market, neg)
            if odds is not None:
                parts.append(f'{self._pick_label(market, neg)} — **{odds:.2f}**')
        return ' · '.join(parts)

    # ── Pool summary ───────────────────────────────────────────────────

    def _pool_summary(self, market):
        pool = cf_common.user_db.bet_market_pool(market.market_id)
        if not pool:
            return None
        return ' · '.join(
            f'{self._pick_label(market, p.pick)}: {p.cnt} ({p.total} {_COIN})'
            for p in pool)

    def _add_pool_field(self, embed, market):
        summary = self._pool_summary(market)
        if summary:
            embed.add_field(name='Action so far', value=summary, inline=False)
        return embed

    # ── Notify role ────────────────────────────────────────────────────

    def _configured_notify_role_id(self, guild_id):
        if cf_common.user_db is None:
            return None
        role_id = cf_common.user_db.get_guild_config(
            guild_id, _NOTIFY_ROLE_CONFIG_KEY)
        if role_id is None:
            return None
        try:
            int(role_id)
        except (TypeError, ValueError):
            return None
        return str(role_id)

    def _configured_notify_role(self, guild):
        role_id = self._configured_notify_role_id(guild.id)
        if role_id is None or not hasattr(guild, 'get_role'):
            return None
        return guild.get_role(int(role_id))

    def _notify_role_mention(self, guild_id):
        role_id = self._configured_notify_role_id(guild_id)
        return f'<@&{role_id}>' if role_id is not None else None

    def _open_announcement_kwargs(self, guild_id, event, *,
                                  suppress_mention=False):
        kwargs = {'embed': self._open_announce_embed(event)}
        mention = None if suppress_mention else self._notify_role_mention(guild_id)
        if mention is not None:
            kwargs['content'] = mention
            kwargs['allowed_mentions'] = _role_mentions()
        return kwargs

    # ── Embeds ─────────────────────────────────────────────────────────

    def _market_embed(self, market, *, current_channel_id=None):
        now = time.time()
        open_now = (market.status == 'open' and now < market.closes_at
                    and not market.bets_closed)
        if self._market_allows_draw(market):
            lines = [
                f'**1** · {market.home_team} win — **{market.odds_home:.2f}**',
                f'**X** · Draw — **{market.odds_draw:.2f}**',
                f'**2** · {market.away_team} win — **{market.odds_away:.2f}**',
            ]
        else:
            lines = [
                f'**1** · {market.home_team} advances — **{market.odds_home:.2f}**',
                f'**2** · {market.away_team} advances — **{market.odds_away:.2f}**',
            ]
        not_line = self._not_odds_line(market)
        if not_line:
            lines.extend(['', f'Not bets: {not_line}'])
        lines.extend(['', f'Kickoff: {discord_ts(market.closes_at)}'])
        if open_now:
            if market.thread_id:
                if str(current_channel_id) == str(market.thread_id):
                    lines.append('\nReply in this thread to bet — '
                                 'betting closes at kickoff.')
                else:
                    lines.append(
                        f'\nBetting thread: <#{market.thread_id}> — '
                        'betting closes at kickoff.')
            else:
                lines.append('\nBet with `;bet home/draw/away <amount>` — '
                             'betting closes at kickoff.')
            color = 0x2ecc71
        elif market.status == 'open':
            if now < market.closes_at:
                lines.append('\n🔒 **Betting ended** — awaiting kickoff.')
            else:
                lines.append('\n🔒 **Betting ended** — awaiting result.')
            color = 0xf1c40f
        else:
            color = 0x95a5a6
        suffix = ' — who advances?' if not self._market_allows_draw(market) else ''
        embed = discord.Embed(
            title=f'⚽ {market.home_team} vs {market.away_team}{suffix}',
            description='\n'.join(lines), color=color)
        return self._add_pool_field(embed, market)

    def _thread_intro_embed(self, market):
        if self._market_allows_draw(market):
            odds_line = (f'Odds (fair/no-vig, frozen): **1** {market.odds_home:.2f} · '
                         f'**X** {market.odds_draw:.2f} · **2** {market.odds_away:.2f}')
            examples = (
                f'• `{market.home_team} 100` — back {market.home_team}\n'
                '• `draw 50` (or `tie`) — back a draw\n'
                f'• `{market.away_team} all` (also `25%`)\n'
                f'• `not {market.away_team} 25` — back {market.away_team} not winning')
        else:
            odds_line = (f'Odds to advance (fair/no-vig, frozen): '
                         f'**1** {market.odds_home:.2f} · '
                         f'**2** {market.odds_away:.2f}')
            examples = (
                f'• `{market.home_team} 100` — back {market.home_team} to advance\n'
                f'• `{market.away_team} all` — back {market.away_team} to advance\n'
                f'• `not {market.home_team} 25` — back {market.home_team} not advancing')
        not_line = self._not_odds_line(market)
        if not_line:
            odds_line += f'\nNot bets: {not_line}'
        pick_hint = 'home/draw/away' if self._market_allows_draw(market) else 'home/away'
        desc = (
            'Reply in this thread to bet — use the **country name** or '
            f'{pick_hint}:\n'
            f'{examples}\n'
            '\n'
            f'{odds_line}\n'
            f'Returns = stake × odds. Re-bet a pick to change it; use `0` '
            f'to remove that pick.\n'
            f'Kickoff: {discord_ts(market.closes_at)}\n'
            '⏱️ **Betting closes at kickoff.**')
        embed = discord.Embed(title='🎟️ Place your bets', description=desc,
                              color=0x2ecc71)
        return self._add_pool_field(embed, market)

    def _thread_name(self, market):
        name = f'⚽ {market.home_team} vs {market.away_team} — bets'
        return name[:100]

    def _open_announce_embed(self, event):
        """The 'betting open' announcement, built from a raw odds event so it
        can be posted BEFORE the market row exists (send-first, so a failed
        send never orphans a market)."""
        o = event['odds']
        if _odds_allow_draw(o):
            lines = [
                f'**1** · {event["home_team"]} win — **{o["home"]:.2f}**',
                f'**X** · Draw — **{o["draw"]:.2f}**',
                f'**2** · {event["away_team"]} win — **{o["away"]:.2f}**',
            ]
        else:
            lines = [
                f'**1** · {event["home_team"]} advances — **{o["home"]:.2f}**',
                f'**2** · {event["away_team"]} advances — **{o["away"]:.2f}**',
            ]
        lines.extend([
            '',
            # <t:..:R> renders as a live countdown on the client ("in 53 minutes").
            f'Kickoff: {discord_ts(event["commence_time"])}',
            '⏱️ **Betting closes at kickoff.**',
            '\n👇 **Place your bets in the thread below.**',
        ])
        suffix = ' — who advances?' if not _odds_allow_draw(o) else ''
        return discord.Embed(
            title=f'⚽ {event["home_team"]} vs {event["away_team"]}{suffix}',
            description='\n'.join(lines), color=0x2ecc71)

    def _settlement_embed(self, market, outcome, home_score, away_score,
                          outcome_rows, source):
        label = self._pick_label(market, outcome)
        if home_score is not None:
            headline = (f'{market.home_team} **{home_score}–{away_score}** '
                        f'{market.away_team}')
        else:
            headline = f'Result: **{label}**'
        rows = []
        for user_id, pick, stake, odds, pay in outcome_rows:
            odds_text = f' @ {odds:.2f}' if odds is not None else ''
            paid_text = f', paid {int(pay or 0)}' if pay else ''
            rows.append((user_id, self._pick_label(market, pick), stake, pay,
                         f'{paid_text}{odds_text}'))
        head = [headline, '', f'**Winning pick: {label}**']
        if not any(pay for _, _, _, _, pay in outcome_rows):
            head.append(f'Nobody backed **{label}**.')
        if rows:
            head.append('')
        tag = 'auto-settled from final score' if source == 'auto' \
            else 'settled by a moderator'
        return final_results_embed(
            f'✅ {market.home_team} vs {market.away_team} — final', head,
            rows, 'No bets were placed.', 0x2ecc71, footer=tag)

    def _wallet_txn_line(self, row):
        labels = {
            'init': 'wallet opened',
            'daily': 'daily claim',
            'wager_refund': 're-bet refund',
            'wager_stake': 'wager',
            'payout': 'payout',
            'resettle_delta': 'correction',
            'void_refund': 'void refund',
            'admin_grant': 'admin grant',
            'admin_take': 'admin take',
            'admin_setbalance': 'admin set balance',
            'profitadd': 'profit adjustment',
            'grantall': 'grant-all',
            'grantall_revert': 'grant-all revert',
            'mod_grant': 'mod grant',
            'mod_take': 'mod take',
            'mod_setbalance': 'mod set balance',
            'transfer_out': 'transfer sent',
            'transfer_in': 'transfer received',
            # The steal command was removed, but these labels stay so existing
            # ledger rows from before the removal still render cleanly.
            'steal_success': 'steal success',
            'steal_victim': 'stolen from',
            'steal_caught': 'caught stealing',
            'adjust': 'adjustment',
            'setbalance': 'set balance',
        }
        sign = '+' if row.amount > 0 else ''
        actor = ''
        if row.action == 'transfer_out':
            if row.actor_id and str(row.actor_id) != str(row.user_id):
                actor = f' by <@{row.actor_id}>'
        elif row.action == 'transfer_in':
            if row.actor_id and row.note and str(row.actor_id) != str(row.note):
                actor = f' by <@{row.actor_id}>'
        elif row.actor_id and str(row.actor_id) != str(row.user_id):
            actor = f' by <@{row.actor_id}>'
        market = f' · market #{row.market_id}' if row.market_id is not None else ''
        if row.action == 'transfer_out' and row.note:
            note = f' · to <@{row.note}>'
        elif row.action == 'transfer_in' and row.note:
            note = f' · from <@{row.note}>'
        elif row.action in ('steal_success', 'steal_caught') and row.note:
            note = f' · target <@{row.note}>'
        elif row.action == 'steal_victim' and row.note:
            note = f' · thief <@{row.note}>'
        else:
            note = f' · {discord.utils.escape_markdown(str(row.note))}' if row.note else ''
        label = labels.get(row.action, row.action.replace('_', ' '))
        return (f'<t:{int(row.created_at)}:R> — **{sign}{row.amount}** {_COIN} '
                f'({label}{actor}{market}{note}) → **{row.balance_after}**')
