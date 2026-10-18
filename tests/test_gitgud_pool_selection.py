"""Pool selection and command entry-point flow for the gitgud family.

Both ``;gitgud`` and ``;gimme`` read one request object, so the filters
(rating band, solved/nogud sets, tag includes/bans, contest-start window)
are applied by one predicate per platform, and ``;gitgud`` accepts the date
window that used to be gimme-only. The single difference is that ``;gimme``
passes ``exclude_nonstandard=False``: nonstandard problems stay in its pool
while ``;gitgud`` and progression exclude them.

The flow tests pin guard ordering: the platform split runs before handle
resolution, argument parsing runs before the submissions fetch, and
progression rejects any rating with its own usage instead of a Codeforces
bounds error.
"""
from types import SimpleNamespace

import asyncio

import pytest

from tests.betting_test_utils import db  # noqa: F401
from tests.gitgud_test_utils import (
    _ac_prob,
    _add_cf_problem,
    _cf_prob,
    _ctx,
    _patch_cf_handle,
    _run,
    _tagged_cf_prob,
    cog,  # noqa: F401
    parse_cf_request,
    select_cf_pool,
)

from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_args import GITGUD_USAGE
from tle.util import codeforces_common as cf_common

# Epoch seconds of the 2001-09-09 boundary used by the date tests.
BEFORE = 1000
AFTER = 10 ** 9


def _install(monkeypatch, problems, nonstandard=(), writers=()):
    """Cache with one contest per problem, starting at problem.start."""
    by_id = {p.contestId: SimpleNamespace(startTimeSeconds=p.start)
             for p in problems}
    prob_by_name = {p.name: p for p in problems}
    nonstandard = set(nonstandard)
    writers = set(writers)

    def _get_contest(cid):
        # Mirror the real cache: an unknown contest raises, and pool
        # selection must tolerate it (the old sort already did).
        if cid not in by_id:
            raise cf_common.cache_system2.ContestNotFound(cid)
        return by_id[cid]

    monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
        problem_cache=SimpleNamespace(
            problems=list(problems), problem_by_name=prob_by_name),
        contest_cache=SimpleNamespace(get_contest=_get_contest),
        atcoder_problem_cache=SimpleNamespace(problems=[])))
    monkeypatch.setattr(cf_common, 'is_contest_writer', lambda cid, h: cid in writers)
    monkeypatch.setattr(cf_common, 'is_nonstandard_problem', lambda p: p.contestId in nonstandard)


class TestGitgudDateWindow:
    def test_older_contest_excluded(self, monkeypatch):
        _install(monkeypatch, [
            _tagged_cf_prob('Old', 10, 1500, start=BEFORE),
            _tagged_cf_prob('New', 11, 1500, start=AFTER),
        ])
        pool = select_cf_pool(parse_cf_request(['d>=2001-09-09']))
        assert [p.name for p in pool] == ['New']

    def test_no_tokens_is_unbounded(self, monkeypatch):
        _install(monkeypatch, [
            _tagged_cf_prob('Old', 10, 1500, start=BEFORE),
            _tagged_cf_prob('New', 11, 1500, start=AFTER),
        ])
        pool = select_cf_pool(parse_cf_request([]))
        assert [p.name for p in pool] == ['Old', 'New']

    def test_window_survives_hint_climb(self, monkeypatch):
        _install(monkeypatch, [
            _tagged_cf_prob('Low', 10, 1000, start=BEFORE),
            _tagged_cf_prob('High', 11, 1600, start=AFTER),
        ])
        pool = select_cf_pool(parse_cf_request(['d>=2001-09-09']), hint_climb=True)
        assert [p.name for p in pool] == ['High']

    def test_upper_bound_is_exclusive(self, monkeypatch):
        _install(monkeypatch, [
            _tagged_cf_prob('AtStart', 10, 1500, start=10 ** 10 - 1),
            _tagged_cf_prob('After', 11, 1500, start=10 ** 10),
        ])
        pool = select_cf_pool(parse_cf_request([]))
        assert [p.name for p in pool] == ['AtStart']


