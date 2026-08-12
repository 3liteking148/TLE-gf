"""AtCoder gitgud tests — kenkoooo fetch layer, problem cache, migration
1.55.0, and platform-aware challenge DB methods.

The cog-flow tests (``;gitgud +atcoder`` -> ``;gotgud``) live in
``test_atcoder_gitgud_flow.py``; the shared fakes live in
``atcoder_test_utils.py``. The fetch-layer tests inject a fake aiohttp
session exactly like ``test_atcoder_handles.py``.
"""
import datetime
import sqlite3
from types import SimpleNamespace

import pytest  # noqa: F401

from tests.atcoder_test_utils import (  # noqa: F401
    _ac_problem, _json_resp, _run, FakeSession,
)
from tests.betting_test_utils import GUILD, USER_A, USER_B, db  # noqa: F401

from tle.util import atcoder_api
from tle.util.cache_system2._atcoder_problems import AtcoderProblemCache
from tle.util.db.user_db_conn import UserDbConn, namedtuple_factory
from tle.util.db.user_db_upgrades import registry


# =====================================================================
# Fetch layer (fake aiohttp session)
# =====================================================================

class TestParseRating:
    def test_plain(self):
        assert atcoder_api.parse_rating('3797') == 3797

    def test_provisional(self):
        assert atcoder_api.parse_rating('683 (Provisional)') == 683

    def test_unrated(self):
        assert atcoder_api.parse_rating('') is None
        assert atcoder_api.parse_rating(None) is None


class TestAtCoderProblem:
    def test_properties(self):
        p = _ac_problem('abc383_a', title='A. Insert 1')
        assert p.name == 'Insert 1'
        assert p.index == 'A'
        assert p.url == 'https://atcoder.jp/contests/abc383/tasks/abc383_a'
        assert p.has_difficulty()

    def test_multi_letter_prefix(self):
        p = atcoder_api.AtCoderProblem(
            'apg4b_cj', 'apg4b', 'EX13', 'EX13. 三人兄弟へのプレゼント', 0)
        assert p.name == '三人兄弟へのプレゼント'

    def test_dash_prefix_still_stripped(self):
        # Legacy datasets/titles used 'A - ' / 'AGC061 A - ' forms.
        p = atcoder_api.AtCoderProblem(
            'agc061_a', 'agc061', 'A', 'AGC061 A - Long Long Ago', 3000)
        assert p.name == 'Long Long Ago'

    def test_no_difficulty(self):
        p = atcoder_api.AtCoderProblem('abc383_a', 'abc383', 'a', 'A. X')
        assert not p.has_difficulty()


class TestGetProblems:
    def test_parses_list(self):
        payload = [
            {'id': 'abc383_a', 'contest_id': 'abc383', 'problem_index': 'a',
             'title': 'A - Insert 1'},
            {'id': 'abc383_b', 'contest_id': 'abc383', 'problem_index': 'b',
             'title': 'B - Minimize Abs 1'},
        ]
        session = FakeSession([_json_resp(payload)])
        problems = _run(atcoder_api.get_problems(session=session))
        assert [p.id for p in problems] == ['abc383_a', 'abc383_b']
        assert problems[0].name == 'Insert 1'

    def test_failure_returns_none(self):
        session = FakeSession([(500, b'')])
        assert _run(atcoder_api.get_problems(session=session)) is None


class TestGetProblemModels:
    def test_clips_and_skips_experimental(self):
        payload = {
            'abc383_a': {'difficulty': -500, 'is_experimental': False},
            'abc383_b': {'difficulty': 5000, 'is_experimental': False},
            'abc383_c': {'difficulty': 1200, 'is_experimental': True},
            'abc383_d': {'difficulty': 800},
            'abc383_e': {},
        }
        session = FakeSession([_json_resp(payload)])
        models = _run(atcoder_api.get_problem_models(session=session))
        assert models == {'abc383_a': 0, 'abc383_b': 4199, 'abc383_d': 800}


class TestGetContests:
    def test_parses_map(self):
        payload = [
            {'id': 'abc383', 'start_epoch_second': 1000, 'title': 'ABC 383'},
        ]
        session = FakeSession([_json_resp(payload)])
        contests = _run(atcoder_api.get_contests(session=session))
        assert contests['abc383'].start_epoch_second == 1000
        assert contests['abc383'].title == 'ABC 383'


