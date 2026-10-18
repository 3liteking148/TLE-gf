"""Market-agnostic wager mechanics over the shared betting tables.

The place/remove/refund flows are identical for every market kind; both operate
on the shared ``bet_market`` / ``bet_wager`` tables (``market_id`` is globally
unique). These are the public wager API used by both kinds — there is no
per-kind wrapper layer. ``_credit_wallet`` is the single wallet-credit +
audit-log step shared by every settlement payout and cancellation refund.
"""
from collections import defaultdict, namedtuple

BetProfitRow = namedtuple('BetProfitRow', 'user_id profit bets wins')


def aggregate_profit(rows):
    """Aggregate ``(user_id, stake, payout, won)`` tuples into per-user
    ``BetProfitRow`` totals. Shared by both market kinds' profit rows."""
    totals = defaultdict(lambda: {'profit': 0, 'bets': 0, 'wins': 0})
    for user_id, stake, payout, won in rows:
        acc = totals[user_id]
        acc['profit'] += payout - stake
        acc['bets'] += 1
        acc['wins'] += 1 if won else 0
    return [BetProfitRow(user_id, values['profit'], values['bets'],
                         values['wins'])
            for user_id, values in totals.items()]


class BettingWagerCoreMixin:
    """Mixin providing the shared wager place/remove/refund API."""

    def _credit_wallet(self, guild_id, user_id, amount, action, *,
                       market_id=None, note=None, created_at=None):
        """Credit a wallet and write the audit transaction, returning the new
        balance. Shared by settlement payouts and cancellation refunds."""
        guild_id, user_id = str(guild_id), str(user_id)
        self.conn.execute(
            'UPDATE bet_wallet SET balance = balance + ? '
            'WHERE guild_id = ? AND user_id = ?',
            (amount, guild_id, user_id)
        )
        balance_after = self.conn.execute(
            'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
            (guild_id, user_id)
        ).fetchone().balance
        self._bet_log_wallet_txn(
            guild_id, user_id, None, action, amount, balance_after,
            market_id=market_id, note=note, created_at=created_at)
        return balance_after

    def _credit_settlement(self, guild_id, market_id, wagers, settled_at,
                           payout_for, action, note):
        """Credit each wager's settlement payout in one shared loop.

        ``payout_for(wager) -> int`` returns the amount to credit (0 = skip).
        Returns ``[(wager, payout)]``. Shared by soccer (fixed-odds) and custom
        (parimutuel/refund) settlement so the credit + audit step lives once.
        Must run inside the caller's ``with self.conn`` transaction.
        """
        outcome = []
        for wager in wagers:
            payout = int(payout_for(wager))
            if payout:
                self._credit_wallet(guild_id, wager.user_id, payout, action,
                                    market_id=market_id, note=note,
                                    created_at=settled_at)
            outcome.append((wager, payout))
        return outcome

    def _wager_market_is_bettable(self, guild_id, market_id):
        row = self.conn.execute(
            'SELECT status, bets_closed FROM bet_market '
            'WHERE guild_id = ? AND market_id = ?',
            (str(guild_id), market_id)
        ).fetchone()
        return row is not None and row.status == 'open' and not row.bets_closed

    def bet_place(self, guild_id, market_id, user_id, pick, stake,
                     placed_at, start_balance, actor_id=None):
        """Place or replace a user's wager on one pick, escrowing the stake
        from their wallet. Re-betting the same pick refunds the previous stake
        first; other picks by the same user stay in place.

        ``actor_id`` records who placed the bet in the wallet audit log; it
        defaults to the bettor (a normal self-bet) but an admin placing a bet
        on someone's behalf passes their own id.

        Returns (ok, reason, new_balance) where reason is 'ok', 'unchanged',
        'invalid', 'closed', or 'insufficient'. Atomic: wallet debit and wager
        write commit together.
        """
        guild_id, user_id = str(guild_id), str(user_id)
        actor = user_id if actor_id is None else str(actor_id)
        pick = str(pick)
        stake = int(stake)
        if stake <= 0:
            return (False, 'invalid', self.bet_get_balance(guild_id, user_id))
        with self.conn:
            balance_row = self.conn.execute(
                'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ).fetchone()
            if not self._wager_market_is_bettable(guild_id, market_id):
                balance = balance_row.balance if balance_row else None
                return (False, 'closed', balance)
            cur = self.conn.execute(
                'INSERT OR IGNORE INTO bet_wallet (guild_id, user_id, balance) '
                'VALUES (?, ?, ?)',
                (guild_id, user_id, start_balance)
            )
            if cur.rowcount:
                self._bet_log_wallet_txn(
                    guild_id, user_id, None, 'init', start_balance, start_balance,
                    created_at=placed_at)
            balance = self.conn.execute(
                'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ).fetchone().balance
            prev = self.conn.execute(
                'SELECT stake FROM bet_wager WHERE market_id = ? '
                'AND user_id = ? AND pick = ?',
                (market_id, user_id, pick)
            ).fetchone()
            if prev and prev.stake == stake:
                return (True, 'unchanged', balance)
            available = balance + (prev.stake if prev else 0)
            if stake > available:
                return (False, 'insufficient', balance)
            new_balance = available - stake
            if prev:
                self._bet_log_wallet_txn(
                    guild_id, user_id, actor, 'wager_refund', prev.stake,
                    available, market_id=market_id, note=pick,
                    created_at=placed_at)
            self.conn.execute(
                'INSERT OR REPLACE INTO bet_wager '
                '(market_id, user_id, pick, stake, placed_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (market_id, user_id, pick, stake, placed_at)
            )
            self.conn.execute(
                'UPDATE bet_wallet SET balance = ? WHERE guild_id = ? AND user_id = ?',
                (new_balance, guild_id, user_id)
            )
            self._bet_log_wallet_txn(
                guild_id, user_id, actor, 'wager_stake', -stake, new_balance,
                market_id=market_id, note=pick, created_at=placed_at)
            return (True, 'ok', new_balance)

    def bet_remove_wager(self, guild_id, market_id, user_id, pick, removed_at,
                      actor_id=None):
        """Remove one wager pick for a user and refund its stake.

        ``actor_id`` records who removed the bet in the wallet audit log,
        defaulting to the bettor; an admin removing on someone's behalf passes
        their own id.

        Returns (ok, reason, new_balance, refunded) where reason is 'removed',
        'missing', or 'closed'. Other picks by the same user stay in place.
        """
        guild_id, user_id, pick = str(guild_id), str(user_id), str(pick)
        actor = user_id if actor_id is None else str(actor_id)
        with self.conn:
            balance_row = self.conn.execute(
                'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ).fetchone()
            balance = balance_row.balance if balance_row else None
            if not self._wager_market_is_bettable(guild_id, market_id):
                return (False, 'closed', balance, 0)
            prev = self.conn.execute(
                'SELECT stake FROM bet_wager WHERE market_id = ? '
                'AND user_id = ? AND pick = ?',
                (market_id, user_id, pick)
            ).fetchone()
            if prev is None:
                return (False, 'missing', balance, 0)
            if balance_row is None:
                self.conn.execute(
                    'INSERT INTO bet_wallet (guild_id, user_id, balance) '
                    'VALUES (?, ?, 0)',
                    (guild_id, user_id)
                )
                balance = 0
            new_balance = (balance or 0) + prev.stake
            self.conn.execute(
                'DELETE FROM bet_wager WHERE market_id = ? AND user_id = ? '
                'AND pick = ?',
                (market_id, user_id, pick)
            )
            self.conn.execute(
                'UPDATE bet_wallet SET balance = ? WHERE guild_id = ? AND user_id = ?',
                (new_balance, guild_id, user_id)
            )
            self._bet_log_wallet_txn(
                guild_id, user_id, actor, 'wager_refund', prev.stake,
                new_balance, market_id=market_id, note=pick,
                created_at=removed_at)
            return (True, 'removed', new_balance, prev.stake)

    def bet_remove_wagers_for_user(self, guild_id, market_id, user_id, removed_at):
        """Remove all wagers by one user on one market and refund their stakes.

        Returns (ok, reason, new_balance, refunded, count) where reason is
        'removed', 'missing', or 'closed'. Wagers by the same user on other
        markets are untouched.
        """
        guild_id, user_id = str(guild_id), str(user_id)
        with self.conn:
            balance_row = self.conn.execute(
                'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ).fetchone()
            balance = balance_row.balance if balance_row else None
            if not self._wager_market_is_bettable(guild_id, market_id):
                return (False, 'closed', balance, 0, 0)
            wagers = self.conn.execute(
                'SELECT pick, stake FROM bet_wager '
                'WHERE market_id = ? AND user_id = ? '
                'ORDER BY placed_at ASC, pick ASC',
                (market_id, user_id)
            ).fetchall()
            if not wagers:
                return (False, 'missing', balance, 0, 0)
            if balance_row is None:
                self.conn.execute(
                    'INSERT INTO bet_wallet (guild_id, user_id, balance) '
                    'VALUES (?, ?, 0)',
                    (guild_id, user_id)
                )
                balance = 0
            total_refunded = sum(w.stake for w in wagers)
            new_balance = balance + total_refunded
            self.conn.execute(
                'DELETE FROM bet_wager WHERE market_id = ? AND user_id = ?',
                (market_id, user_id)
            )
            self.conn.execute(
                'UPDATE bet_wallet SET balance = ? WHERE guild_id = ? AND user_id = ?',
                (new_balance, guild_id, user_id)
            )
            running_balance = balance
            for wager in wagers:
                running_balance += wager.stake
                self._bet_log_wallet_txn(
                    guild_id, user_id, user_id, 'wager_refund', wager.stake,
                    running_balance, market_id=market_id, note=wager.pick,
                    created_at=removed_at)
            return (True, 'removed', new_balance, total_refunded, len(wagers))

    def bet_void(self, guild_id, market_id, voided_at):
        """Cancel an open market: refund every stake to its wallet and mark the
        market cancelled. Returns [(user_id, stake)] refunded, or None if the
        market was not open.

        The status flip is scoped to ``status='open'`` and runs first, so a
        void can't double-refund or undo a settlement. Atomic.
        """
        guild_id = str(guild_id)
        with self.conn:
            changed = self.conn.execute(
                "UPDATE bet_market SET status = 'cancelled', settled_at = ? "
                "WHERE market_id = ? AND status = 'open'",
                (voided_at, market_id)
            ).rowcount
            if changed == 0:
                return None
            refunds = []
            wagers = self.conn.execute(
                'SELECT user_id, stake FROM bet_wager WHERE market_id = ?',
                (market_id,)
            ).fetchall()
            for w in wagers:
                self._credit_wallet(guild_id, w.user_id, w.stake,
                                    'void_refund', market_id=market_id,
                                    created_at=voided_at)
                refunds.append((w.user_id, w.stake))
        return refunds
