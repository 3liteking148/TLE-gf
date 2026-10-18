"""DB-layer tests for user-created parimutuel markets: the shared wager core,
settlement math, stats and migration 1.61.0."""

from tle.util.db.user_db_conn import UserDbConn
from tests.betting_test_utils import (  # noqa: F401
    GUILD, CH, THREAD, USER_A, USER_B, db, _make_custom_market, _make_market,
)

USER_C = '300'


class TestCustomMarketCrud:
    def test_create_and_choices(self, db):
        mid = _make_custom_market(db)
        market = db.bet_market_get(mid)
        assert market.title == 'Will it rain?'
        assert market.status == 'open' and market.closes_at == 9e12
        choices = db.bet_custom_market_choices(mid)
        assert [(c.choice_key, c.label) for c in choices] == \
            [('yes', 'Yes'), ('no', 'No')]
        assert db.bet_custom_market_choice(mid, 'yes').label == 'Yes'

    def test_open_markets_and_thread_lookup(self, db):
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        assert [m.market_id for m in
                db.bet_markets_open(GUILD, kind='custom')] == [mid]
        row = db.bet_market_get_active_by_thread(GUILD, THREAD, kind='custom')
        assert row is not None and row.market_id == mid
        assert db.bet_market_get_active(
            GUILD, CH, kind='custom').market_id == mid

    def test_custom_market_does_not_leak_into_soccer_queries(self, db):
        _make_custom_market(db)
        assert db.bet_markets_open(GUILD) == []
        assert db.bet_markets_pending_settlement(9e9) == []
        assert db.bet_market_get_active(GUILD, CH) is None

    def test_open_count_for_user(self, db):
        _make_custom_market(db)
        _make_custom_market(db, title='Second')
        assert db.bet_custom_open_count_for_user(GUILD, USER_A) == 2
        assert db.bet_custom_open_count_for_user(GUILD, USER_B) == 0


class TestMarketUniformity:
    """Both market kinds expose the same fields and DB helpers — no shape
    sniffing anywhere in the shared code."""

    def test_bettable(self, db):
        import time as _t
        mid = _make_custom_market(db, closes=_t.time() + 3600)
        assert db.bet_market_bettable(db.bet_market_get(mid)) is True
        mid2 = _make_custom_market(db, closes=_t.time() - 3600)
        assert db.bet_market_bettable(db.bet_market_get(mid2)) is False
        db.bet_market_close_betting(mid)
        assert db.bet_market_bettable(db.bet_market_get(mid)) is False

    def test_removability_is_per_market(self, db):
        soccer = db.bet_market_get(_make_market(db))
        custom = db.bet_market_get(_make_custom_market(db))
        assert soccer.removable == 1 and db.bet_market_allows_remove(soccer)
        assert custom.removable == 0 and not db.bet_market_allows_remove(custom)

    def test_backend_predicates(self, db):
        soccer = db.bet_market_get(_make_market(db))
        custom = db.bet_market_get(_make_custom_market(db, created_by=USER_A))
        assert db.bet_market_title(soccer) == 'Spain vs Cape Verde'
        assert db.bet_market_title(custom) == 'Will it rain?'
        assert db.bet_market_owned_by(soccer, USER_A) is False
        assert db.bet_market_owned_by(custom, USER_A) is True
        assert db.bet_market_owned_by(custom, USER_B) is False
        assert db.bet_market_has_fixed_odds(soccer) is True
        assert db.bet_market_has_fixed_odds(custom) is False
        assert db.bet_market_allows_draw(soccer) is True
        assert db.bet_market_allows_draw(custom) is False

    def test_soccer_not_pick_without_payable_odds_is_rejected(self, db):
        # A not-pick on an odds<=1 outcome can never pay, so it is not a valid
        # pick — the soccer backend folds that rule into bet_pick_allowed.
        market = db.bet_market_get(_make_market(db, odds=(1.0, 2.0, 3.0)))
        assert db.bet_pick_allowed(market, 'home') is True
        assert db.bet_pick_allowed(market, 'not_home') is False


