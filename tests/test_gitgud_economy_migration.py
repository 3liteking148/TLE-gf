"""Tests for upgrades 1.57.0 / 1.58.0 — the gitgud economy rebalance.

1.57.0 doubles the ranklist points of every completed AtCoder challenge
(``platform = 'ac'``, ``status = 0``) and rebuilds the affected users'
all-time aggregates from the completed-row sum. 1.58.0 tops up every betting
wallet by 9x its historical ``action='gitgud'`` ledger total — lifetime
gitgud coin earnings become an effective x10 — and writes one audit
transaction per wallet. The two never compound: they touch disjoint tables.
"""
import sqlite3

import pytest

from tle.util.db._user_db_upgrades_part5 import upgrade_1_57_0, upgrade_1_58_0
from tle.util.db.user_db_conn import namedtuple_factory


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = namedtuple_factory
    yield conn
    conn.close()


# ── seed helpers ─────────────────────────────────────────────────────────────

def _make_challenge_db(db):
    db.execute('''
        CREATE TABLE challenge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            issue_time REAL NOT NULL,
            finish_time REAL,
            problem_name TEXT NOT NULL,
            contest_id INTEGER NOT NULL,
            p_index INTEGER NOT NULL,
            rating_delta INTEGER NOT NULL,
            status INTEGER NOT NULL,
            platform TEXT NOT NULL DEFAULT 'cf',
            score INTEGER
        )
    ''')
    db.execute('''
        CREATE TABLE user_challenge (
            user_id TEXT,
            active_challenge_id INTEGER,
            issue_time REAL,
            score INTEGER NOT NULL,
            num_completed INTEGER NOT NULL,
            num_skipped INTEGER NOT NULL,
            PRIMARY KEY(user_id)
        )
    ''')


def _add_challenge(db, user_id='100', platform='cf', status=0, score=None,
                   delta=100, finished=True):
    return db.execute(
        'INSERT INTO challenge '
        '(user_id, issue_time, finish_time, problem_name, contest_id, '
        ' p_index, rating_delta, status, platform, score) '
        'VALUES (?, 1000.0, ?, ?, 1234, ?, ?, ?, ?, ?)',
        (user_id, 2000.0 if finished else None, 'Problem', 'A',
         delta, status, platform, score)).lastrowid


def _ensure_user_challenge(db, user_id, score, num_completed=0, num_skipped=0):
    db.execute(
        'INSERT INTO user_challenge '
        '(user_id, active_challenge_id, issue_time, score, num_completed, '
        ' num_skipped) VALUES (?, NULL, NULL, ?, ?, ?)',
        (user_id, score, num_completed, num_skipped))


def _scores(db):
    return [row[0] for row in db.execute(
        'SELECT score FROM challenge ORDER BY id')]


def _aggregate(db, user_id):
    row = db.execute(
        'SELECT score FROM user_challenge WHERE user_id = ?',
        (user_id,)).fetchone()
    return row.score if row else None


# ── 1.57.0: double AtCoder scores ────────────────────────────────────────────

class TestDoubleAtCoderScores:
    def test_doubles_only_completed_atcoder_rows(self, db):
        _make_challenge_db(db)
        # user 100: one completed AtCoder solve (8 pts) + one CF solve (5 pts)
        ac_done = _add_challenge(db, '100', platform='ac', score=8)
        cf_done = _add_challenge(db, '100', platform='cf', score=5)
        # user 200: skipped (status 2) and still-active (status 1) AtCoder rows
        ac_skip = _add_challenge(db, '200', platform='ac', status=2,
                                 score=None, finished=False)
        ac_active = _add_challenge(db, '200', platform='ac', status=1,
                                   score=None, finished=False)

        upgrade_1_57_0(db)

        scores = {r[0]: r[1] for r in db.execute(
            'SELECT id, score FROM challenge')}
        assert scores[ac_done] == 16          # doubled exactly once
        assert scores[cf_done] == 5           # CF untouched
        assert scores[ac_skip] is None        # skipped untouched
        assert scores[ac_active] is None      # active untouched

    def test_deltas_and_counters_never_move(self, db):
        _make_challenge_db(db)
        _add_challenge(db, '100', platform='ac', score=8, delta=500)
        _ensure_user_challenge(db, '100', score=13, num_completed=2,
                               num_skipped=1)

        before = db.execute(
            'SELECT rating_delta, num_completed, num_skipped FROM challenge c '
            'JOIN user_challenge u ON u.user_id = c.user_id').fetchone()

        upgrade_1_57_0(db)

        after = db.execute(
            'SELECT rating_delta, num_completed, num_skipped FROM challenge c '
            'JOIN user_challenge u ON u.user_id = c.user_id').fetchone()
        assert tuple(before) == tuple(after)

    def test_rebuilds_aggregates_for_affected_users_only(self, db):
        _make_challenge_db(db)
        # affected: completed ac + cf mix -> aggregate becomes exact SUM
        _add_challenge(db, '100', platform='ac', score=8)
        _add_challenge(db, '100', platform='cf', score=5)
        _ensure_user_challenge(db, '100', score=13)
        # atcoder rows but nothing completed -> not in the affected set
        _add_challenge(db, '200', platform='ac', status=2, score=None,
                       finished=False)
        _ensure_user_challenge(db, '200', score=7)
        # cf-only user -> untouched entirely
        _add_challenge(db, '300', platform='cf', score=23)
        _ensure_user_challenge(db, '300', score=23)

        upgrade_1_57_0(db)

        assert _aggregate(db, '100') == 21     # 16 + 5, rebuilt from SUM
        assert _aggregate(db, '200') == 7      # left alone
        assert _aggregate(db, '300') == 23     # left alone

    def test_noop_when_tables_absent(self, db):
        upgrade_1_57_0(db)  # must not raise

    def test_noop_on_pre_platform_schema(self, db):
        db.execute('''
            CREATE TABLE challenge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, issue_time REAL NOT NULL,
                finish_time REAL, problem_name TEXT NOT NULL,
                contest_id INTEGER NOT NULL, p_index INTEGER NOT NULL,
                rating_delta INTEGER NOT NULL, status INTEGER NOT NULL
            )
        ''')
        upgrade_1_57_0(db)  # missing platform/score columns -> skip


