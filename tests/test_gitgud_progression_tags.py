"""Tag/bantag penalization for ``;gitgudprogression``.

Parity with ``;gitgud`` (``_gitgud_impl`` passes ``tags``/``bantags`` into
``score_model.delta_and_score``): requesting ``+``/``~`` filters divides each
slot's ladder score by ``penalised_count + 1`` (ceiling, min 1) while
``rating_delta`` stays raw. Free hardening filters (``+div1``,
``~div3/~div4/~edu``) cost nothing. The ThemeCP time bonus then composes on
the penalized base.
"""
from types import SimpleNamespace

from tests.betting_test_utils import GUILD, USER_A, db  # noqa: F401
from tests.gitgud_test_utils import (  # noqa: F401
    _backdate,
    _ctx,
    _make_bettor,
    _patch_cf_handle,
    _run,
    _solve_names,
    cog,
)
from tle.cogs._codeforces_helpers import _GITGUD_COIN_MULTIPLIER
from tle.util import codeforces_common as cf_common


def _tagged_prob(name, cid, rating, tags, index='A'):
    lowered = [t.strip().lower() for t in tags]

    def _matches_all(want):
        return all(w.strip().lower() in lowered for w in want)

    def _matches_any(banned):
        return any(b.strip().lower() in lowered for b in banned)

    return SimpleNamespace(
        name=name,
        contestId=cid,
        contest_name=f'Round {cid}',
        index=index,
        key=name,
        rating=rating,
        url=f'https://codeforces.com/contest/{cid}/problem/{index}',
        tags=list(tags),
        matches_all_tags=_matches_all,
        matches_any_tag=_matches_any,
        get_matched_tags=lambda want: [w for w in want if w.strip().lower() in lowered],
    )


_VOCAB_TAGS = ('dp', 'fft', 'div1', 'div3', 'div4', 'edu')


def _install_cache(monkeypatch, probs):
    """Swap the ``cog``-fixture cache; always add an off-rating vocab filler
    so banned tags not present on the selectable pool still pass the unknown-
    tag check (mirrors the real CF cache, where banned tags exist on *some*
    problem)."""
    probs = list(probs) + [_tagged_prob('VOCAB_FILLER', 9999, 9999, _VOCAB_TAGS)]
    cache = cf_common.cache2
    monkeypatch.setattr(cache.problem_cache, 'problems', probs)
    monkeypatch.setattr(cache.problem_cache, 'problem_by_name', {p.name: p for p in probs})


def _install_level1(monkeypatch, tags):
    """Replace the ``cog``-fixture cache with 4 tag-carrying 800s (level 1)."""
    probs = [_tagged_prob(f'CF800_{i}', 1000 + i, 800, tags, chr(ord('A') + i))
             for i in range(4)]
    _install_cache(monkeypatch, probs)


def _install_ratings(monkeypatch, ratings, tags):
    probs = [_tagged_prob(f'CF{r}_{i}', 1000 + i, r, tags, chr(ord('A') + i))
             for i, r in enumerate(ratings)]
    _install_cache(monkeypatch, probs)


def _issue(db, cog, monkeypatch, args, rating=800, tags=('dp',)):
    _install_level1(monkeypatch, list(tags))
    _patch_cf_handle(monkeypatch, rating=rating, solved=set())
    _run(cog._gitgudprogression_impl(_ctx(), args))
    return db.list_active_challenges(USER_A)


