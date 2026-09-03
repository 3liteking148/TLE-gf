"""ThemeCP progression + generic batch-claim integration tests.

Covers issuing a ``;gitgudprogression`` batch, claiming it (strict, partial,
bonus window), skipping it, monthly doubling, and coin rewards — all against
an in-memory DB with mocked CF edges. Single and batch claims share the
original ``Challenge completed ...`` message; only a second ``Bonus applied``
line distinguishes a boosted batch.
"""
import datetime
import time

import pytest

from tests.betting_test_utils import GUILD, USER_A, db  # noqa: F401
from tests.gitgud_test_utils import (  # noqa: F401
    _backdate,
    _ctx,
    _issue_level1,
    _make_bettor,
    _more_points_on,
    _patch_cf_handle,
    _run,
    _solo_prob,
    _solve_names,
    _solve_offsets,
    cog,
)
from tle import constants
from tle.cogs._codeforces_helpers import CodeforcesCogError, _GITGUD_CLAIM_MARGIN, _GITGUD_COIN_MULTIPLIER


class TestProgressionIssue:
    def test_issues_batch(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        assert len(actives) == 4
        assert all(a.platform == 'cf' for a in actives)
        assert all(a.batch_id.startswith('prog-1-snowflake-') for a in actives)
        assert len({a.batch_id for a in actives}) == 1
        # level 1 is 800; user rating 800 -> delta_base 1100 -> delta -300 -> score 2
        assert [a.problem_key for a in actives] == [f'CF800_{i}' for i in range(4)]
        assert [a.contest_id for a in actives] == [1000 + i for i in range(4)]
        assert [a.rating_delta for a in actives] == [-300, -300, -300, -300]
        assert [a.score for a in actives] == [2, 2, 2, 2]
        assert db.get_current_batch_id(USER_A).startswith('prog-1-')
        assert db.count_active_challenges(USER_A) == 4

    def test_issue_embeds_bonus_proof(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        ctx = _ctx()
        _run(cog._gitgudprogression_impl(ctx, ('1',)))
        assert ctx.sent and ctx.sent[0][1] is not None
        embed = ctx.sent[0][1]
        assert 'ThemeCP level 1' in embed.title
        fields = {f['name']: f['value'] for f in embed.fields}
        assert fields['Alltime points'] == '8'
        assert fields['Monthly points'] == '8'
        # bonus proof lives in the per-slot preview + footer (bonus total 11)
        assert '(3 with bonus)' in embed.description
        assert '(4 with bonus)' in embed.description
        assert 'bonus total 11' in embed.footer['text']
        assert embed.footer['text'].startswith('Bonus applies')

    def test_second_issue_while_active_rejected(self, db, cog, monkeypatch):
        _issue_level1(db, cog, monkeypatch)
        with pytest.raises(CodeforcesCogError, match='4 active challenge'):
            _run(cog._gitgudprogression_impl(_ctx(), ('1',)))
        assert db.count_active_challenges(USER_A) == 4

    def test_single_issue_blocks_progression(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        prob = _solo_prob()
        _run(cog._gitgud(_ctx(), 'handleA', prob, -300, 2, False,
                         cog._backend_for_platform('cf'),
                         datetime.datetime.now()))
        with pytest.raises(CodeforcesCogError, match='active challenge'):
            _run(cog._gitgudprogression_impl(_ctx(), ('1',)))
        assert db.count_active_challenges(USER_A) == 1

    def test_invalid_level_rejected(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        with pytest.raises(CodeforcesCogError, match='Invalid ThemeCP level'):
            _run(cog._gitgudprogression_impl(_ctx(), ('9999',)))
        assert db.count_active_challenges(USER_A) == 0

    def test_rating_arg_rejected(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        with pytest.raises(CodeforcesCogError, match='fixes ratings'):
            _run(cog._gitgudprogression_impl(_ctx(), ('1', '900')))
        assert db.count_active_challenges(USER_A) == 0


class TestProgressionClaim:
    def test_claims_with_bonus_within_window(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        # freshly issued batch claims immediately, well within the 120 min window
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        assert db.list_active_challenges(USER_A) == []
        # base 2 each; THEME_MULTS (1,1,1.5,2) => 2,2,3,4 = 11 total in user_challenge,
        # and boosted scores are persisted per-row so monthly range queries match.
        assert db.get_gudgitter_score(USER_A) == 11
        assert 'Challenge completed' in ctx.sent[0][0]
        assert '11 alltime' in ctx.sent[0][0]
        assert any('Bonus applied' in (m[0] or '') for m in ctx.sent[1:])
        rows = db.conn.execute('SELECT status, finish_time, score FROM challenge WHERE user_id=?', (USER_A,)).fetchall()
        assert all(r[0] == 0 for r in rows)  # GOTGUD
        assert sorted([r[2] for r in rows]) == [2, 2, 3, 4]

    def test_claims_without_bonus_after_window(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        # backdate issue past the 120 min window so the bonus expires
        _backdate(db, 8000)
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        assert db.list_active_challenges(USER_A) == []
        assert db.get_gudgitter_score(USER_A) == 8  # 2*4 no bonus
        assert 'Challenge completed' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)
        rows = db.conn.execute('SELECT score FROM challenge WHERE user_id=?', (USER_A,)).fetchall()
        assert sorted([r[0] for r in rows]) == [2, 2, 2, 2]

    def test_strict_names_missing_and_keeps_batch(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        solved = {a.problem_key for a in actives[:3]}
        missing = [a.problem_key for a in actives if a.problem_key not in solved][0]
        _solve_names(monkeypatch, solved)
        with pytest.raises(CodeforcesCogError, match=f'missing 1.*{missing}'):
            _run(cog._gotgud_impl(_ctx()))
        assert db.count_active_challenges(USER_A) == 4
        assert db.get_gudgitter_score(USER_A) == 0

    def test_finish_times_use_claim_time(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_offsets(db, monkeypatch, {n: 60 * (i + 1) for i, n in enumerate(names)})
        ctx = _ctx()
        before = time.time()
        _run(cog._gotgud_impl(ctx))
        after = time.time()
        assert db.list_active_challenges(USER_A) == []
        rows = db.conn.execute(
            'SELECT problem_name, finish_time FROM challenge WHERE user_id=?', (USER_A,)).fetchall()
        # storage uses claim receipt, not CF solve epochs; bonus still used
        # the solve offsets (all within window, so boosted total).
        assert db.get_gudgitter_score(USER_A) == 11
        for r in rows:
            assert before - 10 <= r[1] <= after + 10
        # duration uses wall-clock claim time, not the CF solve times
        # (offsets are future-dated, so a solve-based duration would say minutes).
        assert 'second' in ctx.sent[0][0]

    def test_ac_within_margin_counts(self, db, cog, monkeypatch):
        # ACs up to _GITGUD_CLAIM_MARGIN before issue are leniently accepted
        # (clock skew / solve-then-issue races).
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_offsets(db, monkeypatch, {n: -10 for n in names})
        before = time.time()
        _run(cog._gotgud_impl(_ctx()))
        after = time.time()
        assert db.list_active_challenges(USER_A) == []
        rows = db.conn.execute(
            'SELECT problem_name, finish_time FROM challenge WHERE user_id=?', (USER_A,)).fetchall()
        for r in rows:
            assert before - 10 <= r[1] <= after + 10

    def test_ac_before_margin_does_not_count(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_offsets(db, monkeypatch, {n: -_GITGUD_CLAIM_MARGIN - 100 for n in names})
        with pytest.raises(CodeforcesCogError, match='missing 4'):
            _run(cog._gotgud_impl(_ctx()))
        assert db.count_active_challenges(USER_A) == 4
        assert db.get_gudgitter_score(USER_A) == 0

    def test_split_times_bonus_early_only(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        # A, B, C solved fast, D solved past the 120 min window: D keeps
        # base points while the early slots keep their mults.
        _solve_offsets(
            db, monkeypatch, {names[0]: 60, names[1]: 60, names[2]: 60, names[3]: 8000})
        ctx = _ctx()
        before = time.time()
        _run(cog._gotgud_impl(ctx))
        after = time.time()
        assert db.get_gudgitter_score(USER_A) == 9  # 2, 2, 3, 2
        rows = db.conn.execute(
            'SELECT problem_name, finish_time, score FROM challenge WHERE user_id=?',
            (USER_A,)).fetchall()
        by_name = {r[0]: (r[1], r[2]) for r in rows}
        assert [by_name[n][1] for n in names] == [2, 2, 3, 2]
        for n in names:
            assert before - 10 <= by_name[n][0] <= after + 10
        assert any('Bonus applied' in (m[0] or '') for m in ctx.sent[1:])

    def test_api_error_fails_gotgud(self, db, cog, monkeypatch):
        from tle.util import codeforces_api as cf_api

        _issue_level1(db, cog, monkeypatch)

        async def boom(*, handle):
            raise RuntimeError('cf down')

        monkeypatch.setattr(cf_api.user, 'status', boom)
        with pytest.raises(CodeforcesCogError, match='Could not fetch'):
            _run(cog._gotgud_impl(_ctx()))
        assert db.count_active_challenges(USER_A) == 4
        assert db.get_gudgitter_score(USER_A) == 0

    def test_gotgud_with_no_active_rejected(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        with pytest.raises(CodeforcesCogError, match='active challenge'):
            _run(cog._gotgud_impl(_ctx()))


class TestPartialClaims:
    """``+partial`` works on any multi-challenge batch: at least one solved
    required; solved slots keep positional mults (D stays ×2); the bonus line
    appears only when a stored slot was actually boosted; the unsolved rest is
    NOGUD-skipped atomically. The flag is inert on singletons."""

    def test_zero_solved_raises_and_keeps_batch(self, db, cog, monkeypatch):
        _issue_level1(db, cog, monkeypatch)
        _solve_names(monkeypatch, set())
        with pytest.raises(CodeforcesCogError, match="haven't completed"):
            _run(cog._gotgud_impl(_ctx(), '+partial'))
        assert db.count_active_challenges(USER_A) == 4
        assert db.get_gudgitter_score(USER_A) == 0

    def test_three_of_four_keeps_positional_bonus(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_names(monkeypatch, names[:3])  # A, B, C solved; D skipped
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        assert db.list_active_challenges(USER_A) == []
        # positional mults: A×1, B×1, C×1.5 → 2,2,3 = 7; D skipped.
        # boosted scores persist on completed rows; the skipped row keeps base.
        assert db.get_gudgitter_score(USER_A) == 7
        rows = db.conn.execute(
            'SELECT problem_name, status, score FROM challenge WHERE user_id=?',
            (USER_A,)).fetchall()
        by_name = {r[0]: (r[1], r[2]) for r in rows}
        assert [by_name[n][1] for n in names] == [2, 2, 3, 2]
        assert db.get_nogud_problem_keys(USER_A) == {names[3]}
        assert 'Challenge completed' in ctx.sent[0][0]
        assert '7 alltime' in ctx.sent[0][0]
        assert any('Bonus applied' in (m[0] or '') for m in ctx.sent[1:])

    def test_d_only_scores_base_no_bonus(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_names(monkeypatch, {names[3]})
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        # lone D is outside any streak from A: base 2, no bonus line.
        assert db.get_gudgitter_score(USER_A) == 2
        assert 'Challenge completed' in ctx.sent[0][0]
        assert '2 alltime' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)

    def test_gapped_set_scores_base_no_bonus(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        # B+C+D solved but A missing: no streak from A, all base.
        _solve_names(monkeypatch, set(names[1:]))
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        assert db.get_gudgitter_score(USER_A) == 6
        assert 'Challenge completed' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)

    def test_prefix_pair_scores_base_no_bonus(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        # A+B streak covers only x1.0 slots: base 4, no bonus line.
        _solve_names(monkeypatch, set(names[:2]))
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        assert db.get_gudgitter_score(USER_A) == 4
        assert 'Challenge completed' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)

    def test_flat_slots_show_no_bonus_line(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_names(monkeypatch, set(names[:2]))  # only x1.0 slots
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        assert db.get_gudgitter_score(USER_A) == 4
        assert 'Challenge completed' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)

    def test_all_solved_via_flag_matches_strict(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        assert db.get_gudgitter_score(USER_A) == 11
        assert 'Challenge completed' in ctx.sent[0][0]

    def test_partial_coins_use_stored_total(self, db, cog, monkeypatch):
        _make_bettor(db)
        actives = _issue_level1(db, cog, monkeypatch)
        names = [a.problem_key for a in actives]
        _solve_names(monkeypatch, set(names[:3]))
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx, '+partial'))
        exp = _GITGUD_COIN_MULTIPLIER * 7
        assert f'{exp} \U0001fa99' in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == 900 + exp

    def test_partial_silent_on_singleton(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        prob = _solo_prob()
        _run(cog._gitgud(_ctx(), 'handleA', prob, -300, 2, False,
                         cog._backend_for_platform('cf'), datetime.datetime.now()))
        # unsolved singleton + flag behaves exactly like strict: same error
        with pytest.raises(CodeforcesCogError, match="haven't completed"):
            _run(cog._gotgud_impl(_ctx(), '+partial'))
        assert db.count_active_challenges(USER_A) == 1


class TestProgressionNogud:
    def test_skips_batch_after_guard(self, db, cog, monkeypatch):
        _issue_level1(db, cog, monkeypatch)
        # immediate skip blocked by 2h guard
        ctx = _ctx()
        _run(cog._nogud_impl(ctx))
        assert len(db.list_active_challenges(USER_A)) == 4
        assert ctx.sent and 'Think more' in ctx.sent[0][0]
        # advance issue_time 3h ago
        _backdate(db, 3 * 3600)
        _run(cog._nogud_impl(_ctx()))
        assert db.list_active_challenges(USER_A) == []
        assert db.get_nogud_problem_keys(USER_A) == {f'CF800_{i}' for i in range(4)}
        assert db.get_gudgitter_score(USER_A) == 0

    def test_nogud_with_no_active_rejected(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        with pytest.raises(CodeforcesCogError, match='active challenge'):
            _run(cog._nogud_impl(_ctx()))

    def test_atcoder_rejects_batch_claim(self, db, cog, monkeypatch):
        from tle.cogs._atcoder_gitgud import _AcBackend
        actives = _issue_level1(db, cog, monkeypatch)
        assert len(actives) == 4
        with pytest.raises(CodeforcesCogError, match='single claims'):
            _run(_AcBackend().verify_claims(_ctx(), 'handleA', actives))
        assert db.count_active_challenges(USER_A) == 4


class TestMonthlyTotal:
    def test_mid_month_is_flat(self, cog):
        assert cog._monthly_total(8, datetime.datetime(2025, 3, 5)) == 8

    def test_last_week_doubles(self, cog):
        assert cog._monthly_total(8, datetime.datetime(2025, 3, 27)) == 16

    def test_progression_embed_monthly_fields_double(self, db, cog, monkeypatch):
        _more_points_on(cog, monkeypatch)
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        ctx = _ctx()
        _run(cog._gitgudprogression_impl(ctx, ('1',)))
        fields = {f['name']: f['value'] for f in ctx.sent[0][1].fields}
        assert fields['Alltime points'] == '8'
        assert fields['Monthly points'] == str(2 * 8)

    def test_claim_message_shows_doubled_monthly(self, db, cog, monkeypatch):
        _more_points_on(cog, monkeypatch)
        actives = _issue_level1(db, cog, monkeypatch)
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        assert 'Challenge completed' in ctx.sent[0][0]
        assert '11 alltime' in ctx.sent[0][0]
        assert '22 monthly' in ctx.sent[0][0]


class TestProgressionCoins:
    """Coins key off the stored alltime ``total`` (bonus-included) at
    ``_GITGUD_COIN_MULTIPLIER`` per point and never see monthly doubling."""

    def _issue_and_solve(self, db, cog, monkeypatch):
        actives = _issue_level1(db, cog, monkeypatch)
        _solve_names(monkeypatch, [a.problem_key for a in actives])

    def test_bettor_earns_multiplier_times_bonus_total(self, db, cog, monkeypatch):
        _make_bettor(db)
        self._issue_and_solve(db, cog, monkeypatch)
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        exp = _GITGUD_COIN_MULTIPLIER * 11
        assert f'{exp} \U0001fa99' in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == 900 + exp

    def test_expired_window_pays_base_total(self, db, cog, monkeypatch):
        _make_bettor(db)
        self._issue_and_solve(db, cog, monkeypatch)
        _backdate(db, 8000)
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        exp = _GITGUD_COIN_MULTIPLIER * 8
        assert f'{exp} \U0001fa99' in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == 900 + exp

    def test_monthly_doubling_does_not_touch_coins(self, db, cog, monkeypatch):
        _more_points_on(cog, monkeypatch)
        _make_bettor(db)
        self._issue_and_solve(db, cog, monkeypatch)
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        exp = _GITGUD_COIN_MULTIPLIER * 11
        assert '22 monthly' in ctx.sent[0][0]
        assert f'{exp} \U0001fa99' in ctx.sent[0][0]
        assert f'{2 * exp} \U0001fa99' not in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == 900 + exp

    def test_nonbettor_banks_silently(self, db, cog, monkeypatch):
        self._issue_and_solve(db, cog, monkeypatch)
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        exp = _GITGUD_COIN_MULTIPLIER * 11
        assert '11 alltime' in ctx.sent[0][0]
        assert '\U0001fa99' not in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == constants.BET_START_BALANCE + exp
        assert db.bet_has_wagered(GUILD, USER_A) is False
        ids = [row.user_id for row in db.bet_balance_leaderboard(GUILD)]
        assert USER_A not in ids

    def test_double_claim_pays_once(self, db, cog, monkeypatch):
        _make_bettor(db)
        self._issue_and_solve(db, cog, monkeypatch)
        _run(cog._gotgud_impl(_ctx()))
        after_first = db.bet_get_balance(GUILD, USER_A)
        with pytest.raises(CodeforcesCogError):
            _run(cog._gotgud_impl(_ctx()))
        assert db.bet_get_balance(GUILD, USER_A) == after_first


class TestH1H2Regressions:
    """H1: admin force-skips write FORCED_NOGUD (excluded from gitlog and
    future pools). H2: boosted scores persist per-row so monthly range
    queries match the announced totals."""

    def test_forced_skip_excluded_from_gitlog_and_pool(self, db, cog, monkeypatch):
        from types import SimpleNamespace

        from tle.util.db.user_db_conn import Gitgud

        _issue_level1(db, cog, monkeypatch)
        member = SimpleNamespace(id=USER_A, display_name='user')
        _run(cog._force_nogud_impl(_ctx(), member))
        assert db.count_active_challenges(USER_A) == 0
        assert db.gitlog(USER_A) == []
        assert db.get_nogud_problem_keys(USER_A) == set()
        rows = db.conn.execute(
            'SELECT status FROM challenge WHERE user_id=?', (USER_A,)).fetchall()
        assert {r[0] for r in rows} == {Gitgud.FORCED_NOGUD}

    def test_boosted_scores_visible_in_monthly_range(self, db, cog, monkeypatch):
        import time

        actives = _issue_level1(db, cog, monkeypatch)
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        _run(cog._gotgud_impl(_ctx()))
        now = time.time()
        rows = db.get_gudgitters_timerange(now - 3600, now + 3600)
        total = sum(r[1] for r in rows if str(r[0]) == str(USER_A))
        assert total == 11
        per_user = db.get_gudgitters_timerange_for_user(USER_A, now - 3600, now + 3600)
        assert sum(r[0] for r in per_user) == 11
