"""AtCoder handle DB methods — mirrors ``HandleDbMixin`` for the AtCoder platform.

Owns the ``user_atcoder_handle`` table. AtCoder handles live in a separate
table rather than a platform column on ``user_handle`` because that table's
primary key and unique ``(guild_id, handle)`` index are Codeforces-specific;
rebuilding them in SQLite would risk the cross-guild sync provenance
(``updated_at``/``synced_at``). Expects ``self.conn`` to be a sqlite3
connection and ``UniqueConstraintFailed`` to be importable from the
composing module.
"""
import logging
import time

logger = logging.getLogger(__name__)


class AtcoderHandleDbMixin:
    """Mixin providing AtCoder handle DB methods."""

    def _create_atcoder_handle_tables(self):
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS user_atcoder_handle ('
            'user_id     TEXT,'
            'guild_id    TEXT,'
            'handle      TEXT,'
            'active      INTEGER,'
            'updated_at  INTEGER,'
            'synced_at   INTEGER,'
            'PRIMARY KEY (user_id, guild_id)'
            ')'
        )
        self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS '
                          'ix_user_atcoder_handle_guild_handle '
                          'ON user_atcoder_handle (guild_id, handle)')

    def set_atcoder_handle(self, user_id, guild_id, handle, synced=False):
        from tle.util.db.user_db_conn import UniqueConstraintFailed
        query = ('SELECT user_id '
                 'FROM user_atcoder_handle '
                 'WHERE guild_id = ? AND UPPER(handle) = UPPER(?)')
        existing = self.conn.execute(query, (guild_id, handle)).fetchone()
        if existing and int(existing[0]) != user_id:
            raise UniqueConstraintFailed

        now = int(time.time())
        synced_at = now if synced else None
        query = ('INSERT OR REPLACE INTO user_atcoder_handle '
                 '(user_id, guild_id, handle, active, updated_at, synced_at) '
                 'VALUES (?, ?, ?, 1, ?, ?)')
        with self.conn:
            return self.conn.execute(
                query, (user_id, guild_id, handle, now, synced_at)).rowcount

    def get_atcoder_handle(self, user_id, guild_id):
        query = ('SELECT handle '
                 'FROM user_atcoder_handle '
                 'WHERE user_id = ? AND guild_id = ?')
        res = self.conn.execute(query, (user_id, guild_id)).fetchone()
        return res[0] if res else None

    def get_atcoder_handle_row(self, user_id, guild_id):
        """Return ``(handle, synced_at)`` for a user in a guild, else None."""
        query = ('SELECT handle, synced_at '
                 'FROM user_atcoder_handle '
                 'WHERE user_id = ? AND guild_id = ?')
        res = self.conn.execute(query, (user_id, guild_id)).fetchone()
        return (res[0], res[1]) if res else None

    def get_other_guild_atcoder_handle(self, user_id, exclude_guild_id):
        """Most recently set active AtCoder handle outside ``exclude_guild_id``.

        Legacy rows without ``updated_at`` sort last (NULLs are smallest in
        descending order), so a manual identify elsewhere still wins over them.
        """
        query = ('SELECT handle '
                 'FROM user_atcoder_handle '
                 'WHERE user_id = ? AND guild_id != ? AND active = 1 '
                 'ORDER BY updated_at DESC, guild_id ASC '
                 'LIMIT 1')
        res = self.conn.execute(query, (user_id, exclude_guild_id)).fetchone()
        return res[0] if res else None

    def get_atcoder_user_id(self, handle, guild_id):
        query = ('SELECT user_id '
                 'FROM user_atcoder_handle '
                 'WHERE UPPER(handle) = UPPER(?) AND guild_id = ?')
        res = self.conn.execute(query, (handle, guild_id)).fetchone()
        return int(res[0]) if res else None

    def remove_atcoder_handle(self, handle, guild_id):
        query = ('DELETE FROM user_atcoder_handle '
                 'WHERE UPPER(handle) = UPPER(?) AND guild_id = ?')
        with self.conn:
            return self.conn.execute(query, (handle, guild_id)).rowcount

    def get_atcoder_handles_for_guild(self, guild_id):
        query = ('SELECT user_id, handle '
                 'FROM user_atcoder_handle '
                 'WHERE guild_id = ? AND active = 1')
        res = self.conn.execute(query, (guild_id,)).fetchall()
        return [(int(user_id), handle) for user_id, handle in res]