class TestCustomWagers:
    def test_wagers_and_pool(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 200, 1.0, 1000)
        assert [(w.pick, w.stake) for w in db.bet_get_wagers_for_user(
            mid, USER_A)] == [('yes', 100)]
        assert [(w.pick, w.stake) for w in db.bet_get_wagers(mid)] == \
            [('yes', 100), ('no', 200)]
        pool = {p.pick: (p.cnt, p.total) for p in db.bet_market_pool(mid)}
        assert pool == {'yes': (1, 100), 'no': (1, 200)}
        assert db.bet_market_pool_totals(mid) == (300, {'yes': 100, 'no': 200})


class TestParimutuelSettle:
    def test_single_winner_takes_the_pool(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_C, 'no', 100, 1.0, 1000)
        rows, total, winning = db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        assert (total, winning) == (300, 100)
        payouts = {r[0]: r[3] for r in rows}
        assert payouts == {USER_A: 300, USER_B: 0, USER_C: 0}
        assert db.bet_get_balance(GUILD, USER_A) == 1200
        assert db.bet_get_balance(GUILD, USER_B) == 900
        assert db.bet_market_get(mid).status == 'settled'
        assert db.bet_market_get(mid).result == 'yes'

    def test_two_winners_split_by_stake(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'yes', 50, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_C, 'no', 150, 1.0, 1000)
        rows, total, winning = db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        payouts = {r[0]: r[3] for r in rows}
        assert payouts == {USER_A: 200, USER_B: 100, USER_C: 0}
        assert sum(payouts.values()) == total == 300

    def test_nobody_backed_winner_refunds_all(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'yes', 50, 1.0, 1000)
        rows, total, winning = db.bet_custom_settle(GUILD, mid, 'no', 5.0)
        assert winning == 0
        assert {r[0]: r[3] for r in rows} == {USER_A: 100, USER_B: 50}
        assert db.bet_get_balance(GUILD, USER_A) == 1000
        assert db.bet_get_balance(GUILD, USER_B) == 1000

    def test_double_settle_is_noop(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 100, 1.0, 1000)
        first = db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        assert first is not None
        assert db.bet_custom_settle(GUILD, mid, 'no', 6.0) is None
        assert db.bet_get_balance(GUILD, USER_A) == 1100

class TestCustomPickMath:
    def test_pick_odds_and_potential(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 300, 1.0, 1000)
        market = db.bet_market_get(mid)
        # total 400 / choice 100 → implied odds 4.0
        assert db.bet_pick_odds(market, 'yes') == 4.0
        assert abs(db.bet_pick_odds(market, 'no') - 4 / 3) < 1e-9
        assert db.bet_pick_odds(market, 'maybe') is None
        # at the current pool (total 400 / choice 100): stake × odds = 400.
        assert db.bet_pick_potential(market, 'yes', 100) == 400
        assert db.bet_pick_potential(market, 'maybe', 100) is None

    def test_pick_label_and_allowed(self, db):
        mid = _make_custom_market(db)
        market = db.bet_market_get(mid)
        assert db.bet_pick_label(market, 'yes') == 'Yes'
        assert db.bet_pick_label(market, 'nope') == 'nope'
        assert db.bet_pick_allowed(market, 'yes') is True
        assert db.bet_pick_allowed(market, 'maybe') is False

    def test_picks_ordered(self, db):
        mid = _make_custom_market(db, choices=(('a', 'A'), ('b', 'B'),
                                               ('c', 'C')))
        assert db.bet_market_picks(db.bet_market_get(mid)) == ['a', 'b', 'c']

    def test_soccer_pick_math(self, db):
        mid = _make_market(db, odds=(2.0, 4.0, 4.0))
        market = db.bet_market_get(mid)
        assert db.bet_pick_odds(market, 'home') == 2.0
        assert db.bet_pick_odds(market, 'not_home') == 2.0  # P(not)=0.75
        assert db.bet_pick_label(market, 'away') == 'Cape Verde'
        assert db.bet_pick_label(market, 'not_away') == 'Not Cape Verde'
        assert db.bet_pick_allowed(market, 'draw') is True
        assert db.bet_pick_allowed(market, 'not_draw') is True
        assert db.bet_pick_potential(market, 'home', 100) == 200


