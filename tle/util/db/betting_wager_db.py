"""Soccer betting settlement over the shared schema.

Operates on the shared ``bet_market`` / ``bet_wager`` tables plus the soccer
``bet_market_soccer`` extension (frozen odds and final scores). Generic market
and wager access lives in ``BettingMarketDbMixin``; the shared place/remove/void
mechanics in ``BettingWagerCoreMixin``. Uses wallet helpers provided by the
composed ``UserDbConn``; the ``_bet_*`` module helpers live in ``user_db_conn``
and are imported lazily to avoid an import cycle.
"""
import logging

from tle.util.db.betting_wager_core import aggregate_profit

logger = logging.getLogger(__name__)


class BettingWagerDbMixin:
    """Mixin providing soccer settlement and profit-stats DB methods."""

    def _market_odds_map(self, market_id):
        """{'home':o,'draw':o,'away':o} of a market's frozen odds, or None."""
        row = self.conn.execute(
            'SELECT odds_home, odds_draw, odds_away FROM bet_market_soccer '
            'WHERE market_id = ?', (market_id,)).fetchone()
        if row is None:
            return None
        return {'home': row.odds_home, 'draw': row.odds_draw,
                'away': row.odds_away}

    def bet_settle(self, guild_id, market_id, result, result_home, result_away,
                   settled_at):
        """Settle a market: credit each winning wager round(stake×odds) to its
        wallet and mark the market settled. Odds are the market's frozen
        odds_<pick>; payout is computed, not stored. Returns
        [(user_id, pick, stake, odds, payout)] for the announcement, or None if
        the market was not open (already settled or cancelled).

        The terminal status flip is the guard: it runs first and is scoped to
        ``status='open'``, so a second settle (or a settle racing the
        auto-poller) is a no-op and nobody is paid twice. Atomic.
        """
        from tle.util.db.user_db_conn import _bet_odds_for_pick, _bet_pick_wins
        guild_id = str(guild_id)
        with self.conn:
            changed = self.conn.execute(
                "UPDATE bet_market SET status = 'settled', result = ?, "
                'settled_at = ? '
                "WHERE market_id = ? AND status = 'open'",
                (result, settled_at, market_id)
            ).rowcount
            if changed == 0:
                return None
            self.conn.execute(
                'UPDATE bet_market_soccer SET result_home = ?, result_away = ? '
                'WHERE market_id = ?',
                (result_home, result_away, market_id)
            )
            odds_map = self._market_odds_map(market_id)
            wagers = self.conn.execute(
                'SELECT user_id, pick, stake FROM bet_wager WHERE market_id = ?',
                (market_id,)
            ).fetchall()

            def payout_for(w):
                odds = _bet_odds_for_pick(odds_map, w.pick)
                if _bet_pick_wins(w.pick, result) and odds is not None:
                    return int(round(w.stake * odds))
                return 0

            credited = self._credit_settlement(
                guild_id, market_id, wagers, settled_at, payout_for,
                action='payout', note=result)
            outcome = [(w.user_id, w.pick, w.stake,
                        _bet_odds_for_pick(odds_map, w.pick), payout)
                       for w, payout in credited]
        return outcome

    def bet_resettle(self, guild_id, market_id, new_result, new_home, new_away,
                     resettled_at):
        """Correct a settled market's result. Reverses the payouts that were
        credited under the OLD result and applies the new ones (per-wager
        delta), then stamps the new result. Returns
        [(user_id, pick, stake, odds, new_payout, delta)], or None if the
        market is not currently settled. Balances may go negative if a winner
        already spent the erroneous payout — use `;bet set` to reconcile. Atomic.
        """
        from tle.util.db.user_db_conn import _bet_odds_for_pick, _bet_pick_wins
        guild_id = str(guild_id)
        with self.conn:
            market = self.conn.execute(
                'SELECT m.result, e.odds_home, e.odds_draw, e.odds_away '
                'FROM bet_market m JOIN bet_market_soccer e '
                'ON e.market_id = m.market_id '
                "WHERE m.market_id = ? AND m.status = 'settled'", (market_id,)
            ).fetchone()
            if market is None:
                return None
            old_result = market.result
            odds_map = {'home': market.odds_home, 'draw': market.odds_draw,
                        'away': market.odds_away}
            self.conn.execute(
                'UPDATE bet_market SET result = ?, settled_at = ? '
                'WHERE market_id = ?',
                (new_result, resettled_at, market_id)
            )
            self.conn.execute(
                'UPDATE bet_market_soccer SET result_home = ?, result_away = ? '
                'WHERE market_id = ?',
                (new_home, new_away, market_id)
            )
            outcome = []
            wagers = self.conn.execute(
                'SELECT user_id, pick, stake FROM bet_wager WHERE market_id = ?',
                (market_id,)
            ).fetchall()
            for w in wagers:
                odds = _bet_odds_for_pick(odds_map, w.pick)
                old_pay = int(round(w.stake * odds)) \
                    if _bet_pick_wins(w.pick, old_result) and odds is not None else 0
                new_pay = int(round(w.stake * odds)) \
                    if _bet_pick_wins(w.pick, new_result) and odds is not None else 0
                delta = new_pay - old_pay
                if delta:
                    self._credit_wallet(guild_id, w.user_id, delta,
                                        'resettle_delta', market_id=market_id,
                                        note=new_result,
                                        created_at=resettled_at)
                outcome.append((w.user_id, w.pick, w.stake, odds, new_pay, delta))
        return outcome

    def bet_soccer_profit_rows(self, guild_id):
        """Realized profit per user over settled soccer markets:
        (user_id, profit, bets, wins) rows."""
        from tle.util.db.user_db_conn import _bet_odds_for_pick, _bet_pick_wins
        rows = self.conn.execute(
            'SELECT w.user_id, w.pick, w.stake, m.result, '
            '       e.odds_home, e.odds_draw, e.odds_away '
            'FROM bet_wager w JOIN bet_market m ON m.market_id = w.market_id '
            'JOIN bet_market_soccer e ON e.market_id = m.market_id '
            "WHERE m.guild_id = ? AND m.status = 'settled' AND m.kind = 'soccer'",
            (str(guild_id),)).fetchall()
        settled = []
        for row in rows:
            odds_map = {'home': row.odds_home, 'draw': row.odds_draw,
                        'away': row.odds_away}
            odds = _bet_odds_for_pick(odds_map, row.pick)
            won = _bet_pick_wins(row.pick, row.result)
            payout = int(round(row.stake * odds)) if won and odds is not None else 0
            settled.append((row.user_id, row.stake, payout, won))
        return aggregate_profit(settled)
