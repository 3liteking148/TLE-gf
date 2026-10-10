"""Tests for the percentile → rating helper in graphs.py."""
import pytest

from tle.cogs.graphs import _percentile_at_rating, _rating_at_percentile


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
