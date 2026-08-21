"""Tests for upgrade 1.56.0 — explicit challenge scores and honest deltas.

The Jul 2026 tag-penalty change stored penalised scores as sentinel
``rating_delta`` values (``-10**9 - score``). The migration backfills the new
``score`` column from exactly what was stored (nobody's points move) and
rewrites cf-platform deltas to ``problem_rating - clamped_rounded_base``
using local cache sources, normalising legacy flat -200 tagged rows and
leaving anything that does not reproduce cleanly untouched.
"""
import sqlite3

import pytest

from tle.util.db import _challenge_upgrade_reconstruct as cur
from tle.util.db.user_db_conn import namedtuple_factory


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = namedtuple_factory
    yield conn
    conn.close()


def _make_challenge_db(db, *, with_score=False):
    score_col = ', score INTEGER' if with_score else ''
    db.execute(f'''
        CREATE TABLE challenge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            issue_time REAL NOT NULL,
            finish_time REAL,
            problem_name TEXT NOT NULL,
            contest_id INTEGER NOT NULL,
            p_index INTEGER NOT NULL,
            rating_delta INTEGER NOT NULL,
            status INTEGER NOT NULL,
            platform TEXT NOT NULL DEFAULT 'cf'{score_col}
        )
    ''')


def _insert(db, user_id='100', issue_time=1000.0, name='Problem', delta=0,
            platform='cf', score=None):
    has_score = 'score' in {
        row[1] for row in db.execute('PRAGMA table_info(challenge)')}
    if has_score:
        row_id = db.execute(
            'INSERT INTO challenge '
            '(user_id, issue_time, problem_name, contest_id, p_index, '
            ' rating_delta, status, platform, score) '
            "VALUES (?, ?, ?, 1, 'A', ?, 1, ?, ?)",
            (user_id, issue_time, name, delta, platform, score)).lastrowid
    else:
        row_id = db.execute(
            'INSERT INTO challenge '
            '(user_id, issue_time, problem_name, contest_id, p_index, '
            ' rating_delta, status, platform) '
            "VALUES (?, ?, ?, 1, 'A', ?, 1, ?)",
            (user_id, issue_time, name, delta, platform)).lastrowid
    db.commit()
    return row_id


class TestSentinelDecoding:
    def test_valid_sentinel_decodes(self):
        assert cur.decode_sentinel_score(-(10**9) - 12) == 12
        assert cur.decode_sentinel_score(-(10**9) - 1) == 1
        assert cur.decode_sentinel_score(-(10**9) - 23) == 23

    def test_real_deltas_do_not_decode(self):
        for delta in (-2600, -400, -200, 0, 300, 9000):
            assert cur.decode_sentinel_score(delta) is None

    def test_is_sentinel_threshold(self):
        assert cur.is_sentinel_delta(-(10**9) - 5)
        assert cur.is_sentinel_delta(-(10**8))
        assert not cur.is_sentinel_delta(-300)
        assert not cur.is_sentinel_delta(None)


class TestBaseComputation:
    """Must replicate issue-time math bit-for-bit."""

    def test_rounds_to_nearest_hundred(self):
        assert cur.clamp_base(1854) == 1900
        assert cur.clamp_base(1849) == 1800

    def test_bankers_rounding(self):
        assert cur.clamp_base(1850) == 1800
        assert cur.clamp_base(1950) == 2000

    def test_clamps(self):
        assert cur.clamp_base(800) == 1100     # unrated default pins low
        assert cur.clamp_base(3500) == 3000

    def test_rating_at_picks_last_change_before_issue(self):
        history = [(100, 1500), (200, 1600), (300, 1700)]
        assert cur.rating_at(history, 250) == 1600
        assert cur.rating_at(history, 100) == 1500
        assert cur.rating_at(history, 50) is None


class TestBackfillScores:
    def test_sentinel_rows_decode_exactly(self, db):
        _make_challenge_db(db, with_score=True)
        _insert(db, delta=-(10**9) - 12)
        warnings = []
        cur.backfill_scores(db, warnings.append)
        assert db.execute('SELECT score FROM challenge').fetchone()[0] == 12
        assert warnings == []

    def test_ladder_rows_get_ladder_scores(self, db):
        _make_challenge_db(db, with_score=True)
        _insert(db, delta=0)
        _insert(db, delta=-400)
        cur.backfill_scores(db, lambda msg: None)
        assert [row[0] for row in db.execute(
            'SELECT score FROM challenge ORDER BY id')] == [8, 1]

    def test_backfill_is_idempotent(self, db):
        _make_challenge_db(db, with_score=True)
        _insert(db, delta=-(10**9) - 7)
        cur.backfill_scores(db, lambda msg: None)
        cur.backfill_scores(db, lambda msg: None)
        assert db.execute('SELECT score FROM challenge').fetchone()[0] == 7


