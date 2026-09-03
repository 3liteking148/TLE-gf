"""Pure unit tests for progression bonus math, batch ids, arg parsing, and
generic batch-claim partitioning — no DB, no discord."""
import pytest

from tle.cogs._codeforces_helpers import (
    CodeforcesCogError,
    _checkGitgudTags,
    _parseGitgudRatingArgs,
)
from tle.util import gitgud_progression as gp
from tests.gitgud_test_utils import _cf_prob, _run


class _StubBackend:
    """Mirrors ``_CfBackend.parse_args`` rating/tag handling without the cache."""

    def __init__(self, vocab=frozenset({'dp', 'math'})):
        self._vocab = vocab

    def parse_args(self, args, rating):
        from tle.util import codeforces_common as cf_common
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')
        srating, erating, hidden = _parseGitgudRatingArgs(
            args, rating, 'Wrong rating requested.')
        _checkGitgudTags(tags, bantags, self._vocab)
        return srating, erating, hidden, tags, bantags


class TestBonusScores:
    def test_level1_shape(self):
        assert gp.bonus_scores([2, 2, 2, 2]) == [2, 2, 3, 4]

    def test_rounding_uses_bankers_round(self):
        # round(2.5) == 2 in Python; pin the behavior so a mult change is visible
        assert gp.bonus_scores([5, 5, 5, 5]) == [5, 5, 8, 10]

    def test_zero_scores_unchanged(self):
        assert gp.bonus_scores([0, 0, 0, 0]) == [0, 0, 0, 0]


class TestComputeBonusScores:
    def test_single_is_never_bonus(self):
        assert gp.compute_bonus_scores([8], 'prog-1-x', [200.0], 100.0) == ([8], None, None)

    def test_classic_batch_is_never_bonus(self):
        scores, window, mults = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'snowflake-123', [100.0] * 4, 100.0)
        assert scores == [2, 2, 2, 2]
        assert (window, mults) == (None, None)

    def test_within_window_applies_bonus(self):
        window, _ = gp.get_progression_bonus_context('prog-1-x')
        assert gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0 + window - 1] * 4, 100.0
        ) == ([2, 2, 3, 4], window, gp.THEME_MULTS)

    def test_exact_deadline_is_bonus_expiry_plus_one_is_not(self):
        window, _ = gp.get_progression_bonus_context('prog-1-x')
        ok, _, _ = gp.compute_bonus_scores([2, 2, 2, 2], 'prog-1-x', [100.0 + window] * 4, 100.0)
        late, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0 + window + 1] * 4, 100.0)
        assert ok == [2, 2, 3, 4]
        assert late == [2, 2, 2, 2]

    def test_split_times_bonus_early_only(self):
        window, _ = gp.get_progression_bonus_context('prog-1-x')
        late = 100.0 + window + 1
        # A, B, C solved fast keep their mults; late D falls back to base.
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0, 100.0, 100.0, late], 100.0)
        assert scores == [2, 2, 3, 2]
        # late C and D both fall back; early A, B keep theirs.
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0, 100.0, late, late], 100.0)
        assert scores == [2, 2, 2, 2]

    def test_zero_scores_pass_through_with_context(self):
        scores, window, mults = gp.compute_bonus_scores(
            [0, 0, 0, 0], 'prog-1-x', [100.0] * 4, 100.0)
        assert scores == [0, 0, 0, 0]
        assert window is not None and mults is not None

    def test_unknown_level_is_never_bonus(self):
        assert gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-9999-x', [100.0] * 4, 100.0
        ) == ([2, 2, 2, 2], None, None)

    def test_prefix_only_bonus(self):
        # full streak earns the whole ladder
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0, [True, True, True, True])
        assert scores == [2, 2, 3, 4]
        # lone D scores base
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0, [False, False, False, True])
        assert scores == [2, 2, 2, 2]
        # gap voids later slots: A solved, B missing, C+D solved -> only A bonused (x1: unchanged)
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0, [True, False, True, True])
        assert scores == [2, 2, 2, 2]
        # A+B+C streak keeps C's x1.5; D skipped
        scores, _, _ = gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0, [True, True, True, False])
        assert scores == [2, 2, 3, 2]
        # default (None) preserves legacy full-batch behaviour
        assert gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0
        ) == gp.compute_bonus_scores(
            [2, 2, 2, 2], 'prog-1-x', [100.0] * 4, 100.0, [True, True, True, True])