class TestGitgudGimmePoolShared:
    """The shared predicate excludes solved, nogud and own problems for both
    commands; only nonstandard problems differ (gitgud excludes, gimme keeps)."""

    def _install_one(self, monkeypatch, **kw):
        _install(monkeypatch, [_tagged_cf_prob('P', 10, 1500)], **kw)

    def _select(self, req=None, *, solved=(), noguds=(), handle='handle',
                **kw):
        return _CfBackend().select(
            req or parse_cf_request([]), set(solved), set(noguds), handle,
            **kw)

    def test_excludes_noguds(self, monkeypatch):
        self._install_one(monkeypatch)
        assert self._select(noguds={'P'}) == []

    def test_excludes_nonstandard(self, monkeypatch):
        self._install_one(monkeypatch, nonstandard=(10,))
        assert self._select() == []

    def test_gimme_keeps_nonstandard(self, monkeypatch):
        self._install_one(monkeypatch, nonstandard=(10,))
        pool = self._select(exclude_nonstandard=False)
        assert [p.name for p in pool] == ['P']

    def test_gimme_still_excludes_noguds(self, monkeypatch):
        self._install_one(monkeypatch)
        assert self._select(noguds={'P'}, exclude_nonstandard=False) == []

    def test_gimme_still_excludes_own_writer_problem(self, monkeypatch):
        self._install_one(monkeypatch, writers=(10,))
        assert self._select(exclude_nonstandard=False) == []

    def test_excludes_own_writer_problem(self, monkeypatch):
        self._install_one(monkeypatch, writers=(10,))
        assert self._select() == []

    def test_other_users_writer_problem_kept(self, monkeypatch):
        self._install_one(monkeypatch, writers=(99,))
        assert [p.name for p in self._select()] == ['P']

    def test_honours_solved(self, monkeypatch):
        self._install_one(monkeypatch)
        assert self._select(solved={'P'}) == []

    def test_gitgud_climb_still_excludes_nonstandard(self, monkeypatch):
        _install(monkeypatch, [_tagged_cf_prob('P1600', 10, 1600)],
                 nonstandard=(10,))
        pool = self._select(parse_cf_request([]), hint_climb=True)
        assert pool == []

    def test_hidden_comes_from_request(self):
        assert parse_cf_request(['1400-1600']).hidden is True

    def test_date_window_applied(self, monkeypatch):
        self._install_one(monkeypatch)
        assert self._select(parse_cf_request(['d>=2001-09-09'])) == []


class TestUnknownContestTolerated:
    """The contest lookup runs after the cheap filters."""

    def test_date_check_runs_after_cheap_filters(self, monkeypatch):
        _install(monkeypatch, [_tagged_cf_prob('Known', 10, 1500, start=AFTER)])
        orphan = _cf_prob('Orphan', 77, 900)
        cf_common.cache2.problem_cache.problems.append(orphan)
        pool = select_cf_pool(parse_cf_request(['1500', 'd>=2001-09-09']))
        assert [p.name for p in pool] == ['Known']


class TestAtCoderGimme:
    def _install_ac(self, monkeypatch, problems):
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            problem_cache=SimpleNamespace(problems=[]),
            atcoder_problem_cache=SimpleNamespace(problems=problems)))

    def test_gimme_selects_from_rating_and_type(self, monkeypatch):
        self._install_ac(monkeypatch, [
            _ac_prob('abc100_a', BEFORE, rating=1500),
            _ac_prob('arc200_a', BEFORE, rating=1500),
        ])
        req = _AcBackend().parse_request(['+abc'], 1500, GITGUD_USAGE)
        pool = _AcBackend().select(req, set(), set(), 'h')
        assert [p.id for p in pool] == ['abc100_a']
        assert req.tags == ['abc'] and req.hidden is False

    def test_gimme_respects_date_window(self, monkeypatch):
        self._install_ac(monkeypatch, [
            _ac_prob('abc100_a', BEFORE),
            _ac_prob('abc300_a', AFTER),
        ])
        req = _AcBackend().parse_request(
            ['d>=2001-09-09', '+abc'], 1500, GITGUD_USAGE)
        pool = _AcBackend().select(req, set(), set(), 'h')
        assert [p.id for p in pool] == ['abc300_a']
        assert req.tags == ['abc']

    def test_gimme_excludes_noguds(self, monkeypatch):
        self._install_ac(monkeypatch, [_ac_prob('abc100_a', BEFORE)])
        req = _AcBackend().parse_request(['+abc'], 1500, GITGUD_USAGE)
        pool = _AcBackend().select(req, set(), {'abc100_a'}, 'h')
        assert pool == []

    def test_gitgud_pool_still_excludes_solved_and_noguds(self, monkeypatch):
        from tle.cogs._gitgud_args import GitgudArgs
        self._install_ac(monkeypatch, [_ac_prob('abc100_a', BEFORE)])
        req = GitgudArgs(srating=1400, erating=1600, hidden=False)
        assert _AcBackend().select(req, {'abc100_a'}, set(), 'h') == []
        assert _AcBackend().select(req, set(), {'abc100_a'}, 'h') == []

    def test_upsolve_still_unimplemented(self):
        with pytest.raises(CodeforcesCogError, match='not implemented for AtCoder'):
            asyncio.run(_AcBackend().fetch_participated('h'))


class _Stop(Exception):
    """Sentinel raised by a faked backend step to prove what was reached."""