@pytest.fixture
def src():
    cache = sqlite3.connect(':memory:')
    cache.execute('CREATE TABLE problem (name TEXT, rating INTEGER)')
    cache.execute('''CREATE TABLE rating_change (
        handle TEXT, rating_update_time INTEGER, new_rating INTEGER)''')
    cache.execute("INSERT INTO problem VALUES ('Easy Problem', 1600)")
    cache.execute("INSERT INTO problem VALUES ('Hard Problem', 2500)")
    cache.execute("INSERT INTO rating_change VALUES ('h1', 100, 1700)")
    cache.execute("INSERT INTO rating_change VALUES ('h1', 500, 1800)")

    problems, histories, currents = {}, {}, {}
    for name, rating in cache.execute(
            'SELECT name, rating FROM problem'):
        problems[name] = rating
    for handle, t, r in cache.execute(
            'SELECT handle, rating_update_time, new_rating '
            'FROM rating_change ORDER BY handle, rating_update_time'):
        histories.setdefault(handle, []).append((t, r))
        currents[handle] = r
    cache.close()
    currents['h2'] = 2100   # known only from the cf_user_cache snapshot
    return {
        'problems': problems,
        'histories': histories,
        'currents': currents,
        'user_handles': {'100': 'h1', '200': 'h2', '300': 'ghost'},
    }


class TestReconstructDeltas:
    def test_sentinel_row_reconstructed_from_history(self, db, src):
        _make_challenge_db(db, with_score=True)
        # h1 became 1800 at t=500; issue at t=600 -> base 1800 -> 2500-1800.
        _insert(db, user_id='100', issue_time=600, name='Hard Problem',
                delta=-(10**9) - 12)
        warnings = []
        stats = cur.reconstruct_deltas(db, warnings.append, **src)
        assert db.execute('SELECT rating_delta FROM challenge').fetchone()[0] == 700
        assert stats['reconstructed'] == 1
        assert warnings == []

    def test_non_sentinel_rows_are_never_evaluated(self, db, src):
        _make_challenge_db(db, with_score=True)
        # Untagged and legacy-tagged honest rows stay byte-identical; no
        # API call, no lookup, no warning may fire for them.
        _insert(db, user_id='100', issue_time=200, name='Easy Problem',
                delta=-100)
        _insert(db, user_id='100', issue_time=600, name='Easy Problem',
                delta=-400)
        calls = []
        warnings = []
        cur.reconstruct_deltas(
            db, warnings.append,
            api_fetch=lambda handle: calls.append(handle) or [(500, 1800)],
            **src)
        assert calls == []
        assert warnings == []
        assert [row[0] for row in db.execute(
            'SELECT rating_delta FROM challenge ORDER BY id')] == [-100, -400]

    def test_missing_problem_or_handle_skips_with_warning(self, db, src):
        _make_challenge_db(db, with_score=True)
        _insert(db, user_id='100', issue_time=600, name='Ghost Problem',
                delta=-(10**9) - 3)
        _insert(db, user_id='999', issue_time=600, name='Easy Problem',
                delta=-(10**9) - 3)
        warnings = []
        stats = cur.reconstruct_deltas(db, warnings.append, **src)
        assert stats['skipped'] == 2
        assert len(warnings) == 2
        sentinel_rows = [row[0] for row in db.execute(
            'SELECT rating_delta FROM challenge')]
        assert all(cur.is_sentinel_delta(d) for d in sentinel_rows)

    def test_atcoder_rows_never_touched(self, db, src):
        _make_challenge_db(db, with_score=True)
        _insert(db, platform='ac', delta=-(10**9) - 5)
        warnings = []
        cur.reconstruct_deltas(db, warnings.append, **src)
        assert db.execute('SELECT rating_delta FROM challenge').fetchone()[0] == -(10**9) - 5
        assert warnings == []

    def test_current_snapshot_approximates_when_history_short(self, db, src):
        _make_challenge_db(db, with_score=True)
        # h2 has no local history; current snapshot says 2100 -> base 2100.
        # Synthetic score 17 is not derivable from the resulting unpenalised
        # ladder value (delta 400 -> 23), so the snapshot provenance surfaces
        # inside the cross-check warning.
        _insert(db, user_id='200', issue_time=600, name='Hard Problem',
                delta=-(10**9) - 17)
        warnings = []
        stats = cur.reconstruct_deltas(db, warnings.append, **src)
        assert db.execute('SELECT rating_delta FROM challenge').fetchone()[0] == 400
        assert stats['reconstructed'] == 1
        assert len(warnings) == 1
        assert 'not derivable' in warnings[0]
        assert 'snapshot' in warnings[0]

    def test_api_is_the_primary_source_with_local_fallback(self, db, src):
        _make_challenge_db(db, with_score=True)
        _insert(db, user_id='100', issue_time=600, name='Hard Problem',
                delta=-(10**9) - 12)          # answered by the API
        _insert(db, user_id='200', issue_time=600, name='Hard Problem',
                delta=-(10**9) - 23)          # answered by the API
        calls = []

        def fake_api(handle):
            calls.append(handle)
            if handle == 'h1':
                return None   # API failure -> falls back to local history
            return [(100, 1900), (550, 2200)]

        warnings = []
        cur.reconstruct_deltas(db, warnings.append, api_fetch=fake_api, **src)
        assert calls == ['h1', 'h2']
        deltas = {row[0]: row[1] for row in db.execute(
            'SELECT id, rating_delta FROM challenge')}
        # h1: API failed, local history base 1800 -> 2500-1800.
        assert 700 in deltas.values()
        # h2: API history base 2200 -> 2500-2200.
        assert 300 in deltas.values()

    def test_empty_api_history_proves_unrated_at_issue_time(self, db, src):
        _make_challenge_db(db, with_score=True)
        # Issued before any rated contest: live code used DEFAULT_RATING,
        # clamped to a 1100 base -> 2500-1100 = 1400.
        _insert(db, user_id='200', issue_time=50, name='Hard Problem',
                delta=-(10**9) - 17)
        warnings = []
        stats = cur.reconstruct_deltas(
            db, warnings.append, api_fetch=lambda handle: [], **src)
        assert db.execute(
            'SELECT rating_delta FROM challenge').fetchone()[0] == 1400
        assert stats['reconstructed'] == 1
        # Synthetic inconsistency: score 17 cannot arise from an
        # unpenalised 23 (max reachable is 12 at one tag) -> loud warning,
        # frozen score still authoritative.
        assert len(warnings) == 1
        assert 'not derivable' in warnings[0]


