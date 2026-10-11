"""Tests for the percentile → rating helper in graphs.py."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tle.cogs import _graph_helpers, _graph_render2 as _render2
from tle.cogs.graphs import _percentile_at_rating, _rating_at_percentile
from tle.cogs._graph_helpers import _parse_rating_argument, _resolve_percentile_argument
from tle.util.codeforces_api import User


class TestRatingAtPercentile:
    def test_empty_returns_none(self):
        assert _rating_at_percentile([], 50) is None

    def test_zero_percentile_returns_min(self):
        assert _rating_at_percentile([100, 200, 300, 400], 0) == 100

    def test_hundred_percentile_returns_max(self):
        # idx clamps to n-1 for p == 100
        assert _rating_at_percentile([100, 200, 300, 400], 100) == 400

    def test_middle_percentile(self):
        # n=4, p=50 → idx = int(0.5 * 4) = 2 → ratings[2] = 300
        assert _rating_at_percentile([100, 200, 300, 400], 50) == 300

    def test_unsorted_input_is_sorted(self):
        assert _rating_at_percentile([400, 100, 300, 200], 50) == 300

    def test_high_percentile_picks_top(self):
        # n=1000 even distribution 0..999, p=99.5 → idx = int(0.995*1000)=995
        ratings = list(range(1000))
        assert _rating_at_percentile(ratings, 99.5) == 995

    def test_low_percentile(self):
        ratings = list(range(1000))
        # p=10 → idx=100 → 100
        assert _rating_at_percentile(ratings, 10) == 100

    def test_single_element(self):
        assert _rating_at_percentile([1234], 0) == 1234
        assert _rating_at_percentile([1234], 50) == 1234
        assert _rating_at_percentile([1234], 100) == 1234

    def test_duplicates(self):
        # With duplicates, the returned rating is just whatever sits at the idx
        ratings = [100, 100, 100, 200, 200]
        # n=5, p=60 → idx = 3 → 200
        assert _rating_at_percentile(ratings, 60) == 200
        # p=20 → idx = 1 → 100
        assert _rating_at_percentile(ratings, 20) == 100

    def test_negative_percentile_raises(self):
        with pytest.raises(ValueError):
            _rating_at_percentile([100, 200], -1)

    def test_over_hundred_raises(self):
        with pytest.raises(ValueError):
            _rating_at_percentile([100, 200], 101)

    def test_consistency_with_centile_formula(self):
        """The centile command computes: for rating r, percentile = 100 * bisect_left(sorted, r) / n.
        Our inverse should round-trip for percentiles that land exactly on indices."""
        import bisect
        ratings = sorted([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        n = len(ratings)
        for i in range(n):
            r = ratings[i]
            p = 100 * bisect.bisect_left(ratings, r) / n
            recovered = _rating_at_percentile(ratings, p)
            # Recovered rating should equal original (for unique values)
            assert recovered == r, f'percentile {p} gave {recovered}, expected {r}'


class TestPercentileAtRating:
    def test_empty_returns_none(self):
        assert _percentile_at_rating([], 1500) is None

    def test_min_rating_returns_zero(self):
        # bisect_left at the minimum → 0 users strictly below
        assert _percentile_at_rating([100, 200, 300, 400], 100) == 0.0

    def test_top_rating_counts_strictly_below(self):
        # Top user: bisect_left returns n-1 → only the other users count
        # n=4, rating 400 → idx = 3 → 100*3/4 = 75.0
        assert _percentile_at_rating([100, 200, 300, 400], 400) == 75.0

    def test_above_top_rating_returns_hundred(self):
        # Above the max, bisect_left returns n → 100.0
        assert _percentile_at_rating([100, 200, 300, 400], 401) == 100.0

    def test_middle_rating(self):
        # n=4, rating 300 → idx = bisect_left = 2 → 100*2/4 = 50.0
        assert _percentile_at_rating([100, 200, 300, 400], 300) == 50.0

    def test_unsorted_input_is_sorted(self):
        assert _percentile_at_rating([400, 100, 300, 200], 300) == 50.0

    def test_below_min_clamps_to_zero(self):
        assert _percentile_at_rating([100, 200], 1) == 0.0

    def test_above_max_returns_hundred(self):
        assert _percentile_at_rating([100, 200], 9999) == 100.0

    def test_single_element(self):
        # n=1: the only user has nobody strictly below
        assert _percentile_at_rating([1234], 1234) == 0.0
        assert _percentile_at_rating([1234], 999) == 0.0

    def test_absent_rating_between_values(self):
        # 250 sits between 200 and 300 → idx = 2 → 50.0
        assert _percentile_at_rating([100, 200, 300, 400], 250) == 50.0

    def test_duplicates_count_strictly_below(self):
        # `;plot centile` convention: percentile counts users strictly below r
        ratings = [100, 100, 100, 200, 200]
        # rating 100 → bisect_left = 0 → 0.0
        assert _percentile_at_rating(ratings, 100) == 0.0
        # rating 200 → bisect_left = 3 → 100*3/5 = 60.0
        assert _percentile_at_rating(ratings, 200) == 60.0

    def test_round_trip_with_rating_at_percentile(self):
        """For unique ratings, percentile → rating → percentile recovers the
        stated percentile once it lands on an index boundary."""
        ratings = sorted([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        n = len(ratings)
        for r in ratings:
            p = _percentile_at_rating(ratings, r)
            recovered = _rating_at_percentile(ratings, p)
            assert recovered == r, f'rating {r} gave percentile {p}, expected rating {r}'

    def test_rating_at_percentile_lands_on_index(self):
        """percentilefor(ratingfor(p)) equals p rounded down to an achievable
        index, differing by at most one step (100/n)."""
        ratings = list(range(1000))
        n = len(ratings)
        for p in (0, 10, 50, 95, 99.5, 100):
            rating = _rating_at_percentile(ratings, p)
            recovered = _percentile_at_rating(ratings, rating)
            expected = 100 * min(int(p / 100 * n), n - 1) / n
            assert recovered == expected


def _make_user(handle, rating):
    return User(handle=handle,
                firstName='',
                lastName='',
                country='',
                city='',
                organization='',
                contribution=0,
                rating=rating,
                maxRating=rating,
                lastOnlineTimeSeconds=0,
                registrationTimeSeconds=0,
                friendOfCount=0,
                titlePhoto='')


class _FakeCtx:
    def __init__(self):
        self.author = 'author'
        self.sent = []

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)


class TestParseRatingArgument:
    def test_plain_int(self):
        assert _parse_rating_argument('1800') == 1800

    def test_signed_int(self):
        assert _parse_rating_argument('-100') == -100
        assert _parse_rating_argument('+1800') == 1800

    def test_surrounding_whitespace(self):
        assert _parse_rating_argument('  1800  ') == 1800

    def test_non_numeric_returns_none(self):
        assert _parse_rating_argument('tourist') is None
        assert _parse_rating_argument('1800.5') is None
        assert _parse_rating_argument('') is None
        assert _parse_rating_argument('1e3') is None


class TestResolvePercentileArgument:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_int_argument_skips_lookup(self):
        class _Cog:
            converter = None

        with patch('tle.cogs._graph_helpers.cf') as mock_cf, \
             patch('tle.cogs._graph_helpers.cf_common') as mock_common:
            mock_common.resolve_handles = AsyncMock(return_value=['tourist'])

            rating, handle = self._run(
                _resolve_percentile_argument(_Cog(), _FakeCtx(), '1800'))

        assert rating == 1800
        assert handle is None
        mock_common.resolve_handles.assert_not_called()
        mock_cf.user.info.assert_not_called()

    def test_username_resolved_to_rating(self):
        class _Cog:
            converter = None

        with patch('tle.cogs._graph_helpers.cf') as mock_cf, \
             patch('tle.cogs._graph_helpers.cf_common') as mock_common:
            mock_common.resolve_handles = AsyncMock(return_value=['tourist'])
            mock_cf.user.info = AsyncMock(
                return_value=[_make_user('tourist', 3786)])

            rating, handle = self._run(
                _resolve_percentile_argument(_Cog(), _FakeCtx(), 'tourist'))

        assert rating == 3786
        assert handle == 'tourist'
        mock_common.resolve_handles.assert_awaited_once()
        mock_cf.user.info.assert_awaited_once_with(handles=['tourist'])

    def test_unrated_username_raises(self):
        class _Cog:
            converter = None

        with patch('tle.cogs._graph_helpers.cf') as mock_cf, \
             patch('tle.cogs._graph_helpers.cf_common') as mock_common:
            mock_common.resolve_handles = AsyncMock(return_value=['some_newbie'])
            mock_cf.user.info = AsyncMock(
                return_value=[_make_user('some_newbie', None)])

            with pytest.raises(_graph_helpers.GraphCogError,
                               match='`some_newbie` is not rated'):
                self._run(_resolve_percentile_argument(_Cog(), _FakeCtx(),
                                                       'some_newbie'))


class TestPercentileForRender:
    """The render body wires resolution into the unchanged embed."""

    RATINGS = [100, 200, 300, 400]

    def _render(self, arg, user=None, resolved_handle=None):
        """Run _render2.percentilefor with resolution stubbed at the helper."""
        class _Cog:
            converter = None

        ctx = _FakeCtx()
        with patch('tle.cogs._graph_helpers.cf') as mock_cf, \
             patch('tle.cogs._graph_helpers.cf_common') as mock_common, \
             patch('tle.cogs._graph_render2.cf_common') as mock_render_common, \
             patch('tle.cogs._graph_render2.cf') as mock_render_cf:
            mock_common.resolve_handles = AsyncMock(
                return_value=[resolved_handle])
            mock_cf.user.info = AsyncMock(return_value=[user])
            mock_render_common._initialize_done = True
            mock_render_common.cache2.rating_changes_cache.get_all_ratings.return_value = self.RATINGS
            rank = MagicMock()
            rank.color_embed = 0xffaa10
            rank.title = 'Grandmaster'
            mock_render_cf.rating2rank.return_value = rank

            asyncio.run(_render2.percentilefor(_Cog(), ctx, arg))

        assert len(ctx.sent) == 1
        return ctx.sent[0], mock_common.resolve_handles

    def test_rating_argument_produces_embed(self):
        embed, resolve = self._render('300')
        assert embed.title.startswith('Percentile at rating 300')
        # rating 300 sits below exactly 2 of 4 users
        assert '**50** (top 50%)' in embed.description
        resolve.assert_not_called()

    def test_username_argument_produces_same_embed(self):
        user = _make_user('tourist', 300)
        embed, resolve = self._render('tourist', user=user,
                                      resolved_handle='tourist')
        assert embed.title.startswith('Percentile at rating 300')
        assert '**50** (top 50%)' in embed.description
        resolve.assert_awaited_once()
