"""Argument parsing for gitgud/gimme across both platforms.

Pins the two error shapes of ``tle.cogs._gitgud_args``: a bad rating spec
raises ``Invalid rating ...`` with the platform bounds, while every other
unclassifiable token — bare words, ``-``/``+``/``~`` tokens, unknown tags,
and any non-tag token in tag-only mode — is reported together as
``Unexpected argument(s) ...``. Also pins the platform wiring (Codeforces/
AtCoder bounds, AtCoder's ±100 widening for an exact default), gimme's pool
filter, and the tag-only progression entry point. Malformed dates keep
``ParamParseError`` from the shared date parser. No DB, no discord, no
network.
"""
import pytest

from tests.gitgud_test_utils import (
    AC_VOCAB,
    CF_VOCAB,
    _install_caches,
    parse_ac_request as _ac,
    parse_cf_request as _cf,
)
from tle.cogs._atcoder_gitgud import _AcBackend
from tle.cogs._codeforces_gitgud import _CfBackend
from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_args import (
    GIMME_USAGE,
    GITGUD_USAGE,
    parse_tag_filters,
    split_platform,
)
from tle.util.cf_format import ParamParseError


@pytest.fixture(autouse=True)
def empty_caches(monkeypatch):
    # No cached problems, so backend tag-existence checks are no-ops here.
    _install_caches(monkeypatch)


class TestSplitPlatform:
    def test_exact_flag_selects_atcoder(self):
        assert split_platform(['+atcoder', '1200']) == ('ac', ['1200'])
        assert split_platform([]) == ('cf', [])
        assert split_platform(['+dp', '1200']) == ('cf', ['+dp', '1200'])

    @pytest.mark.parametrize('flag', ['+AtCoder', '+ATCODER'])
    def test_flag_is_case_insensitive(self, flag):
        assert split_platform([flag, '1200']) == ('ac', ['1200'])

    def test_rest_strips_only_the_flag(self):
        platform, rest = split_platform(['+atcoder', '+dp', '1200'])
        assert (platform, rest) == ('ac', ['+dp', '1200'])

    def test_repeated_flag_stripped(self):
        platform, rest = split_platform(['+atcoder', '1200', '+atcoder'])
        assert (platform, rest) == ('ac', ['1200'])

    def test_near_miss_is_an_ordinary_token(self):
        # ``+ac``/``~atcoder`` are not special; the tag check handles them.
        assert split_platform(['+ac', '~atcoder']) == (
            'cf', ['+ac', '~atcoder'])


