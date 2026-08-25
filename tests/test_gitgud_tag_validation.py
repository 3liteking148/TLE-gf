"""Unknown-tag detection for gitgud/gimme.

A ``+``/``~`` filter that matches nothing in the platform's vocabulary used
to silently empty the pool into a misleading 'No problem to assign'; it now
raises naming the offender. Detection mirrors each platform's matcher:
case-insensitive word-prefix against Codeforces problem tags (including the
synthesized division tags), exact against AtCoder contest types.
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
from tle.util._cf_api_types import Problem, cf_tag_matches


class TestUnknownTagFilters:
    VOCAB = {'dp', 'data structures', 'div1', 'strings'}

    def test_word_prefix_hit(self):
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
        # 'abc' is (unlike Codeforces word-prefix semantics).
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

    def test_infix_of_word_rejected(self):
        # 'arc' hides inside 'search' but is not a prefix of any word of
        # 'binary search' — the motivating case: +arc must be an error,
        # not a silent binary-search filter.
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().parse_args(['+arc'], 1500)
        assert '+arc' in str(exc.value)

    def test_word_prefix_passes(self):
        _, _, _, tags, _ = _CfBackend().parse_args(['+data', '1500'], 1500)
        assert tags == ['data']

    def test_case_insensitive(self):
        for arg in ('+DATA', '+Div1'):
            _, _, _, tags, _ = _CfBackend().parse_args([arg, '1500'], 1500)
            assert tags == [arg[1:]]

    def test_quoted_multiword_filter_passes(self):
        _, _, _, tags, _ = _CfBackend().parse_args(
            ['+data structures', '1500'], 1500)
        assert tags == ['data structures']

    def test_synthesized_division_tag_valid(self):
        _, _, _, tags, _ = _CfBackend().parse_args(['+div1', '1500'], 1500)
        assert tags == ['div1']

    def test_gimme_unknown_tag_raises(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().select_gimme_pool(['+trashproblem'], None, set(),
                                           1500)
        assert '+trashproblem' in str(exc.value)


class TestCfTagMatcher:
    def test_exact_word(self):
        assert cf_tag_matches('dp', 'dp')

    def test_word_prefix(self):
        assert cf_tag_matches('data', 'data structures')
        assert cf_tag_matches('bin', 'binary search')

    def test_prefix_of_later_word(self):
        assert cf_tag_matches('search', 'binary search')

    def test_infix_rejected(self):
        assert not cf_tag_matches('arc', 'binary search')
        assert not cf_tag_matches('earc', 'binary search')

    def test_case_insensitive(self):
        assert cf_tag_matches('DATA', 'data structures')
        assert cf_tag_matches('Div1', 'div1')

    def test_multiword_filter_words_match_across_tag(self):
        assert cf_tag_matches('data structures', 'data structures')

    def test_special_problem_tag(self):
        assert cf_tag_matches('*special', '*special problem')

    def test_empty_filter_matches_everything(self):
        assert cf_tag_matches('', 'dp')


def _make_cf_problem(tags):
    return Problem(contestId=1, problemsetName=None, index='A', name='X',
                   type='PROGRAMMING', points=None, rating=1200, tags=tags)


class TestProblemTagSelection:
    def test_arc_no_longer_selects_binary_search(self):
        prob = _make_cf_problem(['binary search'])
        assert not prob.matches_all_tags(['arc'])
        assert not prob.matches_any_tag(['arc'])

    def test_word_prefix_selects(self):
        prob = _make_cf_problem(['binary search'])
        assert prob.matches_all_tags(['bin'])
        assert prob.get_matched_tags(['bin']) == ['binary search']

    def test_nonstandard_special_check_still_works(self):
        assert _make_cf_problem(['*special problem']).matches_all_tags(['*special'])
