"""Gitgud / challenge DB methods — extracted from user_db_conn.py.

Owns the ``challenge`` and ``user_challenge`` tables. The ``Gitgud`` enum is
imported lazily from the composing module to avoid an import cycle.
"""
import logging
from typing import NamedTuple, Optional, Union

logger = logging.getLogger(__name__)


class ActiveChallenge(NamedTuple):
    """One active challenge row, as returned by ``list_active_challenges``.

    Field order matches the historical 9-tuple layout, so positional
    indexing and unpacking keep working for legacy call sites.
    """
    challenge_id: int
    issue_time: float
    problem_key: str
    contest_id: Union[str, int]
    rating_delta: int
    platform: str
    p_index: Optional[str]
    score: int
    batch_id: str


class ChallengeDbMixin:
    """Mixin providing gitgud / challenge DB methods."""

    def _begin_immediate(self):
        try:
            self.conn.execute('BEGIN IMMEDIATE')
            return True
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            try:
                self.conn.execute('BEGIN IMMEDIATE')
                return True
            except Exception:
                return False

    def _create_challenge_tables(self):
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS "challenge" (
                "id"	INTEGER PRIMARY KEY AUTOINCREMENT,
                "user_id"	TEXT NOT NULL,
                "issue_time"	REAL NOT NULL,
                "finish_time"	REAL,
                "problem_name"	TEXT NOT NULL,
                "contest_id"	INTEGER NOT NULL,
                "p_index"	INTEGER NOT NULL,
                "rating_delta"	INTEGER NOT NULL,
                "status"	INTEGER NOT NULL,
                "platform"	TEXT NOT NULL DEFAULT 'cf',
                "score"	INTEGER,
                "batch_id"	TEXT NOT NULL DEFAULT ''
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS "user_challenge" (
                "user_id"	TEXT,
                "issue_time"	REAL,
                "score"	INTEGER NOT NULL,
                "num_completed"	INTEGER NOT NULL,
                "num_skipped"	INTEGER NOT NULL,
                PRIMARY KEY("user_id")
            )
        ''')

    def new_challenge(self, user_id, issue_time, prob, delta, score=None,
                      platform='cf', batch_id=''):
        """Insert a single challenge. ``batch_id`` must be ``snowflake-<msg id>``
        from the invoking Discord message (non-empty). Empty ``batch_id`` bails
        (return 0) — callers must pass a hardcoded ``snowflake-...`` id."""
        if not batch_id:
            return 0
        batch_id = str(batch_id)
        if not batch_id:
            return 0
        items = [(prob, delta, score, platform)]
        return 1 if self.new_challenges(user_id, batch_id, issue_time, items) == 1 else 0

    def new_challenges(self, user_id, batch_id, issue_time, items):
        """Atomically insert N challenges sharing one batch_id.

        ``items`` is ``[(prob, delta, score_or_None, platform), ...]``. When
        ``score`` is ``None`` it defaults per platform. ``batch_id`` must be
        ``snowflake-<invoke message id>`` (non-empty). Returns N on success, 0
        if any active challenge exists (idle = COUNT==0) or batch_id empty.
        """
        from tle.util.db.user_db_conn import Gitgud
        from tle.cogs._gitgud_scoring import AC_SCORE_MODEL, CF_SCORE_MODEL
        if not batch_id:
            return 0
        batch_id = str(batch_id)
        if not batch_id:
            return 0
        user_id = str(user_id)
        if not items:
            return 0
        if not self._begin_immediate():
            return 0
        try:
            active_cnt = self.conn.execute(
                f'SELECT COUNT(*) FROM challenge WHERE user_id = ? AND status = {Gitgud.GITGUD}',
                (user_id,)).fetchone()[0]
            if active_cnt != 0:
                self.conn.rollback()
                return 0
            cur = self.conn.cursor()
            cur.execute(
                'INSERT OR IGNORE INTO user_challenge (user_id, score, num_completed, num_skipped) VALUES (?, 0, 0, 0)',
                (user_id,))
            first_id = None
            for prob, delta, score, platform in items:
                if score is None:
                    model = AC_SCORE_MODEL if platform == 'ac' else CF_SCORE_MODEL
                    score = model.raw_score(delta)
                contest_id, problem_key = prob.contestId, prob.key
                cur.execute(
                    'INSERT INTO challenge (user_id, issue_time, problem_name, contest_id, p_index, rating_delta, status, platform, score, batch_id) '
                    'VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)',
                    (user_id, issue_time, problem_key, contest_id, prob.index, delta, platform, score, batch_id))
                if cur.rowcount != 1:
                    raise RuntimeError('insert failed')
                if first_id is None:
                    first_id = cur.lastrowid
            # Set issue_time for the batch
            cur.execute(
                'UPDATE user_challenge SET issue_time = ? WHERE user_id = ?',
                (issue_time, user_id))
            if cur.rowcount != 1:
                cur.execute(
                    'SELECT issue_time FROM user_challenge WHERE user_id = ?', (user_id,))
                cur.execute(
                    'UPDATE user_challenge SET issue_time = ? WHERE user_id = ?',
                    (issue_time, user_id))
            self.conn.commit()
            return len(items)
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return 0

    def check_challenge(self, user_id):
        """Return single active for legacy callers; backed by active set.

        Returns None if no active (idle = COUNT==0). Same ``ActiveChallenge``
        object as ``list_active_challenges`` items.
        """
        actives = self.list_active_challenges(user_id)
        if not actives:
            return None
        return actives[0]

    def get_current_batch_id(self, user_id):
        """Derive current batch_id from challenge rows; '' if idle."""
        row = self.conn.execute(
            'SELECT DISTINCT batch_id FROM challenge WHERE user_id = ? AND status = 1',
            (str(user_id),)).fetchone()
        return row[0] if row else ''

    def count_active_challenges(self, user_id):
        from tle.util.db.user_db_conn import Gitgud
        row = self.conn.execute(
            f'SELECT COUNT(*) FROM challenge WHERE user_id = ? AND status = {Gitgud.GITGUD}',
            (str(user_id),)).fetchone()
        return int(row[0]) if row else 0

    def list_active_challenges(self, user_id):
        """All active challenges for user ordered by id (insertion order)."""
        from tle.util.db.user_db_conn import Gitgud
        rows = self.conn.execute(
            f'SELECT id, issue_time, problem_name, contest_id, rating_delta, platform, p_index, score, batch_id '
            f'FROM challenge WHERE user_id = ? AND status = {Gitgud.GITGUD} ORDER BY id',
            (str(user_id),)).fetchall()
        return [ActiveChallenge(r.id, r.issue_time, r.problem_name, r.contest_id,
                                r.rating_delta, r.platform, r.p_index, r.score, r.batch_id)
                for r in rows]

    def get_gudgitters_timerange(self, timestampStart, timestampEnd):
        query = '''
            SELECT user_id, score, issue_time FROM challenge WHERE finish_time >= ? AND finish_time <= ? ORDER BY user_id
        '''
        return self.conn.execute(query, (timestampStart, timestampEnd)).fetchall()

    def get_gudgitters(self):
        query = '''
            SELECT user_id, score FROM user_challenge
        '''
        return self.conn.execute(query).fetchall()

    def get_gudgitter_score(self, user_id):
        query = '''
            SELECT score FROM user_challenge WHERE user_id = ?
        '''
        row = self.conn.execute(query, (str(user_id),)).fetchone()
        return row[0] if row is not None else 0

    def get_gudgitters_timerange_for_user(self, user_id, timestamp_start, timestamp_end):
        query = '''
            SELECT score, issue_time
            FROM challenge
            WHERE user_id = ? AND finish_time >= ? AND finish_time <= ?
            ORDER BY issue_time
        '''
        return self.conn.execute(query, (str(user_id), timestamp_start, timestamp_end)).fetchall()

    def howgud(self, user_id):
        query = '''
            SELECT rating_delta FROM challenge WHERE user_id = ? AND finish_time IS NOT NULL
        '''
        return self.conn.execute(query, (str(user_id),)).fetchall()

    def get_nogud_problem_keys(self, user_id):
        from tle.util.db.user_db_conn import Gitgud
        query = ('SELECT problem_name '
                 'FROM challenge '
                 f'WHERE user_id = ? AND status = {Gitgud.NOGUD}')
        return {key for key, in self.conn.execute(query, (str(user_id),)).fetchall()}

    def gitlog(self, user_id):
        from tle.util.db.user_db_conn import Gitgud
        query = f'''
            SELECT issue_time, finish_time, problem_name, rating_delta, status, platform, score
            FROM challenge WHERE user_id = ? AND status != {Gitgud.FORCED_NOGUD}
            ORDER BY issue_time DESC
        '''
        return self.conn.execute(query, (str(user_id),)).fetchall()

    def resolve_challenges(self, user_id, *, completions=(), skips=()):
        """Unified completion/skip for one batch — write-lock, checks, then clean writes (atomic).

        ``completions``: ``[(challenge_id, finish_time, score), ...]``
        ``skips``: ``[(challenge_id, status), ...]`` where status is NOGUD/FORCED_NOGUD.
        All ids must belong to the same batch (``snowflake-...``) and batch-atomic
        requires *all* actives at once. Idle = COUNT==0. Only score aggregates.

        Returns number of rows resolved or 0 on failure (rolls back, no partial).
        """
        from tle.util.db.user_db_conn import Gitgud
        user_id = str(user_id)
        comps = list(completions)
        skip_list = list(skips)
        all_ids = [cid for cid, *_ in comps] + [cid for cid, *_ in skip_list]
        if not all_ids:
            return 0
        if len(set(all_ids)) != len(all_ids):
            return 0
        if not self._begin_immediate():
            return 0
        try:
            placeholders = ','.join('?' for _ in all_ids)
            rows = self.conn.execute(
                f'SELECT id, batch_id FROM challenge WHERE id IN ({placeholders}) AND user_id = ? AND status = {Gitgud.GITGUD}',
                (*all_ids, user_id)).fetchall()
            if len(rows) != len(all_ids):
                self.conn.rollback()
                return 0
            inferred = rows[0].batch_id
            if any(r.batch_id != inferred for r in rows):
                self.conn.rollback()
                return 0
            # Batch identity is just the distinct active batch_id; no user_challenge column
            active_batch_row = self.conn.execute(
                'SELECT DISTINCT batch_id FROM challenge WHERE user_id = ? AND status = 1',
                (user_id,)).fetchone()
            cur_batch = active_batch_row[0] if active_batch_row else ''
            if inferred != cur_batch:
                self.conn.rollback()
                return 0
            active_cnt = self.conn.execute(
                f'SELECT COUNT(*) FROM challenge WHERE user_id = ? AND status = {Gitgud.GITGUD}',
                (user_id,)).fetchone()[0]
            if len(all_ids) != int(active_cnt):
                self.conn.rollback()
                return 0
            cur = self.conn.cursor()
            total_score = 0
            for cid, finish_time, score in comps:
                rc = cur.execute(
                    f'UPDATE challenge SET finish_time = ?, score = ?, status = {Gitgud.GOTGUD} WHERE id = ? AND status = {Gitgud.GITGUD}',
                    (finish_time, score, cid)).rowcount
                if rc != 1:
                    raise RuntimeError('complete failed')
                total_score += int(score) if score is not None else 0
            for cid, status in skip_list:
                rc = cur.execute(
                    f'UPDATE challenge SET status = ? WHERE id = ? AND status = {Gitgud.GITGUD}',
                    (status, cid)).rowcount
                if rc != 1:
                    raise RuntimeError('skip failed')
            if comps:
                cur.execute(
                    'UPDATE user_challenge SET score = score + ? WHERE user_id = ?',
                    (total_score, user_id))
                if cur.rowcount != 1:
                    raise RuntimeError('aggregate failed')
            remaining = cur.execute(
                f'SELECT COUNT(*) FROM challenge WHERE user_id = ? AND status = {Gitgud.GITGUD}',
                (user_id,)).fetchone()[0]
            if int(remaining) == 0:
                cur.execute(
                    'UPDATE user_challenge SET issue_time = NULL WHERE user_id = ?',
                    (user_id,))
            self.conn.commit()
            return len(comps) + len(skip_list)
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return 0

    def complete_challenge(self, user_id, challenge_id, finish_time, score):
        from tle.util.db.user_db_conn import Gitgud  # noqa: F401
        return 1 if self.resolve_challenges(
            user_id, completions=[(challenge_id, finish_time, score)], skips=[]) == 1 else 0

    def skip_challenge(self, user_id, challenge_id, status):
        return 1 if self.resolve_challenges(
            user_id, completions=[], skips=[(challenge_id, status)]) == 1 else 0
