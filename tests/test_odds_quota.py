"""Tests for The Odds API rate-limit (quota) reporting: the pure header
parser and the ``fetch_sports(with_quota=True)`` wiring that ``;bet check``
uses to show how many monthly requests are left."""
import asyncio

import pytest

from tle.util import odds_api
from tests.betting_test_utils import _FakeResp, _FakeSession


def _run(coro):
    return asyncio.run(coro)


_QUOTA_HEADERS = {
    'x-requests-remaining': '437',
    'x-requests-used': '63',
    'x-requests-last': '1',
}


class TestParseQuotaHeaders:
    def test_parses_plain_lowercase_headers(self):
        assert odds_api.parse_quota_headers(_QUOTA_HEADERS) == {
            'remaining': 437, 'used': 63, 'last': 1}

    def test_lookup_is_case_insensitive(self):
        # aiohttp preserves original header case; we must still find them.
        headers = {'X-Requests-Remaining': '500', 'X-Requests-Used': '0'}
        parsed = odds_api.parse_quota_headers(headers)
        assert parsed['remaining'] == 500
        assert parsed['used'] == 0
        assert parsed['last'] is None  # absent → None, not an error

    def test_float_valued_header_is_truncated_to_int(self):
        assert odds_api.parse_quota_headers(
            {'x-requests-remaining': '436.0'})['remaining'] == 436

    def test_non_numeric_and_missing_are_none(self):
        parsed = odds_api.parse_quota_headers({'x-requests-remaining': 'n/a'})
        assert parsed == {'remaining': None, 'used': None, 'last': None}

    def test_none_or_empty_headers(self):
        assert odds_api.parse_quota_headers(None) == {
            'remaining': None, 'used': None, 'last': None}
        assert odds_api.parse_quota_headers({}) == {
            'remaining': None, 'used': None, 'last': None}


class TestFetchSportsQuota:
    def test_with_quota_returns_sports_and_parsed_quota(self):
        session = _FakeSession(
            [{'key': odds_api.WORLD_CUP_SPORT_KEY, 'title': 'FIFA World Cup 2026'}],
            headers=_QUOTA_HEADERS)
        sports, quota = _run(odds_api.fetch_sports('KEY', session=session,
                                                   with_quota=True))
        assert sports[0]['key'] == odds_api.WORLD_CUP_SPORT_KEY
        assert quota == {'remaining': 437, 'used': 63, 'last': 1}
        url, params = session.calls[0]
        assert url.endswith('/sports')          # still the quota-free endpoint
        assert params == {'apiKey': 'KEY'}

    def test_with_quota_when_headers_absent(self):
        session = _FakeSession([], headers={})   # provider sent no quota headers
        sports, quota = _run(odds_api.fetch_sports('KEY', session=session,
                                                   with_quota=True))
        assert sports == []
        assert quota == {'remaining': None, 'used': None, 'last': None}

    def test_default_is_backward_compatible_bare_list(self):
        session = _FakeSession([{'key': 'x'}], headers=_QUOTA_HEADERS)
        result = _run(odds_api.fetch_sports('KEY', session=session))
        assert result == [{'key': 'x'}]          # not a tuple — old callers safe


class TestGetJsonHeaderCapture:
    def test_headers_captured_even_on_error_response(self):
        # Quota is still readable on a 429 (out-of-quota) — headers are pulled
        # before the status check raises.
        class _ErrSession:
            def get(self, url, params=None):
                return _FakeResp({}, status=429, text='quota exceeded',
                                 headers={'x-requests-remaining': '0'})

        sink = {}
        with pytest.raises(odds_api.OddsApiError):
            _run(odds_api._get_json(_ErrSession(), 'u', {}, headers_out=sink))
        assert odds_api.parse_quota_headers(sink)['remaining'] == 0
