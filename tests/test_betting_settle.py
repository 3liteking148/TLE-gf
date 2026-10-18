"""Betting DB-layer tests: settlement, void/refund, open-market listing, mod\ntools, and leaderboards."""
import sqlite3  # noqa: F401

import pytest  # noqa: F401

from tle.util.db.user_db_conn import (  # noqa: F401
    UserDbConn, namedtuple_factory, bet_fixture_key,
)
from tests.betting_test_utils import (  # noqa: F401
    GUILD, CH, THREAD, USER_A, USER_B, db, _make_market,
)


class TestSettle:
    def test_winner_credited_loser_zero(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)  # bal 900
        db.bet_place(GUILD, mid, USER_B, 'away', 100, 1.0, 1000)  # bal 900
        rows = db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        by_user = {r[0]: r for r in rows}
        assert by_user[USER_A][4] == 200  # payout 100*2.0
        assert by_user[USER_B][4] == 0
        assert db.bet_get_balance(GUILD, USER_A) == 1100  # 900 + 200
        assert db.bet_get_balance(GUILD, USER_B) == 900   # unchanged
        assert db.bet_market_get(mid).status == 'settled'
        assert db.bet_market_get(mid).result == 'home'
        hist = db.bet_wallet_history(GUILD, USER_A)
        assert hist[0].action == 'payout' and hist[0].amount == 200
        assert hist[0].market_id == mid

    def test_settlement_embed_reports_net_win_loss(self, db, monkeypatch):
        from tle.cogs.betting import Betting
        from tle.util import codeforces_common as cf_common
        monkeypatch.setattr(cf_common, 'user_db', db)
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'away', 50, 1.0, 1000)
        rows = db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)

        embed = Betting(None)._settlement_embed(
            db.bet_market_get(mid), 'home', 2, 1, rows, 'auto')

        desc = embed.description
        assert f'<@{USER_A}> **+100**' in desc
        assert f'<@{USER_B}> **-50**' in desc
        assert 'Total staked: **150**' in desc
        assert 'paid out: **200**' in desc
        assert 'player net: **+50**' in desc

    def test_draw_outcome(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'draw', 100, 1.0, 1000)
        db.bet_settle(GUILD, mid, 'draw', 1, 1, 5.0)
        assert db.bet_get_balance(GUILD, USER_A) == 1200  # 900 + 300

    def test_multiple_picks_for_same_user_settle_independently(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_A, 'away', 100, 2.0, 1000)
        rows = db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        assert sorted((r[1], r[4]) for r in rows) == [('away', 0), ('home', 200)]
        assert db.bet_get_balance(GUILD, USER_A) == 1000

    def test_negative_pick_wins_when_event_does_not_happen(self, db):
        mid = _make_market(db, odds=(2.0, 4.0, 4.0))
        db.bet_place(GUILD, mid, USER_A, 'not_home', 100, 1.0, 1000)
        rows = db.bet_settle(GUILD, mid, 'draw', 1, 1, 5.0)
        by_user = {r[0]: r for r in rows}
        assert by_user[USER_A][4] == 200  # P(not home)=0.5 => odds 2.0
        assert db.bet_get_balance(GUILD, USER_A) == 1100

    def test_negative_pick_loses_when_event_happens(self, db):
        mid = _make_market(db, odds=(2.0, 4.0, 4.0))
        db.bet_place(GUILD, mid, USER_A, 'not_home', 100, 1.0, 1000)
        db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        assert db.bet_get_balance(GUILD, USER_A) == 900

    def test_profit_leaderboard(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'away', 100, 1.0, 1000)
        db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        prof = {r.user_id: (r.profit, r.bets, r.wins)
                for r in db.bet_profit_leaderboard(GUILD)}
        assert prof[USER_A] == (100, 1, 1)   # +200 payout - 100 stake
        assert prof[USER_B] == (-100, 1, 0)

    def test_profit_leaderboard_with_negative_pick(self, db):
        mid = _make_market(db, odds=(2.0, 4.0, 4.0))
        db.bet_place(GUILD, mid, USER_A, 'not_home', 100, 1.0, 1000)
        db.bet_settle(GUILD, mid, 'away', 0, 1, 5.0)
        prof = {r.user_id: (r.profit, r.bets, r.wins)
                for r in db.bet_profit_leaderboard(GUILD)}
        assert prof[USER_A] == (100, 1, 1)

    def test_double_settle_is_noop(self, db):
        """The status='open' guard must make a second settle pay nobody."""
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)  # bal 900
        first = db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        assert first is not None
        assert db.bet_get_balance(GUILD, USER_A) == 1100  # 900 + 200
        second = db.bet_settle(GUILD, mid, 'home', 2, 1, 6.0)
        assert second is None  # already settled — no work
        assert db.bet_get_balance(GUILD, USER_A) == 1100  # not paid twice

    def test_settle_after_void_is_noop(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)  # bal 900
        db.bet_void(GUILD, mid, 4.0)  # refunded → 1000
        assert db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0) is None
        assert db.bet_get_balance(GUILD, USER_A) == 1000  # not paid on top