class TestUpgradeEndToEnd:
    def _seed_pre156_db(self, db):
        _make_challenge_db(db, with_score=False)  # has platform, no score
        db.execute('''CREATE TABLE user_handle (
            user_id TEXT, guild_id TEXT, handle TEXT,
            active INTEGER, updated_at INTEGER, synced_at INTEGER)''')
        db.execute("INSERT INTO user_handle (user_id, guild_id, handle, active, updated_at) "
                   "VALUES ('100', '1', 'h1', 1, 10)")
        db.commit()
        _insert(db, user_id='100', issue_time=600, name='Hard Problem',
                delta=-(10**9) - 12)
        _insert(db, user_id='100', issue_time=600, name='Easy Problem',
                delta=-400)

    def test_upgrade_adds_score_and_repairs_deltas(self, db, tmp_path, monkeypatch):
        self._seed_pre156_db(db)

        cache = sqlite3.connect(':memory:')
        cache.execute('CREATE TABLE problem (name TEXT, rating INTEGER)')
        cache.execute('''CREATE TABLE rating_change (
            handle TEXT, rating_update_time INTEGER, new_rating INTEGER)''')
        cache.execute("INSERT INTO problem VALUES ('Hard Problem', 2500)")
        cache.execute("INSERT INTO problem VALUES ('Easy Problem', 1600)")
        cache.execute("INSERT INTO rating_change VALUES ('h1', 500, 1800)")

        monkeypatch.setattr(cur, 'open_cache_db_readonly', lambda path: cache)
        # Keep the API-first resolution deterministic (offline) in tests.
        monkeypatch.setattr(cur, 'fetch_rating_history_api', lambda handle: None)
        log = tmp_path / 'migration_warnings.log'
        from tle.util.db._user_db_upgrades_part5 import upgrade_1_56_0
        upgrade_1_56_0(db, warning_log_path=str(log))

        columns = {row[1] for row in db.execute(
            'PRAGMA table_info(challenge)').fetchall()}
        assert 'score' in columns
        rows = {row[0]: (row[1], row[2]) for row in db.execute(
            'SELECT id, rating_delta, score FROM challenge')}
        # Sentinel row: exact score kept, honest delta reconstructed.
        assert rows[1] == (700, 12)
        # Non-sentinel rows are never recalculated — the legacy tagged row
        # keeps its stored geometry; its score is frozen at what was paid.
        assert rows[2] == (-400, 1)
        assert not log.exists()   # nothing unresolvable here

    def test_upgrade_writes_warning_log_for_unresolvable_rows(self, db, tmp_path, monkeypatch):
        _make_challenge_db(db, with_score=False)
        _insert(db, user_id='100', issue_time=600, name='Ghost Problem',
                delta=-(10**9) - 3)
        monkeypatch.setattr(cur, 'open_cache_db_readonly', lambda path: None)
        monkeypatch.setattr(cur, 'fetch_rating_history_api', lambda handle: None)
        log = tmp_path / 'migration_warnings.log'
        from tle.util.db._user_db_upgrades_part5 import upgrade_1_56_0
        upgrade_1_56_0(db, warning_log_path=str(log))
        content = log.read_text()
        assert 'Ghost Problem' in content
        # Row left untouched so howgud's filter can exclude it.
        assert db.execute('SELECT rating_delta FROM challenge').fetchone()[0] == -(10**9) - 3
