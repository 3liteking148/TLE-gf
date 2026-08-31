"""DB-layer batch-aware gitgud tests — 1.60.0 batch_id scaffolding (snowflake).

Covers blocking for snowflake singleton and
non-empty batchA via user_challenge.current_batch_id (PK(user_id) => ≤1 batch)
and challenge.batch_id grouping, plus cross-batch inference rejection
in the unified resolve_challenges (inferred, no explicit batch_id param).

Simplified resolve_challenges is tested as: lock (BEGIN IMMEDIATE) →
safety checks (read-only) → clean writes (score only, num_* kept 0).
Batch-atomic: call must resolve *all* actives at once; partials are
future work (see test_challenge_relaxed.py.disabled). Every test has
an inline description comment + docstring.
"""
from types import SimpleNamespace

from tle.util.db.user_db_conn import Gitgud, UserDbConn


def _prob(name, contest=1000, idx='A'):
    # Helper: minimal GitgudProblem shape for new_challenge/new_challenges.
    return SimpleNamespace(name=name, contestId=contest, index=idx, key=name)


def _items(n=4, prefix='Q'):
    # Helper: n distinct problems with delta 0, score None (defaults via ladder).
    return [(_prob(f'{prefix}{i}'), 0, None, 'cf') for i in range(n)]


