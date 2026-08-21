"""Gitgud / challenge DB methods — extracted from user_db_conn.py.

Owns the ``challenge`` and ``user_challenge`` tables. The ``Gitgud`` enum is
imported lazily from the composing module to avoid an import cycle.
"""
import logging

logger = logging.getLogger(__name__)


class ChallengeDbMixin:
    """Mixin providing gitgud / challenge DB methods."""

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
                "score"	INTEGER
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS "user_challenge" (
                "user_id"	TEXT,
                "active_challenge_id"	INTEGER,
                "issue_time"	REAL,
                "score"	INTEGER NOT NULL,
                "num_completed"	INTEGER NOT NULL,
                "num_skipped"	INTEGER NOT NULL,
                PRIMARY KEY("user_id")
            )
        ''')

    def new_challenge(self, user_id, issue_time, prob, delta, score=None,
                      platform='cf'):
        """Insert a challenge. ``rating_delta`` keeps its raw meaning
        (``problem rating - base``); ``score`` is the exact awarded points
        (possibly off-ladder for tag penalties). When ``score`` is omitted it
        defaults to the plain ladder value of ``delta``."""
        if score is None:
            from tle.cogs._codeforces_helpers import _calculateGitgudScoreForDelta
            score = _calculateGitgudScoreForDelta(delta)
        query1 = '''
            INSERT INTO challenge
            (user_id, issue_time, problem_name, contest_id, p_index, rating_delta, status, platform, score)
            VALUES
            (?, ?, ?, ?, ?, ?, 1, ?, ?)
        '''
        query2 = '''
            INSERT OR IGNORE INTO user_challenge (user_id, score, num_completed, num_skipped)
            VALUES (?, 0, 0, 0)
        '''
        query3 = '''
            UPDATE user_challenge SET active_challenge_id = ?, issue_time = ?
            WHERE user_id = ? AND active_challenge_id IS NULL
        '''
        cur = self.conn.cursor()
        # prob.contestId / prob.key normalize both platforms: the challenge
        # key is the problem *name* for Codeforces and the problem *id*
        # (e.g. abc383_a) for AtCoder.
        contest_id, problem_key = prob.contestId, prob.key
        cur.execute(query1, (user_id, issue_time, problem_key, contest_id,
                             prob.index, delta, platform, score))
        last_id, rc = cur.lastrowid, cur.rowcount
        if rc != 1:
            self.conn.rollback()
            return 0
        cur.execute(query2, (user_id,))
        cur.execute(query3, (last_id, issue_time, user_id))
        if cur.rowcount != 1:
            self.conn.rollback()
            return 0
        self.conn.commit()
        return 1

    def check_challenge(self, user_id):
        query1 = '''
            SELECT active_challenge_id, issue_time FROM user_challenge
            WHERE user_id = ?
        '''
        res = self.conn.execute(query1, (user_id,)).fetchone()
        if res is None: return None
        c_id, issue_time = res
        query2 = '''
            SELECT problem_name, contest_id, rating_delta, platform, p_index, score FROM challenge
            WHERE id = ?
        '''
        res = self.conn.execute(query2, (c_id,)).fetchone()
        if res is None: return None
        # (c_id, issue_time, problem_key, contest_id, rating_delta, platform,
        #  p_index, score) — p_index appended last so the positional contract of the
        # consumer unpacking stays stable as the schema evolved; score appended
        # after that (migration 1.56.0).
        return c_id, issue_time, res[0], res[1], res[2], res[3], res[4], res[5]

    def get_gudgitters_timerange(self, timestampStart, timestampEnd):
        query = '''
            SELECT user_id, score, issue_time FROM challenge WHERE finish_time >= ? AND finish_time <= ? ORDER BY user_id
        '''
        return self.conn.execute(query, (timestampStart,timestampEnd)).fetchall()

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
        return self.conn.execute(query, (user_id,)).fetchall()

    def get_nogud_problem_keys(self, user_id):
        from tle.util.db.user_db_conn import Gitgud
        query = ('SELECT problem_name '
                 'FROM challenge '
                 f'WHERE user_id = ? AND status = {Gitgud.NOGUD}')
        return {key for key, in self.conn.execute(query, (user_id,)).fetchall()}

    def gitlog(self, user_id):
        from tle.util.db.user_db_conn import Gitgud
        query = f'''
            SELECT issue_time, finish_time, problem_name, rating_delta, status, platform, score
            FROM challenge WHERE user_id = ? AND status != {Gitgud.FORCED_NOGUD}
            ORDER BY issue_time DESC
        '''
        return self.conn.execute(query, (user_id,)).fetchall()

    def complete_challenge(self, user_id, challenge_id, finish_time, score):
        from tle.util.db.user_db_conn import Gitgud
        query1 = f'''
            UPDATE challenge SET finish_time = ?, status = {Gitgud.GOTGUD}
            WHERE id = ? AND status = {Gitgud.GITGUD}
        '''
        query2 = '''
            UPDATE user_challenge SET score = score + ?, num_completed = num_completed + 1,
            active_challenge_id = NULL, issue_time = NULL
            WHERE user_id = ? AND active_challenge_id = ?
        '''
        rc = self.conn.execute(query1, (finish_time, challenge_id)).rowcount
        if rc != 1:
            self.conn.rollback()
            return 0
        rc = self.conn.execute(query2, (score, user_id, challenge_id)).rowcount
        if rc != 1:
            self.conn.rollback()
            return 0
        self.conn.commit()
        return 1

    def skip_challenge(self, user_id, challenge_id, status):
        from tle.util.db.user_db_conn import Gitgud
        query1 = '''
            UPDATE user_challenge SET active_challenge_id = NULL, issue_time = NULL
            WHERE user_id = ? AND active_challenge_id = ?
        '''
        query2 = f'''
            UPDATE challenge SET status = ? WHERE id = ? AND status = {Gitgud.GITGUD}
        '''
        rc = self.conn.execute(query1, (user_id, challenge_id)).rowcount
        if rc != 1:
            self.conn.rollback()
            return 0
        rc = self.conn.execute(query2, (status, challenge_id)).rowcount
        if rc != 1:
            self.conn.rollback()
            return 0
        self.conn.commit()
        return 1