class TestBatchIdHelpers:
    def test_is_progression_batch(self):
        assert gp.is_progression_batch('prog-1-snowflake-9') is True
        assert gp.is_progression_batch('snowflake-9') is False
        assert gp.is_progression_batch('') is False
        assert gp.is_progression_batch(None) is False

    def test_parse_level(self):
        assert gp._parse_progression_level('prog-12-snowflake-9') == 12
        assert gp._parse_progression_level('prog-abc-x') is None
        assert gp._parse_progression_level('snowflake-9') is None

    def test_bonus_context(self):
        window, mults = gp.get_progression_bonus_context('prog-1-x')
        assert (window, mults) == (7200, gp.THEME_MULTS)
        assert gp.get_progression_bonus_context('snowflake-9') is None
        assert gp.get_progression_bonus_context('prog-9999-x') is None

    def test_batch_id_uses_snowflake_with_prefix(self):
        from types import SimpleNamespace
        from tle.cogs._gitgud import GitgudMixin
        ctx = SimpleNamespace(message=SimpleNamespace(id=42))
        mixin = GitgudMixin()
        assert mixin._batch_id_for_ctx(ctx, 100.0, prefix='prog-3-') == 'prog-3-snowflake-42'
        assert mixin._batch_id_for_ctx(ctx, 100.0) == 'snowflake-42'

    def test_batch_id_fallback_without_message(self):
        from types import SimpleNamespace
        from tle.cogs._gitgud import GitgudMixin
        mixin = GitgudMixin()
        assert mixin._batch_id_for_ctx(SimpleNamespace(), 100.0, prefix='prog-3-').startswith('prog-3-')


class TestParseProgressionArgs:
    def _parse(self, args, backend=None):
        from tle.cogs._gitgud_progression import parse_progression_args
        return parse_progression_args(args, backend or _StubBackend())

    def test_empty_is_usage_error(self):
        with pytest.raises(CodeforcesCogError, match='Usage'):
            self._parse(())

    def test_non_int_level_rejected(self):
        with pytest.raises(CodeforcesCogError, match='Invalid ThemeCP level'):
            self._parse(('x',))

    def test_out_of_range_level_rejected(self):
        with pytest.raises(CodeforcesCogError, match='Invalid ThemeCP level 9999'):
            self._parse(('9999',))

    def test_rating_spec_rejected(self):
        with pytest.raises(CodeforcesCogError, match='fixes ratings'):
            self._parse(('1', '800'))

    def test_range_spec_rejected(self):
        with pytest.raises(CodeforcesCogError, match='fixes ratings'):
            self._parse(('1', '800-900'))

    def test_tags_pass_through(self):
        from tle.cogs._gitgud_progression import require_theme
        level, theme, tags, bantags = self._parse(('2', '+dp', '~math'))
        assert (level, tags, bantags) == (2, ['dp'], ['math'])
        assert theme is require_theme(2)

    def test_unknown_tag_rejected(self):
        with pytest.raises(CodeforcesCogError, match=r'Unknown tag\(s\): \+nope'):
            self._parse(('1', '+nope'))

    def test_invalid_theme_level(self):
        from tle.cogs._gitgud_progression import require_theme
        with pytest.raises(CodeforcesCogError, match='Invalid ThemeCP level 9999'):
            require_theme(9999)


class TestDescLines:
    def test_four_lines_with_bonus_preview(self):
        from tle.cogs._gitgud_progression import build_progression_desc_lines
        probs = [_cf_prob(f'P{i}', 1, 800) for i in range(4)]
        lines = build_progression_desc_lines(probs, [2, 2, 2, 2])
        assert len(lines) == 4
        assert lines[0].endswith('×1 - 2 points (2 with bonus)')
        assert lines[2].endswith('×1.5 - 2 points (3 with bonus)')
        assert lines[3].endswith('×2 - 2 points (4 with bonus)')
        assert all('P' in line and '800' in line for line in lines)


class _PoolBackend:
    """Stub ``select_pool`` with canned per-rating pools."""

    def __init__(self, pools):
        self._pools = pools

    def select_pool(self, srating, erating, solved, noguds, tags, bantags, handle):
        return [p for p in self._pools.get(srating, [])
                if p.name not in solved and p.name not in noguds]