class TestVoid:
    def test_refunds_and_cancels(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 300, 1.0, 1000)  # bal 700
        refunds = db.bet_void(GUILD, mid, 9.0)
        assert dict(refunds) == {USER_A: 300}
        assert db.bet_get_balance(GUILD, USER_A) == 1000  # fully restored
        assert db.bet_market_get(mid).status == 'cancelled'
        hist = db.bet_wallet_history(GUILD, USER_A)
        assert hist[0].action == 'void_refund'
        assert hist[0].amount == 300 and hist[0].balance_after == 1000

    def test_voided_excluded_from_profit(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 300, 1.0, 1000)
        db.bet_void(GUILD, mid, 9.0)
        assert db.bet_profit_leaderboard(GUILD) == []

    def test_void_after_settle_is_noop(self, db):
        """A void must not refund on top of a payout already credited."""
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)  # bal 900
        db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)  # paid 200 → 1100
        assert db.bet_void(GUILD, mid, 9.0) is None
        assert db.bet_get_balance(GUILD, USER_A) == 1100  # unchanged
        assert db.bet_market_get(mid).status == 'settled'  # not flipped


class TestMarketsOpen:
    def test_lists_only_open(self, db):
        open_mid = _make_market(db, commence=1000.0)
        settled = db.bet_market_create(
            GUILD, CH, 'evt2', 'soccer_epl', 'A', 'B', 2000.0,
            2.0, 3.0, 4.0, USER_A, 0.0)
        db.bet_settle(GUILD, settled, 'home', 1, 0, 3.0)
        rows = db.bet_markets_open(GUILD)
        assert [m.market_id for m in rows] == [open_mid]

    def test_guild_isolation(self, db):
        _make_market(db)
        assert db.bet_markets_open('999') == []


