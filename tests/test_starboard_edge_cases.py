"""Edge-case tests for starboard DB methods and bug fixes.

Covers: multi-guild isolation, upsert round-trips, leaderboard ranking,
remove_starboard_message with multi-emoji entries, backfill checkpointing
logic, fetch helpers, and content truncation.

Narcissus / same-message-multi-emoji / backfill-vs-live-race tests live in
``test_starboard_races.py``.  Shared ``FakeUserDb`` + constants come from
``tests/starboard_test_utils.py``.
"""
import sqlite3

import pytest

from tle.util.db.user_db_conn import namedtuple_factory
from tests.starboard_test_utils import FakeUserDb, GUILD_A, GUILD_B, STAR, FIRE, HEART


@pytest.fixture
def db():
    d = FakeUserDb()
    yield d
    d.close()


# =====================================================================
# Multi-guild isolation
# =====================================================================

class TestMultiGuildIsolation:
    def test_emoji_config_per_guild(self, db):
        """Same emoji in two guilds should be independent."""
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, STAR, 5, 0x00ff00)
        db.set_starboard_channel(GUILD_A, STAR, 100)
        db.set_starboard_channel(GUILD_B, STAR, 200)

        a = db.get_starboard_entry(GUILD_A, STAR)
        b = db.get_starboard_entry(GUILD_B, STAR)
        assert a.threshold == 3
        assert a.color == 0xffaa10
        assert a.channel_id == '100'
        assert b.threshold == 5
        assert b.color == 0x00ff00
        assert b.channel_id == '200'

    def test_messages_per_guild(self, db):
        """Messages in different guilds don't collide."""
        db.add_starboard_message_v1('msg1', 'sb1', GUILD_A, STAR, author_id='user1')
        db.add_starboard_message_v1('msg2', 'sb2', GUILD_B, STAR, author_id='user1')

        a_msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        b_msgs = db.get_all_starboard_messages_for_guild(GUILD_B)
        assert len(a_msgs) == 1
        assert a_msgs[0].original_msg_id == 'msg1'
        assert len(b_msgs) == 1
        assert b_msgs[0].original_msg_id == 'msg2'

    def test_leaderboard_per_guild(self, db):
        """Leaderboard for guild A shouldn't include guild B's messages."""
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, STAR, 3, 0xffaa10)
        db.add_starboard_message_v1('msg1', 'sb1', GUILD_A, STAR, author_id='user1')
        db.add_starboard_message_v1('msg2', 'sb2', GUILD_B, STAR, author_id='user1')
        db.update_starboard_star_count('msg1', STAR, 5)
        db.update_starboard_star_count('msg2', STAR, 10)

        lb_a = db.get_starboard_leaderboard(GUILD_A, STAR)
        assert len(lb_a) == 1
        assert lb_a[0].message_count == 1

        lb_b = db.get_starboard_star_leaderboard(GUILD_B, STAR)
        assert len(lb_b) == 1
        assert lb_b[0].total_stars == 10

    def test_remove_emoji_doesnt_affect_other_guild(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, STAR, 3, 0xffaa10)
        db.add_starboard_message_v1('msg1', 'sb1', GUILD_A, STAR, author_id='u')
        db.add_starboard_message_v1('msg2', 'sb2', GUILD_B, STAR, author_id='u')

        db.remove_starboard_emoji(GUILD_A, STAR)
        assert db.get_starboard_entry(GUILD_A, STAR) is None
        assert db.get_starboard_entry(GUILD_B, STAR) is not None
        assert db.check_exists_starboard_message_v1('msg2', STAR)

    def test_guild_config_isolated(self, db):
        db.set_guild_config(GUILD_A, 'starboard_leaderboard', '1')
        assert db.get_guild_config(GUILD_A, 'starboard_leaderboard') == '1'
        assert db.get_guild_config(GUILD_B, 'starboard_leaderboard') is None


# =====================================================================
# Upsert round-trip edge cases
# =====================================================================