def _nums(db, uid):
    # Helper: read legacy counters; they must stay 0 (score is sole aggregate).
    row = db.conn.execute(
        'SELECT num_completed, num_skipped FROM user_challenge WHERE user_id=?', (uid,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


UID = 'U1'
OTHER = 'U2'
BATCH_A = '111111'
BATCH_B = '222222'
T = 1000.0


# ---------------------------------------------------------------------------
# Classic singletons (snowflake) — blocking and clear
# ---------------------------------------------------------------------------

class TestNullBlocks:
    # Description: a classic singleton (snowflake) blocks any second singleton or batch
    # until resolved; count/current_batch_id stay stable on rejected attempts.
    def test_null_single_blocks_second_null_and_batch(self):
        """A single snowflake challenge blocks a second singleton and a batch; DB unchanged."""
        db = UserDbConn(':memory:')
        assert db.new_challenge(UID, T, _prob('P1'), 0, batch_id='snowflake-100001') == 1
        assert db.count_active_challenges(UID) == 1
        batch = db.get_current_batch_id(UID)
        assert batch == 'snowflake-100001'
        row = db.list_active_challenges(UID)[0]
        assert row[8] == batch == 'snowflake-100001'
        assert db.new_challenge(UID, T + 1, _prob('P2'), 0, batch_id='snowflake-100002') == 0
        assert db.new_challenges(UID, BATCH_A, T + 2, _items()) == 0
        # count/current unchanged after blocked attempts
        assert db.count_active_challenges(UID) == 1
        assert db.get_current_batch_id(UID) == batch

    # Description: completing the sole singleton clears active count and
    # current_batch_id, allowing a fresh '' challenge.
    def test_null_clears_allows_new(self):
        """Completing a singleton clears the slot; a new snowflake can be issued."""
        db = UserDbConn(':memory:')
        assert db.new_challenge(UID, T, _prob('P1'), 0, batch_id='snowflake-100003') == 1
        cid = db.list_active_challenges(UID)[0][0]
        batch = db.get_current_batch_id(UID)
        assert batch == 'snowflake-100003'
        assert db.complete_challenge(UID, cid, T + 10, 5) == 1
        assert db.count_active_challenges(UID) == 0
        assert db.get_current_batch_id(UID) == ''
        # score only, legacy counters untouched
        assert _nums(db, UID) == (0, 0)
        assert db.new_challenge(UID, T + 20, _prob('P2'), 0, batch_id='snowflake-100004') == 1
        assert db.get_current_batch_id(UID) == 'snowflake-100004'
        assert db.get_current_batch_id(UID) == db.list_active_challenges(UID)[0][8]


# ---------------------------------------------------------------------------
# Batch blocking (batch_id=BATCH_A)
# ---------------------------------------------------------------------------

class TestNonNullBlocks:
    # Description: a batch sets count 4 and current_batch_id=BATCH_A; any
    # singleton or other batch_id insert is rejected atomically.
    def test_batch_four_blocks_null_and_other_batch(self):
        """new_challenges(4) sets current_batch_id; singleton and other batch blocked."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        assert db.count_active_challenges(UID) == 4
        assert db.get_current_batch_id(UID) == BATCH_A
        assert all(r[8] == BATCH_A for r in db.list_active_challenges(UID))
        assert db.new_challenge(UID, T + 1, _prob('P2'), 0, batch_id='snowflake-100005') == 0
        assert db.new_challenges(UID, BATCH_B, T + 2, _items()) == 0
        assert db.count_active_challenges(UID) == 4
        assert db.get_current_batch_id(UID) == BATCH_A

    # Description: strict default is batch-atomic; a single complete_challenge
    # or 3-of-4 resolve on a batch must fail and leave DB untouched.
    def test_partial_still_blocks_and_atomic_requires_all(self):
        """Strict mode: single or 3/4 resolve on a batch fails (atomic)."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        cid = db.list_active_challenges(UID)[0][0]
        # batch-atomic: single complete must fail when 4 active
        assert db.complete_challenge(UID, cid, T + 10, 5) == 0
        assert db.count_active_challenges(UID) == 4
        assert db.get_current_batch_id(UID) == BATCH_A
        assert db.new_challenges(UID, BATCH_B, T + 20, _items()) == 0
        # also partial resolve with 3 of 4 must fail
        cids = [r[0] for r in db.list_active_challenges(UID)]
        assert db.resolve_challenges(
            UID, completions=[(cids[0], T + 20, 5)], skips=[(cids[1], Gitgud.NOGUD), (cids[2], Gitgud.NOGUD)]
        ) == 0
        assert db.count_active_challenges(UID) == 4


# ---------------------------------------------------------------------------
# Unified resolve — strict (default) must clear only when all 4 resolved
# ---------------------------------------------------------------------------

class TestUnifiedResolveAndClear:
    # Description: strict resolve of 3/4 must fail; full 1 complete + 3 skips
    # must commit, clear current_batch_id, and allow a new batch.
    def test_resolve_mixed_complete_skip_in_batch(self):
        """Strict: 3/4 mixed fails; 1 complete + 3 skips succeeds and clears."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        actives = db.list_active_challenges(UID)
        cids = [r[0] for r in actives]
        # batch-atomic: must resolve all 4 at once
        assert db.resolve_challenges(
            UID, completions=[(cids[0], T + 20, 5)], skips=[(cids[1], Gitgud.NOGUD), (cids[2], Gitgud.NOGUD)]
        ) == 0
        assert db.count_active_challenges(UID) == 4
        # correct atomic resolve: 1 complete + 3 skips
        assert db.resolve_challenges(
            UID, completions=[(cids[0], T + 20, 5)], skips=[(cids[1], Gitgud.NOGUD), (cids[2], Gitgud.NOGUD), (cids[3], Gitgud.NOGUD)]
        ) == 4
        assert db.count_active_challenges(UID) == 0
        assert db.get_current_batch_id(UID) == ''
        assert _nums(db, UID) == (0, 0)  # legacy counters untouched
        # new batch now allowed
        assert db.new_challenges(UID, BATCH_B, T + 40, _items()) == 4

    # Description: strict full resolve aggregates score correctly; partial 3/4
    # still fails. Only score is touched.
    def test_resolve_score_aggregate_and_skip_counts(self):
        """Strict full 2 completes+2 skips aggregates score=20 and clears."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items(prefix='R')) == 4
        cids = [r[0] for r in db.list_active_challenges(UID)]
        # atomic requires all 4
        assert db.resolve_challenges(
            UID, completions=[(cids[0], T + 10, 8), (cids[1], T + 11, 12)], skips=[(cids[2], Gitgud.NOGUD)]
        ) == 0
        assert db.resolve_challenges(
            UID, completions=[(cids[0], T + 10, 8), (cids[1], T + 11, 12)], skips=[(cids[2], Gitgud.NOGUD), (cids[3], Gitgud.NOGUD)]
        ) == 4
        assert db.get_gudgitter_score(UID) == 20
        assert _nums(db, UID) == (0, 0)
        assert db.count_active_challenges(UID) == 0


# ---------------------------------------------------------------------------
# Cross-batch / invalid batch rejections — strict and idempotence
# ---------------------------------------------------------------------------

class TestCrossBatchInvalid:
    # Description: spoofed row with other batch_id creates cross-batch set;
    # resolve mixing them must fail with no partial commit.
    def test_mixed_batch_ids_rejected_no_partial_commit(self):
        """Cross-batch ids (BATCH_A+BATCH_B) rejected; both rows stay GITGUD."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        # spoof a second batch row directly (bypass gate)
        db.conn.execute(
            'INSERT INTO challenge (user_id, issue_time, problem_name, contest_id, p_index, rating_delta, status, platform, score, batch_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (UID, T, 'SPOOF', 9999, 'Z', 0, 1, 'cf', 5, BATCH_B),
        )
        db.conn.commit()
        assert db.count_active_challenges(UID) == 5
        cid_a = db.conn.execute('SELECT id FROM challenge WHERE problem_name="Q0"').fetchone()[0]
        spoof_id = db.conn.execute('SELECT id FROM challenge WHERE problem_name="SPOOF"').fetchone()[0]
        # distinct batch_ids => 0, no partial commit
        assert db.resolve_challenges(UID, completions=[(cid_a, T + 20, 5)], skips=[(spoof_id, Gitgud.NOGUD)]) == 0
        # both rows still active, current_batch unchanged
        assert db.count_active_challenges(UID) == 5
        assert db.get_current_batch_id(UID) == BATCH_A
        assert db.conn.execute('SELECT status FROM challenge WHERE id=?', (cid_a,)).fetchone()[0] == 1

    # Description: raw '' mixed with BATCH_A — distinct grouping — must be rejected.
    def test_mixed_null_and_str_rejected(self):
        """'' and BATCH_A distinct batch_ids in one call → rejected."""
        db = UserDbConn(':memory:')
        # raw '' + str distinct — batch identity now only in challenge
        db.conn.execute(
            'INSERT INTO challenge (user_id, issue_time, problem_name, contest_id, p_index, rating_delta, status, platform, score, batch_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (UID, T, 'A', 1, 'A', 0, 1, 'cf', 5, ''),
        )
        db.conn.execute(
            'INSERT INTO challenge (user_id, issue_time, problem_name, contest_id, p_index, rating_delta, status, platform, score, batch_id) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (UID, T, 'B', 1, 'B', 0, 1, 'cf', 5, BATCH_A),
        )
        db.conn.execute(
            'INSERT OR IGNORE INTO user_challenge (user_id, score, num_completed, num_skipped) VALUES (?,?,?,?)',
            (UID, 0, 0, 0),
        )
        db.conn.commit()
        cids = [r[0] for r in db.conn.execute('SELECT id FROM challenge WHERE user_id=? ORDER BY id', (UID,)).fetchall()]
        assert db.resolve_challenges(UID, completions=[(cids[0], T + 20, 5)], skips=[(cids[1], Gitgud.NOGUD)]) == 0

    # Description: tampered challenge batch_id no longer matches active set → rejected.
    def test_mismatched_current_batch_rejected(self):
        """challenge batch_id spoof mismatches inferred batch_id → 0."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        # tamper one challenge's batch_id to spoof — now batch identity lives only in challenge
        cid_spoof = db.list_active_challenges(UID)[0][0]
        db.conn.execute('UPDATE challenge SET batch_id=? WHERE id=?', ('spoof', cid_spoof))
        db.conn.commit()
        # single complete must fail (cross-batch + batch-atomic)
        assert db.complete_challenge(UID, cid_spoof, T + 20, 5) == 0
        assert db.count_active_challenges(UID) == 4


# ---------------------------------------------------------------------------
# Ordering and visibility helpers
# ---------------------------------------------------------------------------

class TestListCountOrdering:
    # Description: list_active_challenges is ordered by insertion id and
    # exposes the correct batch_id for every row; counts stay consistent.
    def test_active_ordered_by_id_and_batch_id_visible(self):
        """Actives ordered by id, all expose batch_id==BATCH_A."""
        db = UserDbConn(':memory:')
        assert db.new_challenges(UID, BATCH_A, T, _items()) == 4
        rows = db.list_active_challenges(UID)
        ids = [r[0] for r in rows]
        assert ids == sorted(ids)
        assert all(r[8] == BATCH_A for r in rows)
        assert db.count_active_challenges(UID) == len(rows)
        assert db.get_current_batch_id(UID) == BATCH_A