class TestGetUserSubmissions:
    def _sub(self, sid, pid='abc383_a', result='AC'):
        return {'id': sid, 'epoch_second': sid, 'problem_id': pid,
                'result': result}

    def test_paginates_until_short_page(self):
        page1 = [self._sub(i) for i in range(500)]
        page2 = [self._sub(500 + i) for i in range(2)]
        session = FakeSession([_json_resp(page1), _json_resp(page2)])
        subs = _run(atcoder_api.get_user_submissions('tourist', session=session))
        assert len(subs) == 502
        assert len(session.requests) == 2
        assert 'from_second=500' in session.requests[1]

    def test_is_ac_property(self):
        assert atcoder_api.AtCoderSubmission(1, 'abc383_a', 'AC').is_ac
        assert not atcoder_api.AtCoderSubmission(1, 'abc383_a', 'WA').is_ac

    def test_stops_after_max_pages(self):
        full = [self._sub(i) for i in range(500)]
        session = FakeSession([_json_resp(full) for _ in range(3)])
        subs = _run(atcoder_api.get_user_submissions(
            'grinder', session=session, max_pages=2))
        assert len(subs) == 1000
        assert len(session.requests) == 2

    def test_first_page_failure_returns_none(self):
        session = FakeSession([(500, b'')])
        assert _run(atcoder_api.get_user_submissions(
            'tourist', session=session)) is None


# =====================================================================
# AtcoderProblemCache
# =====================================================================

class TestAtcoderProblemCache:
    def _datasets(self):
        problems = [
            atcoder_api.AtCoderProblem('abc383_a', 'abc383', 'a', 'A - P1'),
            atcoder_api.AtCoderProblem('abc383_b', 'abc383', 'b', 'B - P2'),
            atcoder_api.AtCoderProblem('ahc041_a', 'ahc041', 'a', 'A - Heur'),
            atcoder_api.AtCoderProblem('arc184_a', 'arc184', 'a', 'A - P3'),
            atcoder_api.AtCoderProblem('ghost_a', 'ghost', 'a', 'A - Ghost'),
        ]
        models = {'abc383_a': 1200, 'abc383_b': 1500, 'arc184_a': 2000}
        contests = {
            'abc383': atcoder_api.AtCoderContest('abc383', 1000, 'ABC 383'),
            'ahc041': atcoder_api.AtCoderContest('ahc041', 2000, 'AHC 041'),
            'arc184': atcoder_api.AtCoderContest('arc184', 3000, 'ARC 184'),
        }
        return problems, models, contests

    def test_filters_and_sorts(self):
        cache = AtcoderProblemCache()
        problems, models, contests = self._datasets()
        _run(cache._update(problems, models, contests))
        ids = [p.id for p in cache.problems]
        # ahc excluded, ghost has no model, sorted by contest start
        assert ids == ['abc383_a', 'abc383_b', 'arc184_a']
        assert cache.problem_by_id['abc383_a'].difficulty == 1200
        assert 'ghost_a' not in cache.problem_by_id


# =====================================================================
# Migration 1.55.0
# =====================================================================

