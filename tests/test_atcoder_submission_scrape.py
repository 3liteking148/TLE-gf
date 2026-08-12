"""AtCoder submission-link scraper tests — URL parsing, page parsing, fetch layer.

Real page snapshots in ``tests/fixtures/atcoder/`` are downloaded from
atcoder.jp. To regenerate them:

    curl -s -A "Mozilla/5.0" https://atcoder.jp/contests/abc470/submissions/78313913 > tests/fixtures/atcoder/submission_ac.html
    curl -s -A "Mozilla/5.0" https://atcoder.jp/contests/abc470/submissions/78314530 > tests/fixtures/atcoder/submission_wa.html
    curl -s -A "Mozilla/5.0" https://atcoder.jp/contests/abc470/submissions/999999999 > tests/fixtures/atcoder/submission_notfound.html

The harness stubs ``lxml``/``lxml.html`` with empty modules, so this module
pops the stubs before the parser's lazy import runs (same pattern as
``test_atcoder_handles.py``). The claim-flow tests live in
``test_atcoder_gitgud_flow.py``.
"""
import sys
from pathlib import Path

import pytest  # noqa: F401

for _mod in ('lxml', 'lxml.html'):
    sys.modules.pop(_mod, None)

from tests.atcoder_test_utils import _run, FakeSession  # noqa: E402

from tle.util import atcoder_api  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures' / 'atcoder'


def _fixture(name):
    return (FIXTURES / f'{name}.html').read_bytes()


# =====================================================================
# URL parsing
# =====================================================================

class TestParseSubmissionUrl:
    def test_plain(self):
        assert atcoder_api.parse_submission_url(
            'https://atcoder.jp/contests/abc470/submissions/78313913') \
            == ('abc470', '78313913')

    def test_lang_query(self):
        assert atcoder_api.parse_submission_url(
            'https://atcoder.jp/contests/abc470/submissions/78313913?lang=en') \
            == ('abc470', '78313913')

    def test_trailing_slash(self):
        assert atcoder_api.parse_submission_url(
            'https://atcoder.jp/contests/abc470/submissions/78313913/') \
            == ('abc470', '78313913')

    def test_http_and_www(self):
        assert atcoder_api.parse_submission_url(
            'http://www.atcoder.jp/contests/abc470/submissions/78313913') \
            == ('abc470', '78313913')

    def test_user_page_rejected(self):
        assert atcoder_api.parse_submission_url(
            'https://atcoder.jp/users/tourist') is None

    def test_task_page_rejected(self):
        assert atcoder_api.parse_submission_url(
            'https://atcoder.jp/contests/abc470/tasks/abc470_a') is None

    def test_foreign_host_rejected(self):
        assert atcoder_api.parse_submission_url(
            'https://example.com/contests/abc470/submissions/78313913') is None

    def test_garbage_rejected(self):
        assert atcoder_api.parse_submission_url('not a url') is None


# =====================================================================
# Page parser (real lxml against real page snapshots)
# =====================================================================

class TestParseSubmissionPage:
    def test_ac_page(self):
        sub = atcoder_api._parse_submission_page(_fixture('submission_ac'))
        assert sub == ('r00kie_23', 'abc470_d', 'AC')
        assert sub.is_ac

    def test_wa_page(self):
        sub = atcoder_api._parse_submission_page(_fixture('submission_wa'))
        assert sub == ('srjywrdnprkt', 'abc470_e', 'WA')
        assert not sub.is_ac

    def test_notfound_page(self):
        assert atcoder_api._parse_submission_page(
            _fixture('submission_notfound')) is None


# =====================================================================
# Fetch layer (fake aiohttp session)
# =====================================================================

class TestGetSubmission:
    URL = 'https://atcoder.jp/contests/abc470/submissions/78313913'

    def test_fetches_and_parses(self):
        session = FakeSession([(200, _fixture('submission_ac'))])
        sub = _run(atcoder_api.get_submission(self.URL, session=session))
        assert sub == ('r00kie_23', 'abc470_d', 'AC')

    def test_not_found_returns_none(self):
        session = FakeSession([(404, b'')])
        assert _run(atcoder_api.get_submission(self.URL, session=session)) is None

    def test_server_error_returns_none(self):
        session = FakeSession([(500, b'')])
        assert _run(atcoder_api.get_submission(self.URL, session=session)) is None
