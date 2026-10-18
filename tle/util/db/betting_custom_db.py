"""User-created parimutuel market DB methods.

Custom markets live in the shared ``bet_market`` base (``kind='custom'``) with
their title in ``bet_market_custom`` and choices in ``bet_custom_choice``;
wagers use the shared ``bet_wager`` table. Everything else (lifecycle queries,
wager placement/removal, stats) runs through the generic ``BettingMarketDbMixin``
and the shared wager core. Parimutuel payouts are recomputed from the frozen
wagers at read time — never stored — so the wager table stays type-agnostic. The
wallet/ledger (``bet_wallet`` / ``bet_wallet_txn``) is the shared economy.
"""
from tle.util.db.betting_wager_core import aggregate_profit


def _parimutuel_payout(stake, pick, winning_key, total_pool, winning_pool):
    """A single backer's payout: winners split the whole pool by stake
    (``floor(stake * total / winning)``). When nobody backed the winner every
    stake is refunded instead (``payout == stake``)."""
    if winning_pool <= 0:
        return stake
    return (stake * total_pool) // winning_pool if pick == winning_key else 0


class BettingCustomDbMixin:
    """Mixin providing custom-market creation, parimutuel settlement and
    stats; the generic mixin (and the wager core) covers the rest."""

    # -- Markets --

    def bet_custom_market_create(self, guild_id, channel_id, title, created_by,
                                 created_at, closes_at):
        """Open a custom market and return its id."""
        with self.conn:
            cur = self.conn.execute(
                'INSERT INTO bet_market '
                '(kind, guild_id, channel_id, created_by, created_at, '
                "closes_at, status, removable) "
                "VALUES ('custom', ?, ?, ?, ?, ?, 'open', 0)",
                (str(guild_id), str(channel_id), str(created_by),
                 created_at, closes_at)
            )
            market_id = cur.lastrowid
            self.conn.execute(
                'INSERT INTO bet_market_custom (market_id, title) '
                'VALUES (?, ?)', (market_id, title))
        return market_id

    def bet_custom_market_add_choices(self, market_id, choices):
        """Insert the market's choices. ``choices`` is [(choice_key, label)]."""
        self.conn.executemany(
            'INSERT INTO bet_custom_choice (market_id, choice_key, label, position) '
            'VALUES (?, ?, ?, ?)',
            [(market_id, key, label, i) for i, (key, label) in enumerate(choices)]
        )
        self.conn.commit()

    def bet_custom_market_choices(self, market_id):
        """Return the market's choices ordered by position."""
        return self.conn.execute(
            'SELECT market_id, choice_key, label, position FROM bet_custom_choice '
            'WHERE market_id = ? ORDER BY position, choice_key',
            (market_id,)
        ).fetchall()

    def bet_custom_market_choice(self, market_id, choice_key):
        """Return one choice row, or None."""
        return self.conn.execute(
            'SELECT market_id, choice_key, label, position FROM bet_custom_choice '
            'WHERE market_id = ? AND choice_key = ?',
            (market_id, str(choice_key))
        ).fetchone()

    def bet_custom_open_count_for_user(self, guild_id, user_id):
        """Number of currently open custom markets created by this user."""
        return self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM bet_market "
            "WHERE guild_id = ? AND created_by = ? AND status = 'open' "
            "AND kind = 'custom'",
            (str(guild_id), str(user_id))
        ).fetchone().cnt

    # -- Settlement --

    def bet_custom_settle(self, guild_id, market_id, winning_key, settled_at):
        """Settle a parimutuel market: winners split the whole pool by stake.

        ``payout_i = floor(stake_i * total_pool / winning_pool)`` for backers of
        the winning choice; losers get 0. Payouts are not stored — they are
        recomputed from the frozen wagers (see ``bet_custom_profit_rows``). The
        status flip is the guard (scoped to ``status='open'``), so a second
        settle is a no-op. When nobody backed the winner every stake is refunded
        instead (payout = stake).

        Returns ``(rows, total_pool, winning_pool)`` where rows is
        [(user_id, pick, stake, payout)] for the announcement, or None if the
        market was not open.
        """
        guild_id = str(guild_id)
        winning_key = str(winning_key)
        with self.conn:
            changed = self.conn.execute(
                "UPDATE bet_market SET status = 'settled', result = ?, "
                "settled_at = ? WHERE market_id = ? AND status = 'open' "
                "AND kind = 'custom'",
                (winning_key, settled_at, market_id)
            ).rowcount
            if changed == 0:
                return None
            wagers = self.conn.execute(
                'SELECT user_id, pick, stake FROM bet_wager '
                'WHERE market_id = ? ORDER BY placed_at ASC',
                (market_id,)
            ).fetchall()
            total_pool = sum(w.stake for w in wagers)
            winning_pool = sum(w.stake for w in wagers if w.pick == winning_key)
            action = 'payout' if winning_pool > 0 else 'void_refund'
            credited = self._credit_settlement(
                guild_id, market_id, wagers, settled_at,
                lambda w: _parimutuel_payout(
                    w.stake, w.pick, winning_key, total_pool, winning_pool),
                action=action, note=winning_key)
            rows = [(w.user_id, w.pick, w.stake, payout)
                    for w, payout in credited]
        return (rows, total_pool, winning_pool)

    # -- Stats --

    def bet_custom_profit_rows(self, guild_id):
        """Realized profit per user over settled custom markets, recomputed from
        the frozen wagers (parimutuel payout depends only on the stakes and the
        winning choice, both immutable once settled). Each row: user_id, profit,
        bets, wins."""
        markets = self.conn.execute(
            "SELECT market_id, result FROM bet_market "
            "WHERE guild_id = ? AND status = 'settled' AND kind = 'custom'",
            (str(guild_id),)
        ).fetchall()
        settled = []
        for market in markets:
            wagers = self.conn.execute(
                'SELECT user_id, pick, stake FROM bet_wager WHERE market_id = ?',
                (market.market_id,)
            ).fetchall()
            total = sum(w.stake for w in wagers)
            winning = sum(w.stake for w in wagers if w.pick == market.result)
            for w in wagers:
                payout = _parimutuel_payout(
                    w.stake, w.pick, market.result, total, winning)
                settled.append((w.user_id, w.stake, payout, payout > 0))
        return aggregate_profit(settled)