class TestMigration1550:
    _OLD_SCHEMA = '''
        CREATE TABLE challenge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            issue_time REAL NOT NULL,
            finish_time REAL,
            problem_name TEXT NOT NULL,
            contest_id INTEGER NOT NULL,
            p_index INTEGER NOT NULL,
            rating_delta INTEGER NOT NULL,
            status INTEGER NOT NULL
        )
    '''

    def _old_db(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = namedtuple_factory
        conn.execute(self._OLD_SCHEMA)
        conn.execute(
            'INSERT INTO challenge (user_id, issue_time, problem_name, '
            'contest_id, p_index, rating_delta, status) '
            "VALUES ('100', 1, 'Old Problem', 1234, 'A', 0, 1)")
        conn.commit()
        return conn

    def _migrate(self, conn):
        registry.ensure_version_table(conn)
        registry.set_version(conn, '1.54.0')
        registry.run(conn)
        return conn

    def test_upgrade_adds_platform_only(self):
        conn = self._migrate(self._old_db())
        columns = {row[1] for row in conn.execute(
            'PRAGMA table_info(challenge)').fetchall()}
        assert 'platform' in columns
        # problem_name and p_index keep their legacy names and contents;
        # nothing is renamed or dropped.
        assert 'problem_name' in columns
        assert 'p_index' in columns
        assert 'problem_key' not in columns
        row = conn.execute(
            'SELECT problem_name, p_index, platform FROM challenge'
        ).fetchone()
        assert row == ('Old Problem', 'A', 'cf')
        assert registry.get_current_version(conn) == '1.55.0'
        registry.run(conn)  # idempotent
        conn.close()

    def test_fresh_db_schema_has_platform_and_legacy_columns(self, db):
        columns = {row[1] for row in db.conn.execute(
            'PRAGMA table_info(challenge)').fetchall()}
        assert 'platform' in columns
        # Fresh DBs use the same layout as migrated legacy DBs.
        assert 'problem_name' in columns
        assert 'p_index' in columns
        assert 'problem_key' not in columns

    def test_new_challenge_inserts_after_migration(self, tmp_path):
        # Regression: the upgrade leaves the legacy NOT NULL p_index column
        # behind, so an INSERT that omits it must not be how new_challenge
        # writes rows. A production DB that ran 1.55.0 has to keep issuing
        # challenges without IntegrityError.
        dbfile = str(tmp_path / 'user.db')
        conn = sqlite3.connect(dbfile)
        conn.row_factory = namedtuple_factory
        conn.execute(self._OLD_SCHEMA)
        conn.execute(
            'INSERT INTO challenge (user_id, issue_time, problem_name, '
            'contest_id, p_index, rating_delta, status) '
            "VALUES ('100', 1, 'Old Problem', 1234, 'A', 0, 1)")
        registry.ensure_version_table(conn)
        registry.set_version(conn, '1.54.0')
        conn.commit()
        conn.close()

        db = UserDbConn(dbfile)
        assert registry.get_current_version(db.conn) == '1.55.0'
        row = db.conn.execute(
            'SELECT problem_name, p_index, platform FROM challenge'
        ).fetchone()
        assert row == ('Old Problem', 'A', 'cf')

        prob = SimpleNamespace(name='New Problem', contestId=5678, index='C1',
                           key='New Problem')
        assert db.new_challenge('200', 2, prob, 0) == 1
        active = db.check_challenge('200')
        assert active[2] == 'New Problem'
        assert active[6] == 'C1'  # p_index is written on insert


# =====================================================================
# Platform-aware challenge DB methods
# =====================================================================

class TestChallengeDbPlatform:
    def test_new_challenge_atcoder(self, db):
        prob = _ac_problem('abc383_a')
        issue_time = int(datetime.datetime.now().timestamp())
        assert db.new_challenge(
            USER_A, issue_time, prob, 0, platform='ac') == 1
        active = db.check_challenge(USER_A)
        assert active[2] == 'abc383_a'  # problem_name holds the problem id
        assert active[3] == 'abc383'
        assert active[4] == 0           # rating_delta
        assert active[5] == 'ac'
        assert active[6] == 'A'         # p_index is the letter after the '_'

    def test_new_challenge_cf_default(self, db):
        prob = SimpleNamespace(name='CF Problem', contestId=1234, index='A',
                           key='CF Problem')
        assert db.new_challenge(
            USER_A, 1, prob, 0) == 1
        assert db.check_challenge(USER_A)[5] == 'cf'
        # The key is the problem name on Codeforces.
        assert db.check_challenge(USER_A)[2] == 'CF Problem'
        assert db.check_challenge(USER_A)[6] == 'A'  # p_index stays the index

    def test_get_nogud_problem_keys_mixes_platforms(self, db):
        from tle.util.db.user_db_conn import Gitgud
        ac = _ac_problem('abc383_a')
        cf_prob = SimpleNamespace(name='CF Problem', contestId=1234, index='A',
                              key='CF Problem')
        db.new_challenge(USER_A, 1, ac, 0, platform='ac')
        db.new_challenge(USER_B, 1, cf_prob, 0)
        ac_id = db.check_challenge(USER_A)[0]
        cf_id = db.check_challenge(USER_B)[0]
        db.skip_challenge(USER_A, ac_id, Gitgud.NOGUD)
        db.skip_challenge(USER_B, cf_id, Gitgud.NOGUD)
        assert db.get_nogud_problem_keys(USER_A) == {'abc383_a'}
        assert db.get_nogud_problem_keys(USER_B) == {'CF Problem'}

    def test_gitlog_includes_platform(self, db):
        ac = _ac_problem('abc383_a')
        db.new_challenge(USER_A, 1, ac, 0, platform='ac')
        rows = db.gitlog(USER_A)
        assert rows[0][5] == 'ac'
        assert rows[0][2] == 'abc383_a'