class TestProgressionTagPenalty:
    def test_required_tag_penalises_each_slot(self, db, cog, monkeypatch):
        actives = _issue(db, cog, monkeypatch, ('1', '+dp'))
        assert [a.rating_delta for a in actives] == [-300] * 4
        assert [a.score for a in actives] == [1] * 4

    def test_delta_stays_raw(self, db, cog, monkeypatch):
        actives = _issue(db, cog, monkeypatch, ('1', '+dp'))
        # raw problem rating - clamped base, untouched by the penalty
        assert [a.rating_delta for a in actives] == [-300] * 4

    def test_embed_totals_reflect_penalty(self, db, cog, monkeypatch):
        _install_level1(monkeypatch, ['dp'])
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        ctx = _ctx()
        _run(cog._gitgudprogression_impl(ctx, ('1', '+dp')))
        embed = ctx.sent[0][1]
        fields = {f['name']: f['value'] for f in embed.fields}
        assert fields['Alltime points'] == '4'
        assert fields['Monthly points'] == '4'
        # penalized base 1 previews as 1 point, 2 with the positional bonus
        assert '(2 with bonus)' in embed.description

    def test_free_required_tag_costs_nothing(self, db, cog, monkeypatch):
        actives = _issue(db, cog, monkeypatch, ('1', '+div1'), tags=('div1',))
        assert [a.score for a in actives] == [2] * 4

    def test_bantag_penalises(self, db, cog, monkeypatch):
        # pool problems must NOT carry the banned tag to survive filtering
        actives = _issue(db, cog, monkeypatch, ('1', '~fft'), tags=('dp',))
        assert [a.score for a in actives] == [1] * 4

    def test_free_bantags_cost_nothing(self, db, cog, monkeypatch):
        actives = _issue(
            db, cog, monkeypatch, ('1', '~div3', '~div4', '~edu'), tags=('dp',))
        assert [a.score for a in actives] == [2] * 4

    def test_free_plus_penalised_mix_counts_one(self, db, cog, monkeypatch):
        actives = _issue(
            db, cog, monkeypatch, ('1', '+div1', '+dp'), tags=('div1', 'dp'))
        assert [a.score for a in actives] == [1] * 4

    def test_two_penalised_tags_divide_by_three(self, db, cog, monkeypatch):
        # level 1 base is 2 so one vs two tags both saturate at 1; use a
        # high-delta slot (1400 @ base 1100 -> ladder 23) to separate them.
        # pool carries +dp but not the banned tag: the ban still penalises
        # (penalty counts requested filters, not problem tags) while the pool
        # survives filtering.
        _install_ratings(monkeypatch, [800, 1000, 1200, 1400], ['dp'])
        _patch_cf_handle(monkeypatch, rating=800, solved=set())
        # level 13 is (800,1000,1200,1400)
        _run(cog._gitgudprogression_impl(_ctx(), ('13', '+dp', '~fft')))
        actives = db.list_active_challenges(USER_A)
        assert [a.score for a in actives] == [1, 2, 4, 8]


class TestProgressionTagBonusComposition:
    def test_bonus_applies_on_penalised_base(self, db, cog, monkeypatch):
        actives = _issue(db, cog, monkeypatch, ('1', '+dp'))
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        # penalized 1s with mults (1,1,1.5,2) -> 1,1,2,2 = 6
        assert db.get_gudgitter_score(USER_A) == 6
        assert '6 alltime' in ctx.sent[0][0]
        assert any('Bonus applied' in (m[0] or '') for m in ctx.sent[1:])

    def test_expired_window_keeps_penalised_base(self, db, cog, monkeypatch):
        actives = _issue(db, cog, monkeypatch, ('1', '+dp'))
        _backdate(db, 8000)
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        assert db.get_gudgitter_score(USER_A) == 4
        assert '4 alltime' in ctx.sent[0][0]
        assert not any('Bonus applied' in (m[0] or '') for m in ctx.sent)

    def test_coins_use_penalised_bonus_total(self, db, cog, monkeypatch):
        _make_bettor(db)
        actives = _issue(db, cog, monkeypatch, ('1', '+dp'))
        _solve_names(monkeypatch, [a.problem_key for a in actives])
        ctx = _ctx()
        _run(cog._gotgud_impl(ctx))
        exp = _GITGUD_COIN_MULTIPLIER * 6
        assert f'{exp} \U0001fa99' in ctx.sent[0][0]
        assert db.bet_get_balance(GUILD, USER_A) == 900 + exp
