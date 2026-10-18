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
from tests.gitgud_test_utils import _install_caches
from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_args import GITGUD_USAGE
from tle.util._cf_api_types import Problem, cf_tag_matches


class TestCfTagMatcher:
    CASES = [
        ('dp', 'dp', True),
        ('data', 'data structures', True),
        ('bin', 'binary search', True),
        ('search', 'binary search', True),
        ('DATA', 'data structures', True),
        ('Div1', 'div1', True),
        ('data structures', 'data structures', True),
        ('*special', '*special problem', True),
        ('', 'dp', True),
        ('arc', 'binary search', False),
        ('earc', 'binary search', False),
    ]

    @pytest.mark.parametrize('filt,tag,want', CASES)
    def test_matching(self, filt, tag, want):
        assert cf_tag_matches(filt, tag) is want


class TestAcBackendTagDetection:
    @pytest.fixture(autouse=True)
    def ac_cache(self, monkeypatch):
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            atcoder_problem_cache=SimpleNamespace(
                problems=[SimpleNamespace(contest_type='abc'),
                          SimpleNamespace(contest_type='arc')])))

    def test_unknown_contest_type_raises(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _AcBackend().parse_request(['+dp'], 1500, GITGUD_USAGE)
        assert '+dp' in str(exc.value)

    def test_known_contest_types_pass(self):
        req = _AcBackend().parse_request(['+abc', '~arc', '1500'], 1500, GITGUD_USAGE)
        assert req.tags == ['abc']
        assert req.bantags == ['arc']

    def test_partial_word_not_enough_on_atcoder(self):
        # Exact matching: 'ab' is not an AtCoder contest type even though
        # 'abc' is (unlike Codeforces word-prefix semantics).
        with pytest.raises(CodeforcesCogError):
            _AcBackend().parse_request(['+ab'], 1500, GITGUD_USAGE)


class TestCfBackendTagDetection:
    @pytest.fixture(autouse=True)
    def cf_cache(self, monkeypatch):
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            problem_cache=SimpleNamespace(
                problems=[SimpleNamespace(tags=['dp', 'data structures']),
                          SimpleNamespace(tags=['div1'])])))

    def test_infix_of_word_rejected(self):
        # 'arc' hides inside 'search' but is not a prefix of any word, so
        # +arc must be an error, not a silent binary-search filter.
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().parse_request(['+arc'], 1500, GITGUD_USAGE)
        assert '+arc' in str(exc.value)

    def test_known_filters_parse_through(self):
        for arg in ('+data', '+DATA', '+div1', '+data structures'):
            req = _CfBackend().parse_request([arg, '1500'], 1500, GITGUD_USAGE)
            assert req.tags == [arg[1:]]

    def test_gimme_unknown_tag_raises(self):
        # Gimme shares the parser, so the offender is named before the pool
        # is ever selected.
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().parse_request(['+trashproblem'], 1500, GITGUD_USAGE)
        assert '+trashproblem' in str(exc.value)

    def test_cold_cache_disables_check(self, monkeypatch):
        _install_caches(monkeypatch)
        req = _CfBackend().parse_request(['+anything'], 1500, GITGUD_USAGE)
        assert req.tags == ['anything']


def _make_cf_problem(tags):
    return Problem(contestId=1, problemsetName=None, index='A', name='X',
                   type='PROGRAMMING', points=None, rating=1200, tags=tags)


class TestProblemTagSelection:
    def test_arc_no_longer_selects_binary_search(self):
        prob = _make_cf_problem(['binary search'])
        assert not prob.matches_all_tags(['arc'])
        assert not prob.matches_any_tag(['arc'])
        assert prob.matches_all_tags(['bin'])
        assert prob.get_matched_tags(['bin']) == ['binary search']

    def test_nonstandard_special_check_still_works(self):
        assert _make_cf_problem(
            ['*special problem']).matches_all_tags(['*special'])