class TestModTools:
    def test_resettle_reverses_and_reapplies(self, db):
        mid = _make_market(db)  # odds home 2.0, away 4.0
        db.bet_place(GUILD, mid, USER_A, 'home', 100, 1.0, 1000)  # bal 900
        db.bet_place(GUILD, mid, USER_B, 'away', 100, 1.0, 1000)  # bal 900
        db.bet_settle(GUILD, mid, 'home', 2, 1, 5.0)
        assert db.bet_get_balance(GUILD, USER_A) == 1100  # +200
        assert db.bet_get_balance(GUILD, USER_B) == 900
        # Correct: it was actually an away win 1-2.
        rows = db.bet_resettle(GUILD, mid, 'away', 1, 2, 6.0)
        assert rows is not None
        assert db.bet_get_balance(GUILD, USER_A) == 900   # 1100 - 200 (reversed)
        assert db.bet_get_balance(GUILD, USER_B) == 1300  # 900 + 400 (now wins)
        m = db.bet_market_get(mid)
        assert m.result == 'away' and m.result_home == 1 and m.result_away == 2

    def test_resettle_negative_pick_delta(self, db):
        mid = _make_market(db, odds=(2.0, 4.0, 4.0))
        db.bet_place(GUILD, mid, USER_A, 'not_home', 100, 1.0, 1000)
        db.bet_settle(GUILD, mid, 'home', 1, 0, 5.0)
        assert db.bet_get_balance(GUILD, USER_A) == 900
        db.bet_resettle(GUILD, mid, 'draw', 1, 1, 6.0)
        assert db.bet_get_balance(GUILD, USER_A) == 1100

    def test_resettle_only_on_settled(self, db):
        mid = _make_market(db)
        assert db.bet_resettle(GUILD, mid, 'home', 1, 0, 5.0) is None  # still open

    def test_adjust_balance_grant_and_floor(self, db):
        assert db.bet_adjust_balance(
            GUILD, USER_A, 250, 1000, actor_id=USER_B,
            action='mod_grant') == 1250
        hist = db.bet_wallet_history(GUILD, USER_A)
        assert hist[0].action == 'mod_grant'
        assert hist[0].actor_id == USER_B and hist[0].amount == 250
        assert db.bet_adjust_balance(GUILD, USER_A, -5000, 1000) == 0  # floored
        assert db.bet_wallet_history(GUILD, USER_A)[0].amount == -1250

    def test_set_balance(self, db):
        assert db.bet_set_balance(
            GUILD, USER_A, 500, 1000, actor_id=USER_B,
            action='mod_setbalance') == 500
        hist = db.bet_wallet_history(GUILD, USER_A)
        assert hist[0].action == 'mod_setbalance'
        assert hist[0].actor_id == USER_B and hist[0].amount == -500
        assert db.bet_set_balance(GUILD, USER_A, -10, 1000) == 0

    def test_close_betting(self, db):
        mid = _make_market(db)
        assert db.bet_market_close_betting(mid) is True
        assert db.bet_market_get(mid).bets_closed == 1
        assert db.bet_market_close_betting(mid) is False  # already closed

    def test_set_odds_and_count_wagers(self, db):
        mid = _make_market(db)
        assert db.bet_market_count_wagers(mid) == 0
        assert db.bet_market_set_odds(mid, 1.5, 3.8, 6.0) is True
        m = db.bet_market_get(mid)
        assert (m.odds_home, m.odds_draw, m.odds_away) == (1.5, 3.8, 6.0)
        db.bet_place(GUILD, mid, USER_A, 'home', 10, 1.0, 1000)
        assert db.bet_market_count_wagers(mid) == 1

    def test_latest_settled_lookup(self, db):
        mid = _make_market(db)
        db.bet_market_set_thread(mid, THREAD)
        db.bet_settle(GUILD, mid, 'home', 1, 0, 5.0)
        assert db.bet_market_get_latest_settled_by_thread(
            GUILD, THREAD).market_id == mid
        assert db.bet_market_get_latest_settled_by_channel(
            GUILD, CH).market_id == mid
        assert db.bet_market_get_latest_settled_by_thread(GUILD, 'nope') is None


class TestBetsClosedExecution:
    """A market with bets_closed=1 rejects bets even before kickoff."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_closed_flag_blocks_betting(self, db, monkeypatch):
        from tle.util import codeforces_common as cf_common
        from tle import constants
        from tle.cogs.betting import Betting
        monkeypatch.setattr(cf_common, 'user_db', db)
        monkeypatch.setattr(constants, 'BET_START_BALANCE', 1000, raising=False)
        monkeypatch.setattr(constants, 'BET_MIN_STAKE', 1, raising=False)
        cog = Betting(bot=None)
        mid = _make_market(db, commence=1e12)  # far future → not kickoff-closed
        db.bet_market_close_betting(mid)
        market = db.bet_market_get(mid)
        user = type('U', (), {'id': USER_A})()
        status, _ = self._run(cog._execute_bet(GUILD, market, user, 'home', '50'))
        assert status == 'closed'


class TestBalanceLeaderboard:
    def test_orders_by_balance_desc(self, db):
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_A, 'home', 50, 0.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'away', 50, 0.0, 1000)
        db.conn.execute('UPDATE bet_wallet SET balance = 1500 WHERE user_id = ?',
                        (USER_B,))
        db.conn.commit()
        rows = db.bet_balance_leaderboard(GUILD)
        assert [r.user_id for r in rows] == [USER_B, USER_A]

    def test_excludes_wallets_without_bets(self, db):
        # USER_A has only ever claimed a wallet; USER_B has placed a bet.
        db.bet_ensure_wallet(GUILD, USER_A, 1000)
        mid = _make_market(db)
        db.bet_place(GUILD, mid, USER_B, 'home', 50, 0.0, 1000)
        rows = db.bet_balance_leaderboard(GUILD)
        assert [r.user_id for r in rows] == [USER_B]
