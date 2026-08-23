"""Unit tests for the extracted GitgudScoreModel and per-platform ladders.

Covers the floor->round (bankers) fix needed for AtCoder's scaled step
and the fact that Codeforces and AtCoder have distinct free-tag sets.
"""
import pytest

from tle.cogs._gitgud_scoring import AC_SCORE_MODEL, CF_SCORE_MODEL, GitgudScoreModel


class TestRawScoreCF:
    def test_edges(self):
        assert CF_SCORE_MODEL.raw_score(-5000) == 1
        assert CF_SCORE_MODEL.raw_score(-400) == 1
        assert CF_SCORE_MODEL.raw_score(300) == 23
        assert CF_SCORE_MODEL.raw_score(9000) == 23

    def test_ladder_rungs(self):
        # Bankers round: -350 (0.5) -> 0, -250 (1.5) -> 2, -150 (2.5) -> 2
        assert CF_SCORE_MODEL.raw_score(-350) == 1  # 0.5 bankers to 0
        assert CF_SCORE_MODEL.raw_score(-300) == 2
        assert CF_SCORE_MODEL.raw_score(-250) == 3  # 1.5 bankers to 2
        assert CF_SCORE_MODEL.raw_score(-200) == 3
        assert CF_SCORE_MODEL.raw_score(-150) == 3  # 2.5 bankers to 2
        assert CF_SCORE_MODEL.raw_score(-100) == 5
        assert CF_SCORE_MODEL.raw_score(0) == 8
        assert CF_SCORE_MODEL.raw_score(100) == 12
        assert CF_SCORE_MODEL.raw_score(200) == 17
        # 150 is the classic half-point: 5.5 -> 6 bankers -> 17 (floor would be 12)
        assert CF_SCORE_MODEL.raw_score(150) == 17
        assert CF_SCORE_MODEL.raw_score(250) == 17  # 6.5 bankers to 6 -> 17
        assert CF_SCORE_MODEL.raw_score(260) == 23  # 6.6 -> 7 -> 23

    def test_matches_legacy_floor_for_exact_multiples(self):
        for d in (-400, -300, -200, -100, 0, 100, 200, 300):
            assert CF_SCORE_MODEL.raw_score(d) == (1, 2, 3, 5, 8, 12, 17, 23)[(d + 400) // 100]


class TestRawScoreAC:
    S = 1031 / 780

    def test_scaled_edges(self):
        assert AC_SCORE_MODEL.raw_score(-10000) == 1
        assert AC_SCORE_MODEL.raw_score(-400 * self.S) == 1
        assert AC_SCORE_MODEL.raw_score(300 * self.S) == 23

    def test_half_rung_uses_round_not_floor(self):
        # CF delta 150 with floor gave 12, with round gives 17.  AC's step is
        # 100*S ≈132.  An AC delta of 150 should be ~5.13 steps -> round 5 -> 12,
        # not 17.  This proves the scaled step + round matters.
        assert CF_SCORE_MODEL.raw_score(150) == 17
        assert AC_SCORE_MODEL.raw_score(150) == 12
        # AC delta 200 ≈5.51 steps -> round 6 -> 17, matching the earlier
        # CF-equivalent difficulty.
        assert AC_SCORE_MODEL.raw_score(200) == 17
        assert AC_SCORE_MODEL.raw_score(0) == 8
        assert AC_SCORE_MODEL.raw_score(132) == 12  # ~one scaled step above 0


class TestPenalisedCountPerPlatform:
    def test_cf_free_filters(self):
        assert CF_SCORE_MODEL.penalised_count(['div1'], []) == 0
        assert CF_SCORE_MODEL.penalised_count([], ['div3', 'div4', 'edu']) == 0
        assert CF_SCORE_MODEL.penalised_count(['edu'], []) == 1
        assert CF_SCORE_MODEL.penalised_count(['arc'], []) == 1  # not free on CF
        assert CF_SCORE_MODEL.penalised_count(['abc'], []) == 1
        assert CF_SCORE_MODEL.penalised_count([], ['abc']) == 1

    def test_ac_free_filters(self):
        assert AC_SCORE_MODEL.penalised_count(['arc'], []) == 0
        assert AC_SCORE_MODEL.penalised_count(['agc'], []) == 0
        assert AC_SCORE_MODEL.penalised_count([], ['abc']) == 0
        assert AC_SCORE_MODEL.penalised_count(['div1'], []) == 1  # not free on AC
        assert AC_SCORE_MODEL.penalised_count(['dp'], []) == 1
        assert AC_SCORE_MODEL.penalised_count(['abc'], []) == 1
        assert AC_SCORE_MODEL.penalised_count([], ['arc']) == 1

    def test_case_insensitive_and_stripped(self):
        assert CF_SCORE_MODEL.penalised_count([' Div1 '], []) == 0
        assert AC_SCORE_MODEL.penalised_count([' ARC '], []) == 0


class TestFinalScore:
    def test_cf_penalty_halves_ceiling(self):
        assert CF_SCORE_MODEL.final_score(300, ['dp'], []) == 12  # 23/2 ceiling
        assert CF_SCORE_MODEL.final_score(0, ['dp'], []) == 4    # 8/2

    def test_ac_penalty_via_model(self):
        # AC difficulty 1800 vs base 1600 => delta 200 -> raw 17 -> 1 tag => 9
        delta, score = AC_SCORE_MODEL.delta_and_score(1800, 1600, ['dp'], [])
        assert delta == 200
        assert score == 9
        # Hard arc filter is free on AC
        delta, score = AC_SCORE_MODEL.delta_and_score(1800, 1600, ['arc'], [])
        assert score == 17


class TestDeltaAndScore:
    def test_cf_problem_rating(self):
        class P:
            rating = 2500
        d, s = CF_SCORE_MODEL.delta_and_score(P().rating, 2200, ['dp'], [])
        assert d == 300 and s == 12

    def test_ac_problem_difficulty(self):
        class P:
            rating = 1800
        d, s = AC_SCORE_MODEL.delta_and_score(P().rating, 1600, [], [])
        assert d == 200 and s == 17

    def test_unified_rating_of_path(self):
        # Forced ``problem.rating`` on both platforms.
        from tle.cogs._atcoder_gitgud import _AcBackend
        from tle.cogs._codeforces_gitgud import _CfBackend
        ac = _AcBackend()
        cf = _CfBackend()
        # CF probe: problem rating 2100 vs base 1900 => delta 200 -> 17
        class CfP:
            rating = 2100
        assert cf.score_model.final_score(200) == 17
        assert cf.score_model.delta_and_score(CfP().rating, 1900) == (200, 17)
        class AcP:
            rating = 1500
        assert ac.score_model.final_score(300) != ac.score_model.final_score(150)
        assert ac.score_model.delta_and_score(AcP().rating, 1200) == (300, ac.score_model.raw_score(300))


class TestCompatShims:
    def test_scoring_module_is_canonical(self):
        from tle.cogs._gitgud_scoring import CF_SCORE_MODEL, DISTRIB

        assert DISTRIB == (1, 2, 3, 5, 8, 12, 17, 23)
        assert CF_SCORE_MODEL.raw_score(0) == 8
        assert CF_SCORE_MODEL.penalised_count(['div1'], []) == 0
        assert CF_SCORE_MODEL.penalty_score(300, 1) == 12