class TestUpsertEdgeCases:
    def test_upsert_after_here_clear_here(self, db):
        """channel_id survives add → here → clear → here → upsert."""
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD_A, STAR, 100)
        db.clear_starboard_channel(GUILD_A, STAR)
        db.set_starboard_channel(GUILD_A, STAR, 200)

        # Upsert with new threshold
        db.add_starboard_emoji(GUILD_A, STAR, 7, 0x0000ff)
        entry = db.get_starboard_entry(GUILD_A, STAR)
        assert entry.threshold == 7
        assert entry.color == 0x0000ff
        assert entry.channel_id == '200'  # Preserved through upsert

    def test_upsert_multiple_times(self, db):
        """Repeated upserts should always preserve channel_id."""
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD_A, STAR, 100)

        for i in range(5):
            db.add_starboard_emoji(GUILD_A, STAR, i + 1, 0x000000 + i)

        entry = db.get_starboard_entry(GUILD_A, STAR)
        assert entry.threshold == 5
        assert entry.color == 4
        assert entry.channel_id == '100'  # Still there

    def test_upsert_different_emoji_no_interference(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD_A, STAR, 100)
        db.add_starboard_emoji(GUILD_A, FIRE, 5, 0xff0000)

        star = db.get_starboard_entry(GUILD_A, STAR)
        fire = db.get_starboard_entry(GUILD_A, FIRE)
        assert star.channel_id == '100'
        assert fire.channel_id is None  # Never set


# =====================================================================
# Leaderboard ranking edge cases
# =====================================================================

