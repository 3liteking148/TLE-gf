"""Codeforces gitgud rating climb (``hint_climb``).

``;gitgud`` with no rating argument whose pool is empty — the user solved
(or noguded) everything at that rating, possibly with ``+tags`` — retries
successively higher ratings (100-point steps, up to the 3500 ceiling) instead
of failing with 'No problem to assign'. Explicit ``rating`` / ``lo-hi``
requests keep the exact-window behaviour, and the fixed-rating progression
slots never climb because they don't pass the hint.
"""
import pytest

from tests.betting_test_utils import USER_A, db  # noqa: F401
from tests.gitgud_test_utils import (  # noqa: F401
    _cf_prob,
    _ctx,
    _patch_cf_handle,
    _run,
    _set_cf_problems as _set_cache,
    _tagged_cf_prob as _tagged_prob,
    cog,
)

from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_args import GITGUD_USAGE


def _pool(backend, srating, erating, **kw):
    from tle.cogs._gitgud_args import GitgudArgs
    req = GitgudArgs(srating=srating, erating=erating, hidden=False)
    return backend.select(req, set(), set(), 'handle', **kw)


class TestGitgudRatingSpecified:
    @pytest.mark.parametrize('args,expected', [
        ((), False),
        (('+trees',), False),
        (('~dp',), False),
        (('',), False),
        (('1400',), True),
        (('1000-1400',), True),
        (('+dp', '1500'), True),
    ])
    def test_detection(self, args, expected):
        req = _CfBackend().parse_request(list(args), 1500, GITGUD_USAGE)
        assert req.rating_given is expected


class TestSelectPoolHintClimb:
    def test_no_climb_by_default(self, cog):
        _set_cache([_cf_prob('P900', 10, 900)])
        assert _pool(_CfBackend(), 800, 800) == []

    def test_climbs_to_next_rung(self, cog):
        _set_cache([_cf_prob('P900', 10, 900)])
        pool = _pool(_CfBackend(), 800, 800, hint_climb=True)
        assert [p.name for p in pool] == ['P900']

    def test_skips_rung_that_is_fully_solved(self, cog):
        from tle.cogs._gitgud_args import GitgudArgs
        _set_cache([
            _cf_prob('P900', 10, 900),
            _cf_prob('P1000', 11, 1000),
        ])
        pool = _CfBackend().select(
            GitgudArgs(srating=800, erating=800, hidden=False),
            {'P900'}, set(), 'handle', hint_climb=True)
        assert [p.name for p in pool] == ['P1000']

    def test_climb_reaches_3500(self, cog):
        _set_cache([_cf_prob('P3500', 10, 3500)])
        pool = _pool(_CfBackend(), 3300, 3300, hint_climb=True)
        assert [p.name for p in pool] == ['P3500']

    def test_climb_from_3400_user_rating(self, cog):
        _set_cache([_cf_prob('P3500', 10, 3500)])
        pool = _pool(_CfBackend(), 3400, 3400, hint_climb=True)
        assert [p.name for p in pool] == ['P3500']

    def test_never_exceeds_3500(self, cog):
        _set_cache([_cf_prob('P3600', 10, 3600)])
        assert _pool(_CfBackend(), 3400, 3400, hint_climb=True) == []

    def test_no_climb_from_3500(self, cog):
        _set_cache([_cf_prob('P3600', 10, 3600)])
        assert _pool(_CfBackend(), 3500, 3500, hint_climb=True) == []

    def test_empty_cache_climbs_nowhere(self, cog):
        _set_cache([])
        assert _pool(_CfBackend(), 800, 800, hint_climb=True) == []

    def test_tag_filters_apply_at_every_rung(self, cog):
        from tle.cogs._gitgud_args import GitgudArgs
        _set_cache([
            _tagged_prob('Trees1400', 10, 1400, ['trees']),
            _tagged_prob('Dp1500', 11, 1500, ['dp']),
            _tagged_prob('Trees1600', 12, 1600, ['trees']),
        ])
        pool = _CfBackend().select(
            GitgudArgs(srating=1400, erating=1400, hidden=False,
                       tags=['trees']),
            {'Trees1400'}, set(), 'handle', hint_climb=True)
        assert [p.name for p in pool] == ['Trees1600']

    def test_atcoder_backend_accepts_and_ignores_hint(self, cog):
        from tle.cogs._gitgud_args import GitgudArgs
        assert _AcBackend().select(
            GitgudArgs(srating=800, erating=900, hidden=False),
            set(), set(), 'handle', hint_climb=True) == []


class TestGitgudDefaultClimbEndToEnd:
    def test_default_request_climbs_to_900(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800,
                         solved={f'CF800_{i}' for i in range(4)})
        ctx = _ctx()
        _run(cog._gitgud_impl(ctx, ()))

        active = db.check_challenge(USER_A)
        assert active is not None
        assert active.problem_key == 'CF800_x0'
        assert active.platform == 'cf'
        assert active.rating_delta == -200  # 900 - delta_base 1100
        assert active.score == 3            # ladder rung for -200

        msg, embed, _view = ctx.sent[0]
        assert msg == 'Challenge problem for `handleA`'
        assert embed.fields[0]['value'] == 900

    def test_explicit_rating_does_not_climb(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800,
                         solved={f'CF800_{i}' for i in range(4)})
        with pytest.raises(CodeforcesCogError,
                           match='No problem to assign'):
            _run(cog._gitgud_impl(_ctx(), ('800',)))
        assert db.check_challenge(USER_A) is None

    def test_explicit_range_does_not_climb(self, db, cog, monkeypatch):
        _patch_cf_handle(monkeypatch, rating=800,
                         solved={f'CF800_{i}' for i in range(4)} |
                                {'CF800_x0', 'CF800_x1'})
        with pytest.raises(CodeforcesCogError,
                           match='No problem to assign'):
            _run(cog._gitgud_impl(_ctx(), ('800-900',)))
        assert db.check_challenge(USER_A) is None

    def test_tag_request_climbs_over_missing_rung(self, db, cog, monkeypatch):
        _set_cache([
            _tagged_prob('Trees1400', 11, 1400, ['trees']),
            _tagged_prob('Dp1500', 12, 1500, ['dp']),
            _tagged_prob('Trees1600', 13, 1600, ['trees']),
        ])
        _patch_cf_handle(monkeypatch, rating=1400, solved={'Trees1400'})
        ctx = _ctx()
        _run(cog._gitgud_impl(ctx, ('+trees',)))

        active = db.check_challenge(USER_A)
        assert active is not None
        assert active.problem_key == 'Trees1600'
        assert active.rating_delta == 200  # 1600 - delta_base 1400
        assert active.score == 9           # 17 for +200, halved for 1 tag

    def test_no_problem_anywhere_still_errors(self, db, cog, monkeypatch):
        _set_cache([_cf_prob('P800', 1, 800)])
        _patch_cf_handle(monkeypatch, rating=800, solved={'P800'})
        with pytest.raises(CodeforcesCogError,
                           match='No problem to assign'):
            _run(cog._gitgud_impl(_ctx(), ()))
        assert db.check_challenge(USER_A) is None