class TestCustomStats:
    def test_profit_rows_only_settled(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 100, 1.0, 1000)
        assert db.bet_custom_profit_rows(GUILD) == []
        db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        prof = {r.user_id: (r.profit, r.bets, r.wins)
                for r in db.bet_custom_profit_rows(GUILD)}
        assert prof[USER_A] == (100, 1, 1)
        assert prof[USER_B] == (-100, 1, 0)

    def test_active_wagers_for_user_shape(self, db):
        soccer = _make_market(db, commence=5_000.0)
        db.bet_market_set_thread(soccer, THREAD)
        db.bet_place(GUILD, soccer, USER_A, 'home', 10, 1.0, 1000)
        mid = _make_custom_market(db, closes=9_000.0)
        db.bet_market_set_thread(mid, THREAD)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        rows = db.bet_active_wagers_for_user(GUILD, USER_A)
        assert [r.kind for r in rows] == ['soccer', 'custom']
        assert [r.closes_at for r in rows] == [5_000.0, 9_000.0]
        assert rows[1].title == 'Will it rain?' and rows[1].thread_id == THREAD
        assert [db.bet_market_title(r) for r in rows] == \
            ['Spain vs Cape Verde', 'Will it rain?']

    def test_shared_leaderboards_include_custom(self, db):
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 100, 1.0, 1000)
        db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        assert db.bet_has_wagered(GUILD, USER_A) is True
        ids = [r.user_id for r in db.bet_balance_leaderboard(GUILD)]
        assert USER_A in ids and USER_B in ids
        prof = {r.user_id: r.profit for r in db.bet_profit_leaderboard(GUILD)}
        assert prof[USER_A] == 100 and prof[USER_B] == -100

    def test_custom_and_soccer_stats_merge(self, db):
        soccer = _make_market(db)
        db.bet_place(GUILD, soccer, USER_A, 'home', 100, 1.0, 1000)
        db.bet_settle(GUILD, soccer, 'home', 2, 1, 5.0)
        mid = _make_custom_market(db)
        db.bet_place(GUILD, mid, USER_A, 'yes', 100, 1.0, 1000)
        db.bet_place(GUILD, mid, USER_B, 'no', 100, 1.0, 1000)
        db.bet_custom_settle(GUILD, mid, 'yes', 5.0)
        prof = {r.user_id: (r.profit, r.bets, r.wins)
                for r in db.bet_profit_leaderboard(GUILD)}
        # soccer +100 (fixed odds), custom +100 (won a 100 pool against 100)
        assert prof[USER_A] == (200, 2, 2)


class TestMigration161:
    def test_fresh_db_has_unified_tables(self):
        conn = UserDbConn(':memory:')
        tables = {r[0] for r in conn.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {'bet_market', 'bet_market_soccer', 'bet_market_custom',
                'bet_custom_choice', 'bet_wager'} <= tables
        assert 'bet_custom_market' not in tables
        assert 'bet_custom_wager' not in tables
        cols = {r[1] for r in conn.conn.execute(
            'PRAGMA table_info(bet_market)')}
        assert 'removable' in cols
        # Parimutuel payout is a pure function of the frozen wagers, so it is
        # recomputed at read time rather than stored on the wager row.
        wager_cols = {r[1] for r in conn.conn.execute(
            'PRAGMA table_info(bet_wager)')}
        assert 'payout' not in wager_cols