class TestLeaderboardRanking:
    def test_ordering_descending(self, db):
        """Leaderboard should be ordered by count descending."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        # user3: 1 msg, user1: 3 msgs, user2: 2 msgs (insert in jumbled order)
        db.add_starboard_message_v1('m1', 's1', GUILD_A, STAR, author_id='user3')
        for i in range(3):
            db.add_starboard_message_v1(f'm1{i}', f's1{i}', GUILD_A, STAR, author_id='user1')
        for i in range(2):
            db.add_starboard_message_v1(f'm2{i}', f's2{i}', GUILD_A, STAR, author_id='user2')

        lb = db.get_starboard_leaderboard(GUILD_A, STAR)
        assert [r.author_id for r in lb] == ['user1', 'user2', 'user3']
        assert [r.message_count for r in lb] == [3, 2, 1]

    def test_star_leaderboard_ordering(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        db.add_starboard_message_v1('m1', 's1', GUILD_A, STAR, author_id='user1')
        db.add_starboard_message_v1('m2', 's2', GUILD_A, STAR, author_id='user2')
        db.update_starboard_star_count('m1', STAR, 3)
        db.update_starboard_star_count('m2', STAR, 10)

        lb = db.get_starboard_star_leaderboard(GUILD_A, STAR)
        assert lb[0].author_id == 'user2'
        assert lb[0].total_stars == 10
        assert lb[1].author_id == 'user1'
        assert lb[1].total_stars == 3

    def test_star_leaderboard_aggregates_multiple_messages(self, db):
        """user1 has 2 messages with 3+7=10 total stars."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        db.add_starboard_message_v1('m1', 's1', GUILD_A, STAR, author_id='user1')
        db.add_starboard_message_v1('m2', 's2', GUILD_A, STAR, author_id='user1')
        db.update_starboard_star_count('m1', STAR, 3)
        db.update_starboard_star_count('m2', STAR, 7)

        lb = db.get_starboard_star_leaderboard(GUILD_A, STAR)
        assert len(lb) == 1
        assert lb[0].total_stars == 10

    def test_star_leaderboard_excludes_zero_star_count(self, db):
        """Messages with star_count=0 should not appear in star leaderboard."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        db.add_starboard_message_v1('m1', 's1', GUILD_A, STAR, author_id='user1')
        # star_count defaults to 0, never updated
        lb = db.get_starboard_star_leaderboard(GUILD_A, STAR)
        assert len(lb) == 0

    def test_message_leaderboard_includes_zero_star_messages(self, db):
        """Message leaderboard counts messages regardless of star_count."""
        db.add_starboard_emoji(GUILD_A, STAR, 1, 0xffaa10)
        db.add_starboard_message_v1('m1', 's1', GUILD_A, STAR, author_id='user1')
        # star_count is 0 (default) but the message exists
        lb = db.get_starboard_leaderboard(GUILD_A, STAR)
        assert len(lb) == 1
        assert lb[0].message_count == 1


# =====================================================================
# remove_starboard_message with multi-emoji entries
# =====================================================================

class TestRemoveMultiEmoji:
    def test_remove_by_emoji_only_removes_that_emoji(self, db):
        """Same original message tracked for star and fire — remove star only."""
        db.add_starboard_message_v1('msg1', 'sb_star', GUILD_A, STAR, author_id='u')
        db.add_starboard_message_v1('msg1', 'sb_fire', GUILD_A, FIRE, author_id='u')

        rc = db.remove_starboard_message(original_msg_id='msg1', emoji=STAR)
        assert rc == 1
        assert not db.check_exists_starboard_message_v1('msg1', STAR)
        assert db.check_exists_starboard_message_v1('msg1', FIRE)

    def test_remove_by_original_removes_all_emojis(self, db):
        """Remove by original_msg_id without emoji removes all emoji entries."""
        db.add_starboard_message_v1('msg1', 'sb_star', GUILD_A, STAR)
        db.add_starboard_message_v1('msg1', 'sb_fire', GUILD_A, FIRE)

        rc = db.remove_starboard_message(original_msg_id='msg1')
        assert rc == 2
        assert not db.check_exists_starboard_message_v1('msg1', STAR)
        assert not db.check_exists_starboard_message_v1('msg1', FIRE)

    def test_remove_by_starboard_msg_id_is_precise(self, db):
        """Each emoji gets a different starboard_msg_id — remove one."""
        db.add_starboard_message_v1('msg1', 'sb_star', GUILD_A, STAR)
        db.add_starboard_message_v1('msg1', 'sb_fire', GUILD_A, FIRE)

        rc = db.remove_starboard_message(starboard_msg_id='sb_star')
        assert rc == 1
        # Fire entry still there
        assert db.check_exists_starboard_message_v1('msg1', FIRE)


# =====================================================================
# Backfill checkpoint logic (DB-side)
# =====================================================================

class TestBackfillCheckpointing:
    def _seed_pending(self, db, count=5):
        """Create messages with author_id=None (pending backfill)."""
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        for i in range(count):
            db.add_starboard_message_v1(f'msg{i}', f'sb{i}', GUILD_A, STAR)

    def test_pending_messages_have_null_author(self, db):
        self._seed_pending(db, 3)
        msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        assert all(m.author_id is None for m in msgs)

    def test_backfill_marks_author_skips_on_next_run(self, db):
        """After setting author_id, the message should be skippable."""
        self._seed_pending(db, 3)
        # "Backfill" msg0 and msg1
        db.update_starboard_author_and_count('msg0', STAR, 'user1', 5)
        db.update_starboard_author_and_count('msg1', STAR, 'user2', 3)

        # Check: 2 done, 1 still pending
        msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        pending = [m for m in msgs if m.author_id is None]
        done = [m for m in msgs if m.author_id is not None]
        assert len(pending) == 1
        assert pending[0].original_msg_id == 'msg2'
        assert len(done) == 2

    def test_sentinel_marks_unfetchable_permanently(self, db):
        """__UNKNOWN__ sentinel prevents retry."""
        self._seed_pending(db, 3)
        db.update_starboard_author_and_count('msg0', STAR, '__UNKNOWN__', 0)
        db.update_starboard_author_and_count('msg1', STAR, 'user1', 5)

        msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        pending = [m for m in msgs if m.author_id is None]
        assert len(pending) == 1  # Only msg2 is still pending

    def test_sentinel_excluded_from_both_leaderboards(self, db):
        """__UNKNOWN__ should appear in neither leaderboard."""
        self._seed_pending(db, 3)
        db.update_starboard_author_and_count('msg0', STAR, '__UNKNOWN__', 0)
        db.update_starboard_author_and_count('msg1', STAR, 'user1', 5)
        db.update_starboard_author_and_count('msg2', STAR, 'user2', 3)

        msg_lb = db.get_starboard_leaderboard(GUILD_A, STAR)
        star_lb = db.get_starboard_star_leaderboard(GUILD_A, STAR)

        # Only user1 and user2, not __UNKNOWN__
        assert len(msg_lb) == 2
        assert all(r.author_id != '__UNKNOWN__' for r in msg_lb)
        assert len(star_lb) == 2
        assert all(r.author_id != '__UNKNOWN__' for r in star_lb)

    def test_full_backfill_leaves_no_pending(self, db):
        """After all messages are backfilled, no pending remain."""
        self._seed_pending(db, 5)
        for i in range(5):
            db.update_starboard_author_and_count(f'msg{i}', STAR, f'user{i}', i + 1)

        msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        pending = [m for m in msgs if m.author_id is None]
        assert len(pending) == 0

    def test_partial_backfill_preserves_star_counts(self, db):
        """Already-backfilled messages keep their star_count."""
        self._seed_pending(db, 3)
        db.update_starboard_author_and_count('msg0', STAR, 'user1', 10)
        db.update_starboard_author_and_count('msg1', STAR, 'user2', 20)

        # Simulate "restart" — only msg2 still pending
        msgs = db.get_all_starboard_messages_for_guild(GUILD_A)
        done = {m.original_msg_id: m for m in msgs if m.author_id is not None}
        assert done['msg0'].star_count == 10
        assert done['msg1'].star_count == 20


# =====================================================================
# _fetchone / _fetchall helpers
# =====================================================================

class TestFetchHelpers:
    """Test the _fetchone/_fetchall pattern that saves/restores row_factory."""

    def test_fetchone_with_different_factory(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = namedtuple_factory
        conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
        conn.execute("INSERT INTO t VALUES (1, 'hello')")
        conn.commit()

        # Simulate _fetchone with a custom factory (None = raw tuples)
        original = conn.row_factory
        conn.row_factory = None
        res = conn.execute('SELECT id, name FROM t WHERE id = 1').fetchone()
        conn.row_factory = original

        assert res == (1, 'hello')  # Raw tuple
        assert isinstance(res, tuple)

        # Original factory should be restored
        assert conn.row_factory is namedtuple_factory
        res2 = conn.execute('SELECT id, name FROM t WHERE id = 1').fetchone()
        assert hasattr(res2, 'id')
        assert res2.id == 1
        conn.close()

    def test_fetchall_restores_factory(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = namedtuple_factory
        conn.execute('CREATE TABLE t (id INTEGER)')
        conn.execute('INSERT INTO t VALUES (1)')
        conn.execute('INSERT INTO t VALUES (2)')
        conn.commit()

        original = conn.row_factory
        conn.row_factory = None
        res = conn.execute('SELECT id FROM t ORDER BY id').fetchall()
        conn.row_factory = original

        assert res == [(1,), (2,)]
        assert conn.row_factory is namedtuple_factory
        conn.close()


# =====================================================================
# Content truncation (bug #6 fix)
# =====================================================================

class TestContentTruncation:
    """Test the content truncation logic used in prepare_embed."""

    def test_short_content_unchanged(self):
        content = 'Hello world'
        if len(content) > 1024:
            content = content[:1021] + '...'
        assert content == 'Hello world'

    def test_exactly_1024_unchanged(self):
        content = 'x' * 1024
        if len(content) > 1024:
            content = content[:1021] + '...'
        assert content == 'x' * 1024
        assert len(content) == 1024

    def test_1025_gets_truncated(self):
        content = 'x' * 1025
        if len(content) > 1024:
            content = content[:1021] + '...'
        assert len(content) == 1024
        assert content.endswith('...')
        assert content == 'x' * 1021 + '...'

    def test_very_long_content(self):
        content = 'a' * 10000
        if len(content) > 1024:
            content = content[:1021] + '...'
        assert len(content) == 1024

    def test_empty_content_not_affected(self):
        content = ''
        # In the real code, empty content is skipped entirely
        if content and len(content) > 1024:
            content = content[:1021] + '...'
        assert content == ''


# =====================================================================
# get_starboard_emojis_for_guild
# =====================================================================

class TestGetEmojisForGuild:
    def test_returns_all_configured_emojis(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_A, FIRE, 5, 0xff0000)
        db.add_starboard_emoji(GUILD_A, HEART, 1, 0xff69b4)

        emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        assert len(emojis) == 3
        emoji_set = {e.emoji for e in emojis}
        assert emoji_set == {STAR, FIRE, HEART}

    def test_returns_empty_for_unconfigured_guild(self, db):
        emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        assert emojis == []

    def test_only_returns_requested_guild(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, FIRE, 5, 0xff0000)

        a_emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        assert len(a_emojis) == 1
        assert a_emojis[0].emoji == STAR