class TestSelectProgressionProblems:
    def _select(self, *args, **kwargs):
        from tle.cogs._gitgud_progression import select_progression_problems
        return select_progression_problems(*args, **kwargs)

    def test_selects_one_per_slot_distinct(self, monkeypatch):
        import random
        monkeypatch.setattr(random, 'randrange', lambda n: 0)
        pools = {800: [_cf_prob('A', 1, 800), _cf_prob('B', 1, 800)],
                 900: [_cf_prob('C', 1, 900), _cf_prob('E', 1, 900)]}
        # level 3 is (800,800,900,900)
        chosen = self._select(_PoolBackend(pools), 3, set(), set(), 'h')
        assert [p.name for p in chosen] == ['A', 'B', 'C', 'E']

    def test_does_not_mutate_caller_solved_set(self, monkeypatch):
        import random
        monkeypatch.setattr(random, 'randrange', lambda n: 0)
        pools = {800: [_cf_prob(f'P{i}', 1, 800) for i in range(8)]}
        solved = {'Solved0'}
        self._select(_PoolBackend(pools), 1, solved, set(), 'h')
        assert solved == {'Solved0'}

    def test_empty_pool_names_level_rating_and_tags(self):
        with pytest.raises(CodeforcesCogError) as exc:
            self._select(_PoolBackend({}), 1, set(), set(), 'h', tags=['dp'], bantags=['math'])
        msg = str(exc.value)
        assert 'progression level 1 rating 800' in msg
        assert '+dp' in msg and '~math' in msg

    def test_solved_and_nogud_filtered(self, monkeypatch):
        import random
        monkeypatch.setattr(random, 'randrange', lambda n: 0)
        pools = {800: [_cf_prob(f'P{i}', 1, 800) for i in range(8)]}
        chosen = self._select(_PoolBackend(pools), 1, {'P0', 'P1'}, {'P2'}, 'h')
        assert not ({p.name for p in chosen} & {'P0', 'P1', 'P2'})


class TestSplitSolvedActives:
    def test_order_preserving_partition(self):
        from tests.gitgud_test_utils import split_solved_actives
        from tle.util.db.challenge_db import ActiveChallenge
        actives = [ActiveChallenge(1, 0.0, 'A', 0, 0, 'cf', 'A', 2, 'b'),
                   ActiveChallenge(2, 0.0, 'B', 0, 0, 'cf', 'B', 2, 'b'),
                   ActiveChallenge(3, 0.0, 'C', 0, 0, 'cf', 'C', 2, 'b')]
        done, missing = split_solved_actives(actives, {'C', 'A'})
        assert [a.challenge_id for a in done] == [1, 3]
        assert [a.challenge_id for a in missing] == [2]

    def test_empty_solved_gives_empty_done(self):
        from tests.gitgud_test_utils import split_solved_actives
        from tle.util.db.challenge_db import ActiveChallenge
        actives = [ActiveChallenge(1, 0.0, 'A', 0, 0, 'cf', 'A', 2, 'b')]
        done, missing = split_solved_actives(actives, set())
        assert done == [] and [a.challenge_id for a in missing] == [1]


class TestSplitGotgudArgs:
    def test_flag_and_url_split(self):
        from tle.cogs._gitgud import _split_gotgud_args
        assert _split_gotgud_args(()) == (None, False)
        assert _split_gotgud_args(('+partial',)) == (None, True)
        url = 'https://atcoder.jp/contests/abc383/submissions/123'
        assert _split_gotgud_args((url,)) == (url, False)
        assert _split_gotgud_args((url, '+partial')) == (url, True)
        assert _split_gotgud_args(('+partial', url)) == (url, True)


def _actives():
    from tle.util.db.challenge_db import ActiveChallenge
    return [ActiveChallenge(1, 100.0, 'A', 1, 0, 'cf', 'A', 2, 'prog-1-x'),
            ActiveChallenge(2, 100.0, 'B', 2, 0, 'cf', 'B', 2, 'prog-1-x'),
            ActiveChallenge(3, 100.0, 'C', 3, 0, 'cf', 'C', 2, 'prog-1-x')]


def _backend_with_solved(monkeypatch, solved, ts=150.0):
    from tle.cogs._codeforces_gitgud import _CfBackend

    async def fake_times(self, handle, actives):
        return {a.challenge_id: ts for a in actives if a.problem_key in solved}

    monkeypatch.setattr(_CfBackend, 'fetch_solve_times', fake_times)
    return _CfBackend()