# ── 1.58.0: retroactive x10 gitgud coins ─────────────────────────────────────

def _make_betting_db(db):
    db.execute('''
        CREATE TABLE bet_wallet (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            balance INTEGER NOT NULL,
            last_daily TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    db.execute('''
        CREATE TABLE bet_wallet_txn (
            txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            actor_id TEXT,
            action TEXT NOT NULL,
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            market_id INTEGER,
            note TEXT,
            created_at REAL NOT NULL
        )
    ''')


def _wallet(db, guild_id, user_id, balance):
    db.execute('INSERT INTO bet_wallet (guild_id, user_id, balance) '
               'VALUES (?, ?, ?)', (guild_id, user_id, balance))


def _txn(db, guild_id, user_id, action, amount, balance_after):
    db.execute(
        'INSERT INTO bet_wallet_txn (guild_id, user_id, actor_id, action, '
        'amount, balance_after, created_at) VALUES (?, ?, NULL, ?, ?, ?, 1.0)',
        (guild_id, user_id, action, amount, balance_after))


def _balance(db, guild_id, user_id):
    return db.execute(
        'SELECT balance FROM bet_wallet WHERE guild_id = ? AND user_id = ?',
        (guild_id, user_id)).fetchone()[0]


def _retro_txns(db):
    return db.execute(
        "SELECT guild_id, user_id, amount, balance_after FROM bet_wallet_txn "
        "WHERE action = 'gitgud_retro_10x' "
        'ORDER BY guild_id, user_id').fetchall()


class TestRetroGitgudCoins:
    G1, G2, A, B = '1', '2', '100', '200'

    def _seed(self, db):
        # A in guild 1: 40 + 115 gitgud coins among other activity.
        _wallet(db, self.G1, self.A, 1055)
        _txn(db, self.G1, self.A, 'init', 1000, 1000)
        _txn(db, self.G1, self.A, 'gitgud', 40, 1040)
        _txn(db, self.G1, self.A, 'gitgud', 115, 1155)
        _txn(db, self.G1, self.A, 'wager_stake', -100, 1055)
        # B in guild 1: bettor, never earned gitgud coins -> untouched.
        _wallet(db, self.G1, self.B, 950)
        _txn(db, self.G1, self.B, 'init', 1000, 1000)
        _txn(db, self.G1, self.B, 'wager_stake', -50, 950)
        # A in guild 2: separate wallet, 80 gitgud coins there.
        _wallet(db, self.G2, self.A, 2000)
        _txn(db, self.G2, self.A, 'gitgud', 80, 2000)

    def test_tops_up_nine_times_gitgud_ledger_total(self, db):
        _make_betting_db(db)
        self._seed(db)

        upgrade_1_58_0(db)

        # 9 x (40 + 115) = 1395 extra on top of 1055.
        assert _balance(db, self.G1, self.A) == 1055 + 1395
        # No gitgud history -> untouched.
        assert _balance(db, self.G1, self.B) == 950
        # Same user, other guild: 9 x 80 = 720 on top of 2000.
        assert _balance(db, self.G2, self.A) == 2720

    def test_writes_one_audit_txn_per_wallet_with_true_balance_after(self, db):
        _make_betting_db(db)
        self._seed(db)

        upgrade_1_58_0(db)

        txns = _retro_txns(db)
        assert [(t[0], t[1], t[2]) for t in txns] == [
            (self.G1, self.A, 1395),
            (self.G2, self.A, 720),
        ]
        assert txns[0][3] == 2450   # balance_after reflects the top-up
        assert txns[1][3] == 2720

    def test_rerun_is_a_noop(self, db):
        _make_betting_db(db)
        self._seed(db)
        upgrade_1_58_0(db)
        balances = db.execute('SELECT * FROM bet_wallet').fetchall()
        count = len(_retro_txns(db))

        upgrade_1_58_0(db)  # guarded by the existing retro txn

        assert db.execute('SELECT * FROM bet_wallet').fetchall() == balances
        assert len(_retro_txns(db)) == count

    def test_noop_without_betting_tables(self, db):
        upgrade_1_58_0(db)  # must not raise

    def test_noop_without_gitgud_history(self, db):
        _make_betting_db(db)
        _wallet(db, self.G1, self.B, 500)
        _txn(db, self.G1, self.B, 'init', 1000, 1000)

        upgrade_1_58_0(db)

        assert _balance(db, self.G1, self.B) == 500
        assert _retro_txns(db) == []


# ── registration ─────────────────────────────────────────────────────────────

class TestRegistration:
    def test_new_versions_registered_last_in_order(self):
        from tle.util.db.user_db_upgrades import registry
        versions = [v for v, _, _ in registry.upgrades]
        assert versions[-2:] == ['1.57.0', '1.58.0']

    def test_latest_version_is_1_58_0(self):
        from tle.util.db.user_db_upgrades import registry
        assert registry.latest_version == '1.58.0'