def _freeze_submissions(monkeypatch):
    """Make the submissions fetch unmistakably loud if it runs."""
    from tle.util import codeforces_api as cf

    async def _boom(*a, **kw):
        raise _Stop('fetch_solved ran before parsing')

    _patch_cf_handle(monkeypatch, rating=1500, solved=set())
    # Applied after the handle patch so it wins over its ``cf.user`` stub.
    monkeypatch.setattr(cf.user, 'status', _boom)


def test_gimme_atcoder_fails_before_handle(cog):
    with pytest.raises(CodeforcesCogError, match='No AtCoder handle found'):
        _run(cog._gimme_impl(_ctx(), ('+atcoder',)))


def test_gimme_atcoder_flag_is_case_insensitive(cog):
    with pytest.raises(CodeforcesCogError, match='No AtCoder handle found'):
        _run(cog._gimme_impl(_ctx(), ('+AtCoder',)))


def test_gimme_parses_before_fetching_solved(cog, monkeypatch):
    _freeze_submissions(monkeypatch)
    with pytest.raises(CodeforcesCogError, match=r'Invalid rating `700`'):
        _run(cog._gimme_impl(_ctx(), ('700',)))


def test_gitgud_parses_before_fetching_solved(cog, monkeypatch):
    _freeze_submissions(monkeypatch)
    with pytest.raises(CodeforcesCogError, match=r'Invalid rating `700`'):
        _run(cog._gitgud_impl(_ctx(), ('700', '+dp')))


def test_gitgud_accepts_client_side_date_windows(cog, monkeypatch):
    _freeze_submissions(monkeypatch)
    with pytest.raises(_Stop):
        _run(cog._gitgud_impl(_ctx(), ('1200', 'd>=2020-01-01')))


def _stop_at_resolve(cog, monkeypatch):
    """Replace the backend lookup with one that stops at handle resolution."""
    class _Backend:
        platform = 'cf'

        async def resolve_handle(self, ctx, converter=None):
            raise _Stop('resolved')
    monkeypatch.setattr(cog, '_backend_for_platform', lambda platform: _Backend())


@pytest.mark.parametrize('args', [('+dp',), ('+5',), ('3',)])
def test_upsolve_plus_tokens_and_numbers_reach_backend(cog, monkeypatch, args):
    # Upsolve collects every ``+`` token as a platform argument and ignores
    # it, so ``+dp``/``+5`` resolve a backend just like a plain number does.
    _stop_at_resolve(cog, monkeypatch)
    with pytest.raises(_Stop):
        _run(cog._upsolve_impl(_ctx(), args))


def test_upsolve_invalid_choice_is_plain_error(cog):
    with pytest.raises(CodeforcesCogError, match=r'Invalid choice `abc`\.'):
        _run(cog._upsolve_impl(_ctx(), ('abc',)))


def test_upsolve_atcoder_fails_on_backend_after_handle(cog):
    with pytest.raises(CodeforcesCogError, match='No AtCoder handle found'):
        _run(cog._upsolve_impl(_ctx(), ('+atcoder',)))


def test_progression_rejects_rating_with_own_usage(cog, monkeypatch):
    _patch_cf_handle(monkeypatch, rating=800, solved=set())
    with pytest.raises(CodeforcesCogError) as exc:
        _run(cog._gitgudprogression_impl(_ctx(), ('1', '500')))
    msg = str(exc.value)
    assert 'takes only' in msg
    assert ';gitgudprogression' in msg
    assert 'rating range' not in msg


def test_gimme_recommends_with_shared_embed(cog, monkeypatch):
    _patch_cf_handle(monkeypatch, rating=800, solved=set())
    ctx = _ctx()
    _run(cog._gimme_impl(ctx, ('900',)))
    content, embed, _ = ctx.sent[-1]
    assert 'Recommended problem for' in content
    assert embed is not None
    assert 'Rating' in [f['name'] for f in embed.fields]
    assert embed.title is not None and '. ' in embed.title


def test_gimme_matched_tags_field(cog, monkeypatch):
    _add_cf_problem(_tagged_cf_prob('Tagged', 10, 1500, tags=['dp']))
    _patch_cf_handle(monkeypatch, rating=1500, solved=set())
    ctx = _ctx()
    _run(cog._gimme_impl(ctx, ('+dp',)))
    content, embed, _ = ctx.sent[-1]
    names = [f['name'] for f in embed.fields]
    assert 'Matched tags' in names


def test_gimme_keeps_nonstandard_problem(cog, monkeypatch):
    from tle.util import codeforces_common as cf_common
    _add_cf_problem(_tagged_cf_prob('Nonstandard', 10, 1500))
    monkeypatch.setattr(cf_common, 'is_nonstandard_problem', lambda p: True)
    _patch_cf_handle(monkeypatch, rating=1500, solved=set())
    ctx = _ctx()
    _run(cog._gimme_impl(ctx, ('1500',)))
    _, embed, _ = ctx.sent[-1]
    assert embed.title == 'A. Nonstandard'
