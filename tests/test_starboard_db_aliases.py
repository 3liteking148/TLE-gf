"""Tests for starboard DB methods: emoji aliases, emoji families, int/str
casting, entry lookups, multi-emoji delete isolation, and per-user defaults.

Companion to ``test_starboard_db.py`` (emoji config / messages / star counts).
Shares ``FakeUserDb`` and constants from ``tests/starboard_test_utils.py``.
"""
import pytest

from tests.starboard_test_utils import FakeUserDb, GUILD, GUILD_OTHER, STAR, FIRE, THUMBS_UP


@pytest.fixture
def db():
    d = FakeUserDb()
    yield d
    d.close()


# =====================================================================
# Emoji alias CRUD
# =====================================================================

class TestAliasAdd:
    def test_add_alias(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        aliases = db.get_aliases_for_emoji(GUILD, STAR)
        assert aliases == [THUMBS_UP]

    def test_add_multiple_aliases(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD, FIRE, STAR)
        aliases = db.get_aliases_for_emoji(GUILD, STAR)
        assert set(aliases) == {THUMBS_UP, FIRE}

    def test_replace_alias(self, db):
        """Adding same alias again should update the main emoji."""
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD, FIRE, 3, 0xff0000)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD, THUMBS_UP, FIRE)
        assert db.resolve_alias(GUILD, THUMBS_UP) == FIRE

    def test_int_guild_id(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        assert db.resolve_alias(GUILD, THUMBS_UP) == STAR


class TestAliasRemove:
    def test_remove_alias(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        rc = db.remove_starboard_alias(GUILD, THUMBS_UP)
        assert rc == 1
        assert db.resolve_alias(GUILD, THUMBS_UP) is None

    def test_remove_nonexistent(self, db):
        rc = db.remove_starboard_alias(GUILD, THUMBS_UP)
        assert rc == 0

    def test_remove_migrates_alias_reactors_to_main(self, db):
        """Removing an alias should migrate its reactor rows to the main emoji."""
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_message_v1('msg1', 'sb1', GUILD, STAR, author_id='a1')
        db.add_reactor('msg1', THUMBS_UP, 'user1')
        db.add_reactor('msg1', THUMBS_UP, 'user2')

        db.remove_starboard_alias(GUILD, THUMBS_UP)
        # Alias reactors should now be under the main emoji
        assert db.get_reactor_count('msg1', STAR) == 2
        assert db.get_reactor_count('msg1', THUMBS_UP) == 0

    def test_remove_migrates_without_duplicating(self, db):
        """If a user reacted with both main and alias, migration should not duplicate."""
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_message_v1('msg1', 'sb1', GUILD, STAR, author_id='a1')
        db.add_reactor('msg1', STAR, 'user1')
        db.add_reactor('msg1', THUMBS_UP, 'user1')  # Same user, both emojis

        db.remove_starboard_alias(GUILD, THUMBS_UP)
        # user1 should still be counted once under main emoji
        assert db.get_reactor_count('msg1', STAR) == 1
        assert db.get_reactor_count('msg1', THUMBS_UP) == 0

    def test_remove_migrates_across_multiple_messages(self, db):
        """Migration should work for alias reactors spread across multiple messages."""
        db.add_starboard_emoji(GUILD, STAR, 1, 0xffaa10)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_message_v1('msg1', 'sb1', GUILD, STAR, author_id='a1')
        db.add_starboard_message_v1('msg2', 'sb2', GUILD, STAR, author_id='a2')
        db.add_reactor('msg1', THUMBS_UP, 'user1')
        db.add_reactor('msg2', THUMBS_UP, 'user2')

        db.remove_starboard_alias(GUILD, THUMBS_UP)
        assert db.get_reactor_count('msg1', STAR) == 1
        assert db.get_reactor_count('msg2', STAR) == 1
        assert db.get_reactor_count('msg1', THUMBS_UP) == 0
        assert db.get_reactor_count('msg2', THUMBS_UP) == 0

    def test_remove_alias_scoped_to_guild(self, db):
        """Removing an alias in one guild must not affect another guild's reactors."""
        GUILD_B = 222222222222222222
        db.add_starboard_emoji(GUILD, STAR, 1, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, FIRE, 1, 0xff0000)
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD_B, THUMBS_UP, FIRE)
        # Guild A message + reactor
        db.add_starboard_message_v1('msg_a', 'sb_a', GUILD, STAR, author_id='u1')
        db.add_reactor('msg_a', THUMBS_UP, 'user1')
        # Guild B message + reactor
        db.add_starboard_message_v1('msg_b', 'sb_b', GUILD_B, FIRE, author_id='u2')
        db.add_reactor('msg_b', THUMBS_UP, 'user2')

        # Remove alias only in Guild A
        db.remove_starboard_alias(GUILD, THUMBS_UP)

        # Guild A: migrated to STAR
        assert db.get_reactor_count('msg_a', STAR) == 1
        assert db.get_reactor_count('msg_a', THUMBS_UP) == 0
        # Guild B: untouched — still under THUMBS_UP
        assert db.get_reactor_count('msg_b', THUMBS_UP) == 1
        assert db.get_reactor_count('msg_b', FIRE) == 0  # NOT migrated


class TestAliasResolve:
    def test_resolve_existing_alias(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        assert db.resolve_alias(GUILD, THUMBS_UP) == STAR

    def test_resolve_non_alias(self, db):
        assert db.resolve_alias(GUILD, STAR) is None

    def test_per_guild_isolation(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        assert db.resolve_alias(222222, THUMBS_UP) is None


class TestGetAllAliases:
    def test_empty(self, db):
        assert db.get_all_aliases_for_guild(GUILD) == []

    def test_multiple_aliases(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD, FIRE, STAR)
        rows = db.get_all_aliases_for_guild(GUILD)
        aliases = {r.alias_emoji: r.main_emoji for r in rows}
        assert aliases == {THUMBS_UP: STAR, FIRE: STAR}


class TestEmojiFamily:
    def test_no_aliases(self, db):
        assert db.get_emoji_family(GUILD, STAR) == [STAR]

    def test_with_aliases(self, db):
        db.add_starboard_alias(GUILD, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD, FIRE, STAR)
        family = db.get_emoji_family(GUILD, STAR)
        assert family[0] == STAR
        assert set(family) == {STAR, THUMBS_UP, FIRE}


# =====================================================================
# Int vs str type handling
# =====================================================================

class TestIntStrCasting:
    """Verify that int IDs from Discord work correctly with TEXT columns."""

    def test_guild_id_as_int(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        # Query with int
        entry = db.get_starboard_entry(GUILD, STAR)
        assert entry is not None

    def test_message_ids_as_int(self, db):
        db.add_starboard_message_v1(12345, 67890, GUILD, STAR, author_id=11111)
        assert db.check_exists_starboard_message_v1(12345, STAR)
        rc = db.remove_starboard_message(starboard_msg_id=67890)
        assert rc == 1

    def test_channel_id_stored_as_str(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD, STAR, 999888777666)
        entry = db.get_starboard_entry(GUILD, STAR)
        assert entry.channel_id == '999888777666'
        assert isinstance(entry.channel_id, str)


# =====================================================================
# Starboard entries for message
# =====================================================================

class TestGetStarboardEntriesForMessage:
    def test_returns_all_emoji_entries(self, db):
        db.add_starboard_message_v1('msg1', 'sb_s', GUILD, STAR, author_id='u')
        db.add_starboard_message_v1('msg1', 'sb_f', GUILD, FIRE, author_id='u')

        entries = db.get_starboard_entries_for_message('msg1')
        emojis = {e.emoji for e in entries}
        assert emojis == {STAR, FIRE}

    def test_returns_empty_for_unknown(self, db):
        assert db.get_starboard_entries_for_message('unknown') == []


# =====================================================================
# Get all emoji configs for a guild (used by ;starboard show)
# =====================================================================

class TestGetStarboardEmojisForGuild:
    def test_empty(self, db):
        assert db.get_starboard_emojis_for_guild(GUILD) == []

    def test_returns_all_emojis(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD, FIRE, 5, 0xff0000)
        entries = db.get_starboard_emojis_for_guild(GUILD)
        emojis = {e.emoji for e in entries}
        assert emojis == {STAR, FIRE}

    def test_includes_threshold_and_color(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        entries = db.get_starboard_emojis_for_guild(GUILD)
        assert entries[0].threshold == 3
        assert entries[0].color == 0xffaa10

    def test_includes_channel_id(self, db):
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD, STAR, '123456')
        entries = db.get_starboard_emojis_for_guild(GUILD)
        assert entries[0].channel_id == '123456'

    def test_per_guild_isolation(self, db):
        GUILD_B = 222222222222222222
        db.add_starboard_emoji(GUILD, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_B, FIRE, 5, 0xff0000)
        entries = db.get_starboard_emojis_for_guild(GUILD)
        assert len(entries) == 1
        assert entries[0].emoji == STAR


# =====================================================================
# Delete emoji isolation — verify deleting one emoji doesn't affect others
# =====================================================================

PILL = '\N{PILL}'
CHOC = '\N{CHOCOLATE BAR}'


class TestDeleteEmojiIsolation:
    """Verify that deleting an emoji (with aliases) doesn't touch
    another emoji's messages, reactors, aliases, or config —
    even when the same original message has entries for both."""

    def _setup_two_emojis_same_message(self, db):
        """Set up: message 333 is on both the pill starboard and the star starboard.
        Pill has chocolate as an alias. Star has fire as an alias."""
        # Emoji configs
        db.add_starboard_emoji(GUILD, PILL, 3, 0xffaa10)
        db.set_starboard_channel(GUILD, PILL, '100')
        db.add_starboard_emoji(GUILD, STAR, 5, 0xff0000)
        db.set_starboard_channel(GUILD, STAR, '200')

        # Aliases
        db.add_starboard_alias(GUILD, CHOC, PILL)
        db.add_starboard_alias(GUILD, FIRE, STAR)

        # Same original message tracked under both emojis
        db.add_starboard_message_v1('333', 'sb_pill', GUILD, PILL, author_id='user1')
        db.update_starboard_star_count('333', PILL, 10)
        db.add_starboard_message_v1('333', 'sb_star', GUILD, STAR, author_id='user1')
        db.update_starboard_star_count('333', STAR, 20)

        # Reactors under pill, chocolate (alias), star, fire (alias)
        db.add_reactor('333', PILL, 'reactor1')
        db.add_reactor('333', PILL, 'reactor2')
        db.add_reactor('333', CHOC, 'reactor3')   # alias of pill
        db.add_reactor('333', STAR, 'reactor4')
        db.add_reactor('333', STAR, 'reactor5')
        db.add_reactor('333', FIRE, 'reactor6')   # alias of star

    def test_delete_pill_preserves_star_message(self, db):
        self._setup_two_emojis_same_message(db)
        db.remove_starboard_emoji(GUILD, PILL)

        # Star message must still exist
        msg = db.get_starboard_message_v1('333', STAR)
        assert msg is not None
        assert msg.star_count == 20
        assert msg.author_id == 'user1'

        # Pill message must be gone
        assert db.get_starboard_message_v1('333', PILL) is None

    def test_delete_pill_preserves_star_reactors(self, db):
        self._setup_two_emojis_same_message(db)
        db.remove_starboard_emoji(GUILD, PILL)

        # Star reactors untouched
        assert db.get_reactor_count('333', STAR) == 2
        star_reactors = db.get_reactors('333', STAR)
        assert 'reactor4' in star_reactors
        assert 'reactor5' in star_reactors

        # Fire (alias of star) reactors untouched
        assert db.get_reactor_count('333', FIRE) == 1
        assert 'reactor6' in db.get_reactors('333', FIRE)

        # Pill and chocolate reactors gone
        assert db.get_reactor_count('333', PILL) == 0
        assert db.get_reactor_count('333', CHOC) == 0

    def test_delete_pill_preserves_star_config(self, db):
        self._setup_two_emojis_same_message(db)
        db.remove_starboard_emoji(GUILD, PILL)

        # Star config still there
        entry = db.get_starboard_entry(GUILD, STAR)
        assert entry is not None
        assert entry.threshold == 5
        assert entry.channel_id == '200'

        # Pill config gone
        assert db.get_starboard_entry(GUILD, PILL) is None

    def test_delete_pill_preserves_star_aliases(self, db):
        self._setup_two_emojis_same_message(db)
        db.remove_starboard_emoji(GUILD, PILL)

        # Star's alias (fire) still registered
        assert db.resolve_alias(GUILD, FIRE) == STAR
        family = db.get_emoji_family(GUILD, STAR)
        assert FIRE in family

        # Pill's alias (chocolate) gone
        assert db.resolve_alias(GUILD, CHOC) is None

    def test_delete_pill_other_messages_untouched(self, db):
        """A different message tracked only under star must survive pill deletion."""
        self._setup_two_emojis_same_message(db)

        # Message 444 only on star starboard
        db.add_starboard_message_v1('444', 'sb_star2', GUILD, STAR, author_id='user2')
        db.update_starboard_star_count('444', STAR, 15)
        db.add_reactor('444', STAR, 'reactor7')
        db.add_reactor('444', FIRE, 'reactor8')

        db.remove_starboard_emoji(GUILD, PILL)

        msg = db.get_starboard_message_v1('444', STAR)
        assert msg is not None
        assert msg.star_count == 15
        assert db.get_reactor_count('444', STAR) == 1
        assert db.get_reactor_count('444', FIRE) == 1

    def test_delete_star_preserves_pill(self, db):
        """Reverse: delete star, pill must survive."""
        self._setup_two_emojis_same_message(db)
        db.remove_starboard_emoji(GUILD, STAR)

        # Pill message and reactors intact
        msg = db.get_starboard_message_v1('333', PILL)
        assert msg is not None
        assert msg.star_count == 10
        assert db.get_reactor_count('333', PILL) == 2
        assert db.get_reactor_count('333', CHOC) == 1

        # Pill config and alias intact
        assert db.get_starboard_entry(GUILD, PILL) is not None
        assert db.resolve_alias(GUILD, CHOC) == PILL

        # Star stuff gone
        assert db.get_starboard_message_v1('333', STAR) is None
        assert db.get_starboard_entry(GUILD, STAR) is None
        assert db.resolve_alias(GUILD, FIRE) is None


# =====================================================================
# Per-user default emoji
# =====================================================================

USER_A = 999111111111111111
USER_B = 999222222222222222


class TestUserStarboardDefault:
    def test_get_returns_none_when_unset(self, db):
        assert db.get_user_starboard_default(GUILD, USER_A) is None

    def test_set_and_get(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        assert db.get_user_starboard_default(GUILD, USER_A) == FIRE

    def test_set_overwrites(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        db.set_user_starboard_default(GUILD, USER_A, STAR)
        assert db.get_user_starboard_default(GUILD, USER_A) == STAR

    def test_clear_removes(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        rc = db.clear_user_starboard_default(GUILD, USER_A)
        assert rc == 1
        assert db.get_user_starboard_default(GUILD, USER_A) is None

    def test_clear_unset_returns_zero(self, db):
        assert db.clear_user_starboard_default(GUILD, USER_A) == 0

    def test_per_user_isolation(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        db.set_user_starboard_default(GUILD, USER_B, STAR)
        assert db.get_user_starboard_default(GUILD, USER_A) == FIRE
        assert db.get_user_starboard_default(GUILD, USER_B) == STAR

    def test_per_guild_isolation(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        db.set_user_starboard_default(GUILD_OTHER, USER_A, STAR)
        assert db.get_user_starboard_default(GUILD, USER_A) == FIRE
        assert db.get_user_starboard_default(GUILD_OTHER, USER_A) == STAR

    def test_int_ids_accepted(self, db):
        db.set_user_starboard_default(GUILD, USER_A, FIRE)
        # Reading with the same int IDs (vs strings) must round-trip.
        assert db.get_user_starboard_default(int(GUILD), int(USER_A)) == FIRE
