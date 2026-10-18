"""Soccer betting wallet DB methods — extracted from user_db_conn.py.

Owns the betting tables (``bet_wallet``, ``bet_market``, ``bet_wager``,
``bet_wallet_txn``) plus wallet bookkeeping and leaderboards. The market /
wager / settlement methods live in ``BettingMarketDbMixin``. The
``bet_fixture_key`` and ``_bet_*`` module helpers live in ``user_db_conn`` and
are imported lazily where needed to avoid an import cycle.
"""
import logging
import time
from collections import defaultdict

from tle.util.db.betting_schema import create_betting_tables
from tle.util.db.betting_wager_core import BetProfitRow

logger = logging.getLogger(__name__)

# guild_config key holding the per-guild starting-balance bonus that
# ``;bet grantall`` adds on top of constants.BET_START_BALANCE for every new
# wallet. Stored as a string int in the generic guild_config table so it shifts
# the seed balance of wallets opened *after* the grant too.
_BET_START_BONUS_CONFIG_KEY = 'bet_start_bonus'


class BettingWalletDbMixin:
    """Mixin providing betting wallet DB methods and table creation."""

    def _create_betting_tables(self):
        # Shared betting schema (base + per-kind extensions); see
        # ``betting_schema.create_betting_tables``. Kept in sync with migration
        # 1.61.0 — fresh DBs get the tables here, existing DBs via the migration.
        create_betting_tables(self.conn)

    # -- Wallet --

    def _bet_log_wallet_txn(self, guild_id, user_id, actor_id, action, amount,
                            balance_after, market_id=None, note=None,
                            created_at=None):
        self.conn.execute(
            'INSERT INTO bet_wallet_txn '
            '(guild_id, user_id, actor_id, action, amount, balance_after, '
            'market_id, note, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (str(guild_id), str(user_id), None if actor_id is None else str(actor_id),
             action, int(amount), int(balance_after), market_id, note,
             time.time() if created_at is None else created_at)
        )

    def _bet_ensure_wallet_txn(self, guild_id, user_id, start_balance,
                               created_at=None):
        """Create a betting wallet inside the caller's transaction."""
        guild_id, user_id = str(guild_id), str(user_id)
        cur = self.conn.execute(
            'INSERT OR IGNORE INTO bet_wallet (guild_id, user_id, balance) '
            'VALUES (?, ?, ?)',
            (guild_id, user_id, start_balance)
        )
        if cur.rowcount:
            self._bet_log_wallet_txn(
                guild_id, user_id, None, 'init', start_balance, start_balance,
                created_at=created_at)
        row = self.conn.execute(
            'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
            (guild_id, user_id)
        ).fetchone()
        return row.balance

    def bet_ensure_wallet(self, guild_id, user_id, start_balance):
        """Create a wallet seeded at start_balance if absent; return the
        current balance either way."""
        with self.conn:
            return self._bet_ensure_wallet_txn(
                guild_id, user_id, start_balance)

    def bet_get_balance(self, guild_id, user_id):
        """Return a user's balance, or None if they have no wallet yet."""
        row = self.conn.execute(
            'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
            (str(guild_id), str(user_id))
        ).fetchone()
        return row.balance if row else None

    def bet_wallet_get(self, guild_id, user_id):
        """Return a wallet row, or None if the user has no wallet yet."""
        return self.conn.execute(
            'SELECT guild_id, user_id, balance, last_daily '
            'FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
            (str(guild_id), str(user_id))
        ).fetchone()

    def bet_wallet_history(self, guild_id, user_id, limit=15):
        """Return recent wallet audit entries for a user, newest first."""
        limit = max(1, min(int(limit), 50))
        return self.conn.execute(
            'SELECT txn_id, guild_id, user_id, actor_id, action, amount, '
            'balance_after, market_id, note, created_at '
            'FROM bet_wallet_txn WHERE guild_id = ? AND user_id = ? '
            'ORDER BY created_at DESC, txn_id DESC LIMIT ?',
            (str(guild_id), str(user_id), limit)
        ).fetchall()

    def bet_claim_daily(self, guild_id, user_id, today, amount, start_balance):
        """Grant a flat `amount` once per UTC day.

        Returns (granted, new_balance, reason) where reason is 'ok' or
        'already' (already claimed today).
        """
        guild_id, user_id = str(guild_id), str(user_id)
        self.bet_ensure_wallet(guild_id, user_id, start_balance)
        row = self.conn.execute(
            'SELECT balance, last_daily FROM bet_wallet '
            'WHERE guild_id = ? AND user_id = ?',
            (guild_id, user_id)
        ).fetchone()
        if row.last_daily == today:
            return (False, row.balance, 'already')
        new_balance = row.balance + amount
        self.conn.execute(
            'UPDATE bet_wallet SET balance = ?, last_daily = ? '
            'WHERE guild_id = ? AND user_id = ?',
            (new_balance, today, guild_id, user_id)
        )
        self._bet_log_wallet_txn(
            guild_id, user_id, user_id, 'daily', amount, new_balance,
            note=today)
        self.conn.commit()
        return (True, new_balance, 'ok')

    def bet_transfer(self, guild_id, sender_id, receiver_id, amount,
                     start_balance, transferred_at=None, actor_id=None):
        """Move coins from one user wallet to another.

        Returns (ok, reason, sender_balance, receiver_balance). On
        insufficient funds, the receiver wallet is not created or modified.
        """
        guild_id = str(guild_id)
        sender_id = str(sender_id)
        receiver_id = str(receiver_id)
        actor_id = sender_id if actor_id is None else str(actor_id)
        amount = int(amount)
        if sender_id == receiver_id:
            bal = self.bet_get_balance(guild_id, sender_id)
            return (False, 'self', bal, bal)
        if amount <= 0:
            bal = self.bet_get_balance(guild_id, sender_id)
            return (False, 'invalid', bal, self.bet_get_balance(guild_id, receiver_id))

        when = time.time() if transferred_at is None else transferred_at
        with self.conn:
            sender_balance = self._bet_ensure_wallet_txn(
                guild_id, sender_id, start_balance, created_at=when)
            if amount > sender_balance:
                return (False, 'insufficient', sender_balance,
                        self.bet_get_balance(guild_id, receiver_id))
            receiver_balance = self._bet_ensure_wallet_txn(
                guild_id, receiver_id, start_balance, created_at=when)
            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount
            self.conn.execute(
                'UPDATE bet_wallet SET balance = ? '
                'WHERE guild_id = ? AND user_id = ?',
                (new_sender, guild_id, sender_id)
            )
            self.conn.execute(
                'UPDATE bet_wallet SET balance = ? '
                'WHERE guild_id = ? AND user_id = ?',
                (new_receiver, guild_id, receiver_id)
            )
            self._bet_log_wallet_txn(
                guild_id, sender_id, actor_id, 'transfer_out', -amount,
                new_sender, note=receiver_id, created_at=when)
            self._bet_log_wallet_txn(
                guild_id, receiver_id, actor_id, 'transfer_in', amount,
                new_receiver, note=sender_id, created_at=when)
            return (True, 'ok', new_sender, new_receiver)

    def bet_balance_leaderboard(self, guild_id):
        """Return [(user_id, balance)] for the guild, richest first.

        Only users who have actually placed at least one wager in the guild
        are included; wallets created via daily claims or balance checks
        without any bets are excluded.
        """
        return self.conn.execute(
            'SELECT wal.user_id, wal.balance FROM bet_wallet wal '
            'WHERE wal.guild_id = ? AND EXISTS ('
            '    SELECT 1 FROM bet_wager w '
            '    JOIN bet_market m ON m.market_id = w.market_id '
            '    WHERE m.guild_id = wal.guild_id AND w.user_id = wal.user_id'
            ') '
            'ORDER BY wal.balance DESC, wal.user_id ASC',
            (str(guild_id),)
        ).fetchall()

    def bet_profit_leaderboard(self, guild_id):
        """Return realized profit per user over settled markets, summed across
        every market kind (soccer fixed-odds + custom parimutuel), plus manual
        ``;bet profitadd`` ledger entries. Each row: user_id, profit, bets,
        wins."""
        totals = defaultdict(lambda: {'profit': 0, 'bets': 0, 'wins': 0})
        for rows in (self.bet_soccer_profit_rows(guild_id),
                     self.bet_custom_profit_rows(guild_id)):
            for row in rows:
                acc = totals[row.user_id]
                acc['profit'] += row.profit
                acc['bets'] += row.bets
                acc['wins'] += row.wins
        # Manual `;bet profitadd` ledger entries count as profit too — winning
        # bets the bot never recorded (e.g. placed right at close). They don't
        # add to the bets/wins counts.
        adjustments = self.conn.execute(
            'SELECT user_id, SUM(amount) AS total FROM bet_wallet_txn '
            "WHERE guild_id = ? AND action = 'profitadd' GROUP BY user_id",
            (str(guild_id),)
        ).fetchall()
        for row in adjustments:
            totals[row.user_id]['profit'] += int(row.total or 0)
        out = [BetProfitRow(user_id, values['profit'], values['bets'],
                            values['wins'])
               for user_id, values in totals.items()]
        out.sort(key=lambda r: (-r.profit, -r.wins, r.user_id))
        return out

    # -- Moderator tools --

    def bet_adjust_balance(self, guild_id, user_id, delta, start_balance,
                           actor_id=None, action=None, note=None):
        """Add delta (may be negative) to a wallet, creating it at
        start_balance first. Floors at 0. Returns the new balance."""
        guild_id, user_id = str(guild_id), str(user_id)
        self.bet_ensure_wallet(guild_id, user_id, start_balance)
        before = self.bet_get_balance(guild_id, user_id)
        new_balance = max(0, before + int(delta))
        self.conn.execute(
            'UPDATE bet_wallet SET balance = ? WHERE guild_id = ? AND user_id = ?',
            (new_balance, guild_id, user_id)
        )
        actual_delta = new_balance - before
        if actual_delta or action:
            self._bet_log_wallet_txn(
                guild_id, user_id, actor_id, action or 'adjust', actual_delta,
                new_balance, note=note)
        self.conn.commit()
        return new_balance

    def bet_set_balance(self, guild_id, user_id, value, start_balance,
                        actor_id=None, action='setbalance', note=None):
        """Set a wallet to an absolute value (floored at 0). Returns it."""
        guild_id, user_id = str(guild_id), str(user_id)
        value = max(0, int(value))
        self.bet_ensure_wallet(guild_id, user_id, start_balance)
        before = self.bet_get_balance(guild_id, user_id)
        self.conn.execute(
            'UPDATE bet_wallet SET balance = ? WHERE guild_id = ? AND user_id = ?',
            (value, guild_id, user_id)
        )
        self._bet_log_wallet_txn(
            guild_id, user_id, actor_id, action, value - before, value, note=note)
        self.conn.commit()
        return value

    def bet_get_start_bonus(self, guild_id):
        """Return the guild's starting-balance bonus (>= 0) added on top of the
        base start balance for every new wallet. 0 if ``;bet grantall`` was
        never used here."""
        row = self.conn.execute(
            'SELECT value FROM guild_config WHERE guild_id = ? AND key = ?',
            (str(guild_id), _BET_START_BONUS_CONFIG_KEY)
        ).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(row.value))
        except (TypeError, ValueError):
            return 0

    def bet_grant_all(self, guild_id, amount, actor_id=None, note=None):
        """Shift every existing wallet in the guild by ``amount`` and move the
        per-guild starting-balance bonus by the same amount, so first-time
        wallets opened later seed higher (or lower) too.

        ``amount`` may be negative to claw back a previous grant. Wallet
        balances and the bonus both floor at 0; each wallet's ledger records the
        actual delta applied. The whole sweep runs in one transaction. Returns
        (wallets_changed, new_bonus).
        """
        guild_id = str(guild_id)
        amount = int(amount)
        when = time.time()
        action = 'grantall' if amount >= 0 else 'grantall_revert'
        with self.conn:
            row = self.conn.execute(
                'SELECT value FROM guild_config WHERE guild_id = ? AND key = ?',
                (guild_id, _BET_START_BONUS_CONFIG_KEY)
            ).fetchone()
            try:
                current_bonus = max(0, int(row.value)) if row else 0
            except (TypeError, ValueError):
                current_bonus = 0
            new_bonus = max(0, current_bonus + amount)
            self.conn.execute(
                'INSERT OR REPLACE INTO guild_config (guild_id, key, value) '
                'VALUES (?, ?, ?)',
                (guild_id, _BET_START_BONUS_CONFIG_KEY, str(new_bonus))
            )
            wallets = self.conn.execute(
                'SELECT user_id, balance FROM bet_wallet WHERE guild_id = ?',
                (guild_id,)
            ).fetchall()
            changed = 0
            for wallet in wallets:
                new_balance = max(0, wallet.balance + amount)
                delta = new_balance - wallet.balance
                if delta == 0:
                    continue
                self.conn.execute(
                    'UPDATE bet_wallet SET balance = ? '
                    'WHERE guild_id = ? AND user_id = ?',
                    (new_balance, guild_id, wallet.user_id)
                )
                self._bet_log_wallet_txn(
                    guild_id, wallet.user_id, actor_id, action, delta,
                    new_balance, note=note, created_at=when)
                changed += 1
            return (changed, new_bonus)
