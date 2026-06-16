"""Tests for snowflake-based time filtering of starboard leaderboard queries
and the snowflake_to_unix_sql expression itself.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from tests.starboard_test_utils import FakeUserDb, GUILD_A, STAR, _make_snowflake
from tle.util.db.starboard_db import snowflake_to_unix_sql


@pytest.fixture
def db():
    d = FakeUserDb()
    yield d
    d.close()


class TestSnowflakeTimeFiltering:
    """Test that DB leaderboard queries correctly filter by snowflake timestamp."""

    def _setup_messages(self, db):
        """Add messages at known dates for filtering tests."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        # Jan 2024 message
        db.add_starboard_message_v1(
            _make_snowflake(2024, 1, 15), 'sb1', GUILD_A, STAR, author_id='user1')
        db.update_starboard_star_count(_make_snowflake(2024, 1, 15), STAR, 5)
        # June 2024 message
        db.add_starboard_message_v1(
            _make_snowflake(2024, 6, 15), 'sb2', GUILD_A, STAR, author_id='user1')
        db.update_starboard_star_count(_make_snowflake(2024, 6, 15), STAR, 3)
        # Dec 2024 message
        db.add_starboard_message_v1(
            _make_snowflake(2024, 12, 1), 'sb3', GUILD_A, STAR, author_id='user2')
        db.update_starboard_star_count(_make_snowflake(2024, 12, 1), STAR, 10)
        # Feb 2025 message
        db.add_starboard_message_v1(
            _make_snowflake(2025, 2, 10), 'sb4', GUILD_A, STAR, author_id='user2')
        db.update_starboard_star_count(_make_snowflake(2025, 2, 10), STAR, 7)

    def _setup_reactors(self, db):
        """Add reactors for star-givers filtering tests."""
        db.add_reactor(_make_snowflake(2024, 1, 15), STAR, 'reactor1')
        db.add_reactor(_make_snowflake(2024, 1, 15), STAR, 'reactor2')
        db.add_reactor(_make_snowflake(2025, 2, 10), STAR, 'reactor1')

    def _ts(self, year, month, day):
        """Get unix timestamp for a date."""
        return datetime(year, month, day, tzinfo=timezone.utc).timestamp()

    # --- get_starboard_leaderboard ---

    def test_leaderboard_no_filter(self, db):
        self._setup_messages(db)
        rows = db.get_starboard_leaderboard(GUILD_A, STAR)
        assert len(rows) == 2  # user1 and user2

    def test_leaderboard_dlo_filter(self, db):
        self._setup_messages(db)
        dlo = self._ts(2024, 7, 1)
        rows = db.get_starboard_leaderboard(GUILD_A, STAR, dlo=dlo)
        # Only Dec 2024 and Feb 2025 messages (user2 has both)
        assert len(rows) == 1
        assert rows[0].author_id == 'user2'
        assert rows[0].message_count == 2

    def test_leaderboard_dhi_filter(self, db):
        self._setup_messages(db)
        dhi = self._ts(2024, 7, 1)
        rows = db.get_starboard_leaderboard(GUILD_A, STAR, dhi=dhi)
        # Only Jan 2024 and June 2024 messages (user1 has both)
        assert len(rows) == 1
        assert rows[0].author_id == 'user1'

    def test_leaderboard_range_filter(self, db):
        self._setup_messages(db)
        dlo = self._ts(2024, 6, 1)
        dhi = self._ts(2024, 12, 31)
        rows = db.get_starboard_leaderboard(GUILD_A, STAR, dlo=dlo, dhi=dhi)
        # June 2024 (user1) and Dec 2024 (user2)
        assert len(rows) == 2

    def test_leaderboard_empty_range(self, db):
        self._setup_messages(db)
        dlo = self._ts(2023, 1, 1)
        dhi = self._ts(2023, 12, 31)
        rows = db.get_starboard_leaderboard(GUILD_A, STAR, dlo=dlo, dhi=dhi)
        assert len(rows) == 0

    # --- get_starboard_star_leaderboard ---

    def test_star_leaderboard_no_filter(self, db):
        self._setup_messages(db)
        rows = db.get_starboard_star_leaderboard(GUILD_A, STAR)
        assert len(rows) == 2

    def test_star_leaderboard_dlo_filter(self, db):
        self._setup_messages(db)
        dlo = self._ts(2024, 7, 1)
        rows = db.get_starboard_star_leaderboard(GUILD_A, STAR, dlo=dlo)
        assert len(rows) == 1
        assert rows[0].author_id == 'user2'
        assert rows[0].total_stars == 17  # 10 + 7

    def test_star_leaderboard_range(self, db):
        self._setup_messages(db)
        dlo = self._ts(2024, 1, 1)
        dhi = self._ts(2024, 7, 1)
        rows = db.get_starboard_star_leaderboard(GUILD_A, STAR, dlo=dlo, dhi=dhi)
        assert len(rows) == 1
        assert rows[0].author_id == 'user1'
        assert rows[0].total_stars == 8  # 5 + 3

    # --- get_top_starboard_messages ---

    def test_top_messages_no_filter(self, db):
        self._setup_messages(db)
        rows = db.get_top_starboard_messages(GUILD_A, STAR)
        assert len(rows) == 4

    def test_top_messages_dlo_filter(self, db):
        self._setup_messages(db)
        dlo = self._ts(2025, 1, 1)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dlo=dlo)
        assert len(rows) == 1
        assert rows[0].star_count == 7

    def test_top_messages_dhi_filter(self, db):
        self._setup_messages(db)
        dhi = self._ts(2024, 2, 1)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dhi=dhi)
        assert len(rows) == 1
        assert rows[0].star_count == 5

    def test_top_messages_range(self, db):
        self._setup_messages(db)
        dlo = self._ts(2024, 6, 1)
        dhi = self._ts(2025, 1, 1)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dlo=dlo, dhi=dhi)
        assert len(rows) == 2
        # Should be sorted by star_count DESC
        assert rows[0].star_count == 10
        assert rows[1].star_count == 3

    def test_top_messages_author_with_dlo(self, db):
        """Combining author_id + dlo must not swap SQL parameters."""
        self._setup_messages(db)
        dlo = self._ts(2024, 1, 1)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dlo=dlo, author_id='user2')
        assert len(rows) == 2
        assert all(r.author_id == 'user2' for r in rows)

    def test_top_messages_author_with_range(self, db):
        """Combining author_id + dlo + dhi must not swap SQL parameters."""
        self._setup_messages(db)
        dlo = self._ts(2024, 1, 1)
        dhi = self._ts(2025, 1, 1)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dlo=dlo, dhi=dhi,
                                             author_id='user2')
        # Only Dec 2024 message by user2 falls in range
        assert len(rows) == 1
        assert rows[0].author_id == 'user2'
        assert rows[0].star_count == 10

    def test_top_messages_author_no_time_filter(self, db):
        """author_id alone (no time filter) still works."""
        self._setup_messages(db)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, author_id='user1')
        assert len(rows) == 2
        assert all(r.author_id == 'user1' for r in rows)

    # --- get_star_givers_leaderboard ---

    def test_star_givers_no_filter(self, db):
        self._setup_messages(db)
        self._setup_reactors(db)
        rows = db.get_star_givers_leaderboard(GUILD_A, STAR)
        assert len(rows) == 2
        # reactor1 reacted on 2 messages, reactor2 on 1
        givers = {r.user_id: r.stars_given for r in rows}
        assert givers['reactor1'] == 2
        assert givers['reactor2'] == 1

    def test_star_givers_dlo_filter(self, db):
        self._setup_messages(db)
        self._setup_reactors(db)
        dlo = self._ts(2025, 1, 1)
        rows = db.get_star_givers_leaderboard(GUILD_A, STAR, dlo=dlo)
        # Only Feb 2025 message has reactor1
        assert len(rows) == 1
        assert rows[0].user_id == 'reactor1'
        assert rows[0].stars_given == 1

    def test_star_givers_dhi_filter(self, db):
        self._setup_messages(db)
        self._setup_reactors(db)
        dhi = self._ts(2024, 2, 1)
        rows = db.get_star_givers_leaderboard(GUILD_A, STAR, dhi=dhi)
        # Only Jan 2024 message has reactor1 and reactor2
        assert len(rows) == 2

    # --- Boundary / edge cases ---

    def test_dlo_zero_means_no_bound(self, db):
        """dlo=0 should not filter anything (same as no filter)."""
        self._setup_messages(db)
        rows_all = db.get_starboard_leaderboard(GUILD_A, STAR)
        rows_zero = db.get_starboard_leaderboard(GUILD_A, STAR, dlo=0)
        assert len(rows_all) == len(rows_zero)

    def test_dhi_sentinel_means_no_bound(self, db):
        """dhi=_NO_TIME_BOUND should not filter anything."""
        from tle.util.db.starboard_db import _NO_TIME_BOUND as DB_NO_TIME_BOUND
        self._setup_messages(db)
        rows_all = db.get_starboard_leaderboard(GUILD_A, STAR)
        rows_nobound = db.get_starboard_leaderboard(GUILD_A, STAR, dhi=DB_NO_TIME_BOUND)
        assert len(rows_all) == len(rows_nobound)

    def test_exact_boundary_dlo_inclusive(self, db):
        """dlo is inclusive (>=): a message at exactly dlo should be included."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        exact_ts = self._ts(2024, 6, 15)
        db.add_starboard_message_v1(
            _make_snowflake(2024, 6, 15), 'sb1', GUILD_A, STAR, author_id='user1')
        db.update_starboard_star_count(_make_snowflake(2024, 6, 15), STAR, 5)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dlo=exact_ts)
        assert len(rows) == 1

    def test_exact_boundary_dhi_exclusive(self, db):
        """dhi is exclusive (<): a message at exactly dhi should be excluded."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        exact_ts = self._ts(2024, 6, 15)
        db.add_starboard_message_v1(
            _make_snowflake(2024, 6, 15), 'sb1', GUILD_A, STAR, author_id='user1')
        db.update_starboard_star_count(_make_snowflake(2024, 6, 15), STAR, 5)
        rows = db.get_top_starboard_messages(GUILD_A, STAR, dhi=exact_ts)
        assert len(rows) == 0


class TestSnowflakeToUnixSql:
    """Verify the SQL expression correctly extracts timestamps from Discord snowflakes."""

    def test_known_snowflake(self):
        """Test with a real Discord snowflake ID."""
        conn = sqlite3.connect(':memory:')
        # Known snowflake: 1276961610195537991 -> 2024-08-24 17:49:32 UTC
        expr = snowflake_to_unix_sql('val')
        row = conn.execute(f'SELECT {expr} FROM (SELECT 1276961610195537991 AS val)').fetchone()
        ts = row[0]
        dt_obj = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt_obj.year == 2024
        assert dt_obj.month == 8
        assert dt_obj.day == 24

    def test_roundtrip_with_make_snowflake(self):
        """A snowflake created from a date should produce that same date back."""
        conn = sqlite3.connect(':memory:')
        sf = _make_snowflake(2025, 3, 1)
        expr = snowflake_to_unix_sql('val')
        row = conn.execute(f'SELECT {expr} FROM (SELECT ? AS val)', (sf,)).fetchone()
        ts = row[0]
        dt_obj = datetime.fromtimestamp(ts, tz=timezone.utc)
        assert dt_obj.year == 2025
        assert dt_obj.month == 3
        assert dt_obj.day == 1