class TestRatingSpecs:
    def test_default_passthrough(self):
        req = _cf([])
        assert (req.srating, req.erating, req.hidden, req.rating_given) == (
            1500, 1500, False, False)

    def test_single_and_range(self):
        assert _cf(['1200']).srating == 1200
        req = _cf(['1000-1400'])
        assert (req.srating, req.erating, req.hidden,
                req.rating_given) == (1000, 1400, True, True)

    def test_equal_range_still_hidden_but_given(self):
        req = _cf(['1200-1200'])
        assert (req.hidden, req.rating_given) == (True, True)

    def test_last_spec_wins(self):
        req = _cf(['1200', '1400-1500'])
        assert (req.srating, req.erating) == (1400, 1500)

    def test_empty_token_ignored(self):
        assert _cf(['']).srating == 1500

    def test_tags_skipped_for_rating(self):
        req = _cf(['+dp', '~math', '1300'])
        assert req.srating == 1300
        assert (req.tags, req.bantags) == (['dp'], ['math'])

    @pytest.mark.parametrize('arg', ['800-', '12x', '1-2-3'])
    def test_malformed_specs_name_the_token(self, arg):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf([arg])
        assert f'Invalid rating `{arg}`' in str(exc.value)

    @pytest.mark.parametrize('arg', ['1800-1200', '3600', '4000-5000',
                                     '700', '0'])
    def test_inverted_and_out_of_bounds_share_the_template(self, arg):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf([arg])
        msg = str(exc.value)
        assert f'Invalid rating `{arg}`' in msg
        assert '800-3500' in msg

    @pytest.mark.parametrize('args', [['800-', '1200'], ['12x', '1400'],
                                      ['700', '1500'], ['1800-1200', '1500']])
    def test_every_token_validated_not_just_the_last(self, args):
        with pytest.raises(CodeforcesCogError):
            _cf(args)

    def test_partial_overlap_accepted(self):
        assert _cf(['500-900']).srating == 500
        assert _cf(['0-1800']).erating == 1800

    def test_default_bypasses_bounds(self):
        req = _cf([], default_rating=700)
        assert (req.srating, req.erating) == (700, 700)

    def test_errors_carry_usage(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf(['3600'])
        assert GIMME_USAGE in str(exc.value)
        with pytest.raises(CodeforcesCogError) as exc:
            _cf(['3600'], usage=GITGUD_USAGE)
        assert GITGUD_USAGE in str(exc.value)


class TestUnexpectedArguments:
    @pytest.mark.parametrize('arg', ['abc', '-', '-100', '-trees', '+', '~',
                                     'structures', '+nope'])
    def test_bad_tokens_use_one_message(self, arg):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf([arg])
        msg = str(exc.value)
        assert 'Unexpected' in msg
        assert f'`{arg}`' in msg
        assert GIMME_USAGE in msg
        assert 'Did you mean' not in msg

    def test_multiple_offenders_listed_together(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf(['+a1', '+dp', '~b2'])
        msg = str(exc.value)
        assert 'Unexpected arguments' in msg
        assert '+a1' in msg and '~b2' in msg and '+dp' not in msg

    def test_known_bare_word_gets_no_suggestion(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _cf(['trees'])
        msg = str(exc.value)
        assert 'Unexpected argument `trees`' in msg
        assert 'Did you mean' not in msg

    def test_atcoder_unknown_tag(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _ac(['+dp'])
        assert '`+dp`' in str(exc.value)
        with pytest.raises(CodeforcesCogError) as exc:
            _ac(['abc'])
        assert '`abc`' in str(exc.value)

    def test_cold_vocabulary_keeps_tags(self):
        for vocabulary in (None, set()):
            req = _cf(['+anything'], vocabulary=vocabulary)
            assert req.tags == ['anything']


class TestDateWindows:
    def test_both_bounds(self):
        req = _cf(['d>=2020-01-01', 'd<2021-01-01'])
        assert 0 < req.dlo < req.dhi < 10 ** 10

    def test_lower_bound_only(self):
        req = _cf(['d>=2020-01-01'])
        assert req.dlo > 0 and req.dhi == 10 ** 10

    def test_gitgud_accepts_dates_too(self):
        req = _cf(['d<2020-01-01'])
        assert req.dhi < 10 ** 10 and req.dlo == 0

    def test_unknown_date_format_raises(self):
        with pytest.raises(ParamParseError):
            _cf(['d<not-a-date'])


class TestParseTagFilters:
    def test_tags_pass_through(self):
        tags, bantags = parse_tag_filters(
            ['+dp', '~math'], vocabulary=CF_VOCAB, exact=False,
            usage=GITGUD_USAGE)
        assert (tags, bantags) == (['dp'], ['math'])

    @pytest.mark.parametrize('arg', ['800', 'd<2020-01-01'])
    def test_ratings_and_dates_are_not_tags(self, arg):
        with pytest.raises(CodeforcesCogError, match='takes only'):
            parse_tag_filters([arg], vocabulary=CF_VOCAB, exact=False)

    def test_dash_token_gets_the_tag_only_hint(self):
        with pytest.raises(CodeforcesCogError) as exc:
            parse_tag_filters(['-100'], vocabulary=CF_VOCAB, exact=False)
        msg = str(exc.value)
        assert 'takes only' in msg and '0-0' not in msg

    def test_unknown_tag_named(self):
        with pytest.raises(CodeforcesCogError,
                           match=r'Unexpected argument `\+nope`'):
            parse_tag_filters(['+nope'], vocabulary=CF_VOCAB, exact=False,
                              usage=GITGUD_USAGE)

    def test_default_usage_is_progression(self):
        with pytest.raises(CodeforcesCogError) as exc:
            parse_tag_filters(['800'], vocabulary=CF_VOCAB, exact=False)
        assert 'gitgudprogression' in str(exc.value)

    def test_exact_matching_for_atcoder(self):
        tags, _ = parse_tag_filters(['+abc'], vocabulary=AC_VOCAB, exact=True)
        assert tags == ['abc']
        with pytest.raises(CodeforcesCogError):
            parse_tag_filters(['+ab'], vocabulary=AC_VOCAB, exact=True)

    def test_returned_lists_are_copies(self):
        tags, _ = parse_tag_filters(['+dp'], vocabulary=CF_VOCAB, exact=False,
                                    usage=GITGUD_USAGE)
        tags.append('mutated')
        again, _ = parse_tag_filters(['+dp'], vocabulary=CF_VOCAB, exact=False,
                                     usage=GITGUD_USAGE)
        assert again == ['dp']


class TestAcParseRequest:
    @pytest.mark.parametrize('args,want', [
        pytest.param(['0-1800'], (0, 1800, True), id='low-range-selects'),
        pytest.param(['50-999'], (50, 999, True), id='short-range-selects'),
        pytest.param(['0'], (0, 100, False), id='zero-widens-clamped'),
        pytest.param(['1200'], (1100, 1300, False), id='single-widens'),
        pytest.param(['1000-1400'], (1000, 1400, True), id='range'),
        pytest.param(['1000-5000'], (1000, 5000, True), id='overlap-accepted'),
    ])
    def test_request(self, args, want):
        req = _AcBackend().parse_request(args, 1500, GITGUD_USAGE)
        assert (req.srating, req.erating, req.hidden) == want

    @pytest.mark.parametrize('arg', ['800-', '5000'])
    def test_rejected(self, arg):
        with pytest.raises(CodeforcesCogError):
            _AcBackend().parse_request([arg], 1500, GITGUD_USAGE)


class TestCfParseRequest:
    @pytest.mark.parametrize('args,want', [
        pytest.param([], (1500, 1500, False), id='default'),
        pytest.param(['0-1800'], (0, 1800, True), id='low-range-selects'),
        pytest.param(['1200-1400'], (1200, 1400, True), id='range'),
        pytest.param(['500-900'], (500, 900, True), id='partial-overlap'),
    ])
    def test_request(self, args, want):
        req = _CfBackend().parse_request(args, 1500, GITGUD_USAGE)
        assert (req.srating, req.erating, req.hidden) == want

    @pytest.mark.parametrize('arg', ['0', '700', '799', '3600', '4000-5000',
                                     '800-'])
    def test_rejected(self, arg):
        with pytest.raises(CodeforcesCogError):
            _CfBackend().parse_request([arg], 1500, GITGUD_USAGE)

    def test_unquoted_multiword_tag_names_offender(self):
        with pytest.raises(CodeforcesCogError) as exc:
            _CfBackend().parse_request(['+data', 'structures'], 1500,
                                       GITGUD_USAGE)
        assert 'Unexpected argument `structures`' in str(exc.value)


class TestCfGimmeParsing:
    """Gimme reuses the shared parser and only selects from an
    already-parsed request, so the pool filter runs without cache fixtures."""

    def test_valid_args_reach_pool_filter(self):
        req = _cf(['+dp'])
        assert req.tags == ['dp']
        assert req.hidden is False
        assert _CfBackend().select(req, set(), set(), 'h') == []

    def test_date_window_passes_through(self):
        req = _cf(['+dp', 'd>=2020-01-01'])
        assert req.dlo > 0 and req.dhi == 10 ** 10
        assert _CfBackend().select(req, set(), set(), 'h') == []