class TestVerifyClaimsPartition:
    def test_strict_returns_full_on_success(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, {'A', 'B', 'C'})
        done, missing, times = _run(backend.verify_claims(None, 'h', _actives()))
        assert [a.challenge_id for a in done] == [1, 2, 3] and missing == []
        assert times == {1: 150.0, 2: 150.0, 3: 150.0}

    def test_strict_raises_naming_missing(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, {'A'})
        with pytest.raises(CodeforcesCogError, match='missing 2: B, C'):
            _run(backend.verify_claims(None, 'h', _actives()))

    def test_partial_returns_ordered_partition(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, {'C', 'A'})
        done, missing, times = _run(backend.verify_claims(None, 'h', _actives(), partial=True))
        assert [a.challenge_id for a in done] == [1, 3]
        assert [a.challenge_id for a in missing] == [2]
        assert times == {1: 150.0, 3: 150.0}

    def test_partial_zero_solved_raises(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, set())
        with pytest.raises(CodeforcesCogError, match="haven't completed"):
            _run(backend.verify_claims(None, 'h', _actives(), partial=True))

    def test_partial_all_solved_matches_strict(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, {'A', 'B', 'C'})
        assert _run(backend.verify_claims(None, 'h', _actives(), partial=True)) == \
            _run(backend.verify_claims(None, 'h', _actives()))

    def test_singleton_flag_inert(self, monkeypatch):
        backend = _backend_with_solved(monkeypatch, {'A'})
        done, missing, times = _run(backend.verify_claims(None, 'h', _actives()[:1], partial=True))
        assert [a.challenge_id for a in done] == [1] and missing == []
        assert times == {1: 150.0}
        with pytest.raises(CodeforcesCogError, match="haven't completed"):
            _run(backend.verify_claims(None, 'h', _actives()[1:], partial=True))

    def test_atcoder_ignores_flag_on_singleton(self, monkeypatch):
        from tle.cogs._atcoder_gitgud import _AcBackend
        from tle.util.db.challenge_db import ActiveChallenge
        active = [ActiveChallenge(9, 100.0, 'abc1_a', 'abc1', 0, 'ac', None, 5, 'snowflake-1')]
        backend = _AcBackend()
        calls = []

        async def fake_single(ctx, handle, act, url=None):
            calls.append((handle, act, url))
            return 150.0

        monkeypatch.setattr(backend, '_verify_single_claim', fake_single)
        done, missing, times = _run(backend.verify_claims(None, 'h', active, partial=True))
        assert done == active and missing == []
        assert times == {9: 150.0}
        assert calls == [('h', active[0], None)]

    def test_atcoder_still_rejects_batches(self, monkeypatch):
        from tle.cogs._atcoder_gitgud import _AcBackend
        from tle.util.db.challenge_db import ActiveChallenge
        batch = [ActiveChallenge(1, 100.0, 'a', 'c', 0, 'ac', None, 5, 'b'),
                 ActiveChallenge(2, 100.0, 'd', 'c', 0, 'ac', None, 5, 'b')]
        with pytest.raises(CodeforcesCogError, match='single claims'):
            _run(_AcBackend().verify_claims(None, 'h', batch, partial=True))


def _subs(entries):
    """Build fake CF submissions from ``(name, contestId, index, ts)`` tuples."""
    from types import SimpleNamespace
    return [SimpleNamespace(
        verdict='OK',
        creationTimeSeconds=ts,
        problem=SimpleNamespace(name=name, contestId=cid, index=idx),
    ) for name, cid, idx, ts in entries]


def _patch_status(monkeypatch, subs):
    from types import SimpleNamespace
    from tle.util import codeforces_api as cf_api

    async def fake_status(*, handle):
        return subs

    monkeypatch.setattr(cf_api, 'user', SimpleNamespace(status=fake_status), raising=False)


class TestFetchSolveTimesMatching:
    """ID matching must require the (contestId, index) pair; a same-name
    solve in another contest must not credit the assigned problem."""

    def test_same_name_wrong_contest_does_not_claim(self, monkeypatch):
        from tle.cogs._codeforces_gitgud import _CfBackend
        _patch_status(monkeypatch, _subs([
            ('A', 999, 'Z', 150.0),
            ('B', 999, 'Z', 150.0),
            ('C', 999, 'Z', 150.0),
        ]))
        assert _run(_CfBackend().fetch_solve_times('h', _actives())) == {}
        with pytest.raises(CodeforcesCogError, match='missing 3'):
            _run(_CfBackend().verify_claims(None, 'h', _actives()))

    def test_correct_id_wrong_name_claims(self, monkeypatch):
        from tle.cogs._codeforces_gitgud import _CfBackend
        _patch_status(monkeypatch, _subs([
            ('Renamed A', 1, 'A', 150.0),
            ('Renamed B', 2, 'B', 150.0),
            ('Renamed C', 3, 'C', 150.0),
        ]))
        assert _run(_CfBackend().fetch_solve_times('h', _actives())) == {
            1: 150.0, 2: 150.0, 3: 150.0}

    def test_legacy_none_id_falls_back_to_name(self, monkeypatch):
        from tle.cogs._codeforces_gitgud import _CfBackend
        from tle.util.db.challenge_db import ActiveChallenge
        actives = [ActiveChallenge(9, 100.0, 'Legacy', None, 0, 'cf', None, 2, 'prog-1-x')]
        _patch_status(monkeypatch, _subs([('Legacy', 999, 'Z', 150.0)]))
        assert _run(_CfBackend().fetch_solve_times('h', actives)) == {9: 150.0}

    def test_str_int_id_normalization(self, monkeypatch):
        from tle.cogs._codeforces_gitgud import _CfBackend
        from tle.util.db.challenge_db import ActiveChallenge
        actives = [ActiveChallenge(9, 100.0, 'A', '1', 0, 'cf', 'a', 2, 'prog-1-x')]
        _patch_status(monkeypatch, _subs([('A', 1, 'A', 150.0)]))
        assert _run(_CfBackend().fetch_solve_times('h', actives)) == {9: 150.0}
