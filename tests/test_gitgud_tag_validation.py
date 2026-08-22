"""Unknown-tag detection for gitgud/gimme.

A ``+``/``~`` filter that matches nothing in the platform's vocabulary used
to silently empty the pool into a misleading 'No problem to assign'; it now
raises naming the offender. Detection mirrors each platform's matcher:
substring against Codeforces problem tags (including the synthesized
division tags), exact against AtCoder contest types.
"""
from types import SimpleNamespace

import pytest

import tle.util.codeforces_common as cf_common
from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import (
    CodeforcesCogError,
    _checkGitgudTags,
    _unknownTagFilters,
)


class TestUnknownTagFilters:
    VOCAB = {'dp', 'data structures', 'div1', 'strings'}

    def test_substring_hit(self):
        assert _unknownTagFilters(['data'], self.VOCAB) == []

    def test_miss_reported(self):
        assert _unknownTagFilters(['trashproblem'], self.VOCAB) == [
            'trashproblem']

    def test_division_tags_are_part_of_vocabulary(self):
        assert _unknownTagFilters(['div1'], self.VOCAB) == []

    def test_exact_mode(self):
        assert _unknownTagFilters(['ab'], self.VOCAB, exact=True) == ['ab']
        assert _unknownTagFilters(['dp'], self.VOCAB, exact=True) == []

    def test_empty_vocabulary_disables_check(self):
        assert _unknownTagFilters(['anything'], set()) == []


class TestCheckGitgudTags:
    VOCAB = {'dp', 'div1'}

    def test_unknown_required_named_with_plus(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _checkGitgudTags(['trashproblem'], [], self.VOCAB)
        assert '+trashproblem' in str(exc.value)

    def test_unknown_banned_named_with_tilde(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _checkGitgudTags([], ['nope'], self.VOCAB)
        assert '~nope' in str(exc.value)

    def test_all_known_passes(self):
        _checkGitgudTags(['dp'], ['div1'], self.VOCAB)

    def test_multiple_offenders_listed(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _checkGitgudTags(['a1', 'dp'], ['b2'], self.VOCAB)
        msg = str(exc.value)
        assert '+a1' in msg and '~b2' in msg and '+dp' not in msg


class TestAcBackendTagDetection:
    @pytest.fixture(autouse=True)
    def ac_cache(self, monkeypatch):
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            atcoder_problem_cache=SimpleNamespace(
                problems=[SimpleNamespace(contest_type='abc'),
                          SimpleNamespace(contest_type='arc')])))

    def test_unknown_contest_type_raises(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _AcBackend().parse_args(['+dp'], 1500)
        assert '+dp' in str(exc.value)

    def test_known_contest_types_pass(self):
        _, _, _, tags, bantags = _AcBackend().parse_args(
            ['+abc', '~arc', '1500'], 1500)
        assert tags == ['abc']
        assert bantags == ['arc']

    def test_partial_word_not_enough_on_atcoder(self):
        # Exact matching: 'ab' is not an AtCoder contest type even though
        # 'abc' is (unlike Codeforces substring semantics).
        with pytest.raises(CodeforcesCogError):
            _AcBackend().parse_args(['+ab'], 1500)


class TestCfBackendTagDetection:
    @pytest.fixture(autouse=True)
    def cf_cache(self, monkeypatch):
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            problem_cache=SimpleNamespace(
                problems=[SimpleNamespace(tags=['dp', 'data structures']),
                          SimpleNamespace(tags=['div1'])])))

    def test_unknown_tag_raises(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().parse_args(['+trashproblem'], 1500)
        assert '+trashproblem' in str(exc.value)

    def test_partial_word_matches_via_substring(self):
        # '+data' must stay valid: it substring-matches 'data structures'.
        _, _, _, tags, _ = _CfBackend().parse_args(['+data', '1500'], 1500)
        assert tags == ['data']

    def test_synthesized_division_tag_valid(self):
        _, _, _, tags, _ = _CfBackend().parse_args(['+div1', '1500'], 1500)
        assert tags == ['div1']

    def test_gimme_unknown_tag_raises(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().select_gimme_pool(['+trashproblem'], None, set(),
                                           1500)
        assert '+trashproblem' in str(exc.value)
