"""Rating-argument parsing for gitgud/gimme across both platforms.

The shared classifier lives in ``_codeforces_helpers.py``. These tests pin
the edge cases that motivated it: sub-3-digit AtCoder ratings and ranges
(``0-1800`` used to be silently discarded by an ``arg[0:3].isdigit()``
slice check), malformed specs like ``800-`` (used to crash on ``int('')``),
junk arguments, inverted ranges, and the whole-range bounds rules.
"""
import pytest

import tle.util.codeforces_common as cf_common
from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import (
    CodeforcesCogError,
    _MULTIWORD_TAG_HINT,
    _parseGitgudRatingArgs,
)

_MSG_CF = ('Wrong rating requested. Remember gitgud now uses rating '
           '(800-3500) instead of delta.')


@pytest.fixture(autouse=True)
def empty_caches(monkeypatch):
    """No cached problems, so tag-existence checks become no-ops and these
    tests exercise only rating parsing. Tag detection has its own file."""
    from types import SimpleNamespace
    monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
        problem_cache=SimpleNamespace(problems=[]),
        atcoder_problem_cache=SimpleNamespace(problems=[])))


class TestParseGitgudRatingArgs:
    def test_defaults_passthrough(self):
        assert _parseGitgudRatingArgs([], 1500, _MSG_CF) == (1500, 1500, False)

    def test_single_spec(self):
        assert _parseGitgudRatingArgs(['1200'], 1500, _MSG_CF) == (
            1200, 1200, False)

    def test_range_sets_hidden(self):
        assert _parseGitgudRatingArgs(['1000-1400'], 1500, _MSG_CF) == (
            1000, 1400, True)

    def test_equal_range_still_hidden(self):
        assert _parseGitgudRatingArgs(['1200-1200'], 1500, _MSG_CF) == (
            1200, 1200, True)

    def test_last_spec_wins(self):
        assert _parseGitgudRatingArgs(['1200', '1400-1500'], 1500, _MSG_CF) == (
            1400, 1500, True)

    def test_short_single_specs(self):
        assert _parseGitgudRatingArgs(['0'], 1500, _MSG_CF) == (0, 0, False)
        assert _parseGitgudRatingArgs(['99'], 1500, _MSG_CF) == (99, 99, False)

    def test_short_range_specs(self):
        assert _parseGitgudRatingArgs(['0-1800'], 1500, _MSG_CF) == (
            0, 1800, True)
        assert _parseGitgudRatingArgs(['50-999'], 1500, _MSG_CF) == (
            50, 999, True)

    def test_dates_are_skipped(self):
        args = ['d<2020-01-01', 'd>=2019-01-01', '1500']
        assert _parseGitgudRatingArgs(args, 1500, _MSG_CF) == (
            1500, 1500, False)

    def test_tags_are_skipped(self):
        assert _parseGitgudRatingArgs(['+dp', '~math', '1300'], 1500,
                                      _MSG_CF) == (1300, 1300, False)

    def test_empty_token_ignored(self):
        assert _parseGitgudRatingArgs([''], 1500, _MSG_CF) == (1500, 1500,
                                                               False)

    def test_malformed_range_raises(self):
        with pytest.raises(CodeforcesCogError, match='Invalid rating `800-`'):
            _parseGitgudRatingArgs(['800-'], 1500, _MSG_CF)

    def test_malformed_digits_raise(self):
        with pytest.raises(CodeforcesCogError, match='Invalid rating `12x`'):
            _parseGitgudRatingArgs(['12x'], 1500, _MSG_CF)

    def test_leading_dash_plain_error(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _parseGitgudRatingArgs(['-100'], 1500, _MSG_CF)
        assert str(exc.value) == _MSG_CF

    def test_junk_raises_with_hint(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _parseGitgudRatingArgs(['abc'], 1500, _MSG_CF,
                                   junk_hint=_MULTIWORD_TAG_HINT)
        msg = str(exc.value)
        assert 'Unexpected argument `abc`' in msg
        assert _MULTIWORD_TAG_HINT in msg

    def test_junk_raises_without_hint(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _parseGitgudRatingArgs(['abc'], 1500, _MSG_CF)
        assert 'Unexpected argument `abc`' in str(exc.value)
        assert '+data structures' not in str(exc.value)

    def test_inverted_range_raises(self):
        with pytest.raises(CodeforcesCogError):
            _parseGitgudRatingArgs(['1800-1200'], 1500, _MSG_CF)

    def test_cf_bounds_unreachable_raises(self):
        for arg in ['700', '3600', '0', '4000-5000']:
            with pytest.raises(CodeforcesCogError):
                _parseGitgudRatingArgs([arg], 1500, _MSG_CF,
                                       bounds=(800, 3500))

    def test_cf_bounds_overlap_ok(self):
        assert _parseGitgudRatingArgs(['500-900'], 1500, _MSG_CF,
                                      bounds=(800, 3500)) == (500, 900, True)
        assert _parseGitgudRatingArgs(['0-1800'], 1500, _MSG_CF,
                                      bounds=(800, 3500)) == (0, 1800, True)

    def test_ac_bounds_unreachable_raises(self):
        for arg in ['5000', '-5']:
            with pytest.raises(CodeforcesCogError):
                _parseGitgudRatingArgs([arg], 1500, _MSG_CF, bounds=(0, 4000))

    def test_ac_bounds_overlap_ok(self):
        assert _parseGitgudRatingArgs(['1000-5000'], 1500, _MSG_CF,
                                      bounds=(0, 4000)) == (1000, 5000, True)

    def test_default_bypasses_bounds(self):
        # gimme passes the raw user rating as default; extreme-rated users
        # must not be locked out by validation meant for explicit specs.
        assert _parseGitgudRatingArgs([], 700, _MSG_CF,
                                      bounds=(800, 3500)) == (700, 700, False)


class TestAcParseArgs:
    @pytest.fixture(autouse=True)
    def backend(self):
        self.backend = _AcBackend()

    def test_low_range_selects_not_falls_back(self):
        got = self.backend.parse_args(['0-1800'], 1500)
        assert got[:3] == (0, 1800, True)

    def test_short_range_selects(self):
        got = self.backend.parse_args(['50-999'], 1500)
        assert got[:3] == (50, 999, True)

    def test_zero_widens_clamped(self):
        got = self.backend.parse_args(['0'], 1500)
        assert got[:3] == (0, 100, False)

    def test_regression_single(self):
        got = self.backend.parse_args(['1200'], 1500)
        assert got[:3] == (1100, 1300, False)

    def test_regression_range(self):
        got = self.backend.parse_args(['1000-1400'], 1500)
        assert got[:3] == (1000, 1400, True)

    def test_crash_case_now_friendly_error(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.parse_args(['800-'], 1500)

    def test_above_max_raises(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.parse_args(['5000'], 1500)

    def test_overlap_accepted(self):
        got = self.backend.parse_args(['1000-5000'], 1500)
        assert got[:3] == (1000, 5000, True)

    def test_negative_raises(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.parse_args(['-100'], 1500)


class TestCfParseArgs:
    @pytest.fixture(autouse=True)
    def backend(self):
        self.backend = _CfBackend()

    def test_low_range_selects_not_falls_back(self):
        got = self.backend.parse_args(['0-1800'], 1500)
        assert got[:3] == (0, 1800, True)

    def test_partial_overlap_accepted(self):
        got = self.backend.parse_args(['500-900'], 1500)
        assert got[:3] == (500, 900, True)

    def test_below_min_raises(self):
        for arg in ['0', '700', '799']:
            with pytest.raises(CodeforcesCogError):
                self.backend.parse_args([arg], 1500)

    def test_above_max_raises(self):
        for arg in ['3600', '4000-5000']:
            with pytest.raises(CodeforcesCogError):
                self.backend.parse_args([arg], 1500)

    def test_default_fallback_intact(self):
        got = self.backend.parse_args([], 1500)
        assert got[:3] == (1500, 1500, False)

    def test_regression_range(self):
        got = self.backend.parse_args(['1200-1400'], 1500)
        assert got[:3] == (1200, 1400, True)

    def test_crash_case_now_friendly_error(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.parse_args(['800-'], 1500)

    def test_unquoted_multiword_tag_hint(self):
        with pytest.raises(CodeforcesCogError) as exc:
            self.backend.parse_args(['+data', 'structures'], 1500)
        assert 'Unexpected argument `structures`' in str(exc.value)
        assert _MULTIWORD_TAG_HINT in str(exc.value)


class TestCfGimmeParsing:
    """Gimme parses before touching the problem cache, so error paths run
    without any cache fixtures."""

    @pytest.fixture(autouse=True)
    def backend(self):
        self.backend = _CfBackend()

    def test_malformed_range_raises(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.select_gimme_pool(['800-'], None, set(), 1500)

    def test_junk_raises(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.select_gimme_pool(['abc'], None, set(), 1500)

    def test_inverted_raises(self):
        with pytest.raises(CodeforcesCogError):
            self.backend.select_gimme_pool(['1800-1200'], None, set(), 1500)

    def test_valid_args_reach_pool_filter(self):
        problems, tags, hidden = self.backend.select_gimme_pool(
            ['+dp'], None, set(), 1500)
        assert problems == []
        assert tags == ['dp']
        assert hidden is False
