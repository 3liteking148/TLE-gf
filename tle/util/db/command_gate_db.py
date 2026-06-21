"""Per-channel command-gate DB methods.

Owns the ``command_gate`` table, which records channels where bot commands are
disabled (``;disallow``). A row's ``thread_id`` is the one thread inside that
channel where commands stay allowed (``;disallow thread``), or NULL when the
whole channel is gated. The table is keyed on the parent text channel; the cog
maps a command run inside a thread back to its parent before looking it up.

Discord IDs are stored as TEXT (see the project's SQLite conventions).
"""


class CommandGateDbMixin:
    """Mixin providing per-channel command-gate DB methods."""

    def _create_command_gate_tables(self):
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS command_gate (
                guild_id   TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_id  TEXT,
                PRIMARY KEY (guild_id, channel_id)
            )
        ''')

    def set_command_gate(self, guild_id, channel_id, thread_id=None):
        """Disable bot commands in a channel. ``thread_id`` is the lone thread
        where they stay allowed, or None to gate the whole channel. Upserts, so
        re-running ``;disallow`` just rewrites the thread exception."""
        self.conn.execute(
            'INSERT OR REPLACE INTO command_gate '
            '(guild_id, channel_id, thread_id) VALUES (?, ?, ?)',
            (str(guild_id), str(channel_id),
             None if thread_id is None else str(thread_id)))
        self.conn.commit()

    def get_command_gate(self, guild_id, channel_id):
        """Return the gate row (guild_id, channel_id, thread_id) for a channel,
        or None when commands are not gated there."""
        return self.conn.execute(
            'SELECT guild_id, channel_id, thread_id FROM command_gate '
            'WHERE guild_id = ? AND channel_id = ?',
            (str(guild_id), str(channel_id))
        ).fetchone()

    def clear_command_gate(self, guild_id, channel_id):
        """Re-enable bot commands in a channel (``;allow``). Returns True if a
        gate was actually removed."""
        cur = self.conn.execute(
            'DELETE FROM command_gate WHERE guild_id = ? AND channel_id = ?',
            (str(guild_id), str(channel_id)))
        self.conn.commit()
        return cur.rowcount > 0
