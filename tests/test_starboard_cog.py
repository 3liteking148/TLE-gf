"""Cog-level tests for starboard: jump URL parsing, emoji resolution, the
default-emoji parameter surface, the ;starboard show command, and the
_is_old_format / _starboard_content / _looks_like_emoji helpers.

Shared fakes (FakeUserDb, message/attachment doubles) live in
``tests/starboard_test_utils.py``.  Sibling modules cover the rest:
build_starboard_message → ``test_starboard_build.py``,
arg parsing → ``test_starboard_args.py``,
snowflake filtering → ``test_starboard_snowflake.py``,
reaction threshold gate → ``test_starboard_reactions.py``.
"""
import inspect

import pytest

import discord

from tests.starboard_test_utils import FakeUserDb, GUILD_A, STAR, FIRE, THUMBS_UP
from tle.cogs._starboard_helpers import _parse_jump_url
from tle.cogs.starboard import (
    Starboard,
    _looks_like_emoji,
    _starboard_content,
)
from tle.constants import _DEFAULT_STAR


@pytest.fixture
def db():
    d = FakeUserDb()
    yield d
    d.close()


# =====================================================================
# Jump URL parsing for backfill optimization
# =====================================================================


class TestParseJumpUrl:
    def test_standard_discord_url(self):
        text = '[Original](https://discord.com/channels/111/222/333)'
        result = _parse_jump_url(text)
        assert result == (111, 222, 333)

    def test_discordapp_url(self):
        text = '[Original](https://discordapp.com/channels/111/222/333)'
        result = _parse_jump_url(text)
        assert result == (111, 222, 333)

    def test_real_snowflake_ids(self):
        text = '[Original](https://discord.com/channels/1273752315022540861/1274019679425265685/1276961610195537991)'
        result = _parse_jump_url(text)
        assert result == (1273752315022540861, 1274019679425265685, 1276961610195537991)

    def test_extracts_channel_id(self):
        """The channel_id (second element) is what the backfill needs."""
        text = '[Original](https://discord.com/channels/111/999888777/333)'
        result = _parse_jump_url(text)
        _, channel_id, _ = result
        assert channel_id == 999888777

    def test_no_url_returns_none(self):
        assert _parse_jump_url('no url here') is None

    def test_empty_string_returns_none(self):
        assert _parse_jump_url('') is None

    def test_partial_url_returns_none(self):
        assert _parse_jump_url('https://discord.com/channels/111/222') is None

    def test_wrong_domain_returns_none(self):
        assert _parse_jump_url('https://example.com/channels/111/222/333') is None

    def test_url_embedded_in_markdown(self):
        """The real embed field value has markdown link syntax."""
        text = '[Original](https://discord.com/channels/111/222/333)'
        result = _parse_jump_url(text)
        assert result == (111, 222, 333)

    def test_url_with_extra_text(self):
        text = 'Check this out: https://discord.com/channels/111/222/333 cool right?'
        result = _parse_jump_url(text)
        assert result == (111, 222, 333)


# =====================================================================
# get_starboard_emojis_for_guild now includes channel_id
# =====================================================================

class TestGetEmojisIncludesChannelId:
    def test_channel_id_returned(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD_A, STAR, 999888)

        emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        assert len(emojis) == 1
        assert emojis[0].channel_id == '999888'

    def test_channel_id_none_when_not_set(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)

        emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        assert len(emojis) == 1
        assert emojis[0].channel_id is None

    def test_multiple_emojis_different_channels(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_A, FIRE, 5, 0xff0000)
        db.set_starboard_channel(GUILD_A, STAR, 100)
        db.set_starboard_channel(GUILD_A, FIRE, 200)

        emojis = db.get_starboard_emojis_for_guild(GUILD_A)
        by_emoji = {e.emoji: e for e in emojis}
        assert by_emoji[STAR].channel_id == '100'
        assert by_emoji[FIRE].channel_id == '200'


# =====================================================================
# _is_old_format detection
# =====================================================================


class _FakeSbMsg:
    """Minimal starboard message mock for old-format detection."""
    def __init__(self, embeds):
        self.embeds = embeds


class TestIsOldFormat:
    def test_old_format_with_jump_to(self):
        embed = discord.Embed()
        embed.add_field(name='Channel', value='#general')
        embed.add_field(name='Jump to', value='[Original](https://...)')
        assert Starboard._is_old_format(_FakeSbMsg([embed])) is True

    def test_new_format_no_fields(self):
        embed = discord.Embed(description='Hello')
        assert Starboard._is_old_format(_FakeSbMsg([embed])) is False

    def test_new_format_with_attachment_field(self):
        embed = discord.Embed()
        embed.add_field(name='Attachment', value='[file.pdf](https://...)')
        assert Starboard._is_old_format(_FakeSbMsg([embed])) is False

    def test_empty_embeds(self):
        assert Starboard._is_old_format(_FakeSbMsg([])) is False


# =====================================================================
# _starboard_content helper
# =====================================================================


class TestStarboardContent:
    def test_format(self):
        url = 'https://discord.com/channels/1/2/3'
        result = _starboard_content('\N{WHITE MEDIUM STAR}', 5, url)
        assert '\N{WHITE MEDIUM STAR}' in result
        assert '**5**' in result
        assert url in result

    def test_pipe_separator(self):
        result = _starboard_content('\N{FIRE}', 3, 'https://discord.com/channels/1/2/3')
        assert '|' in result

    def test_no_channel_mention(self):
        """Should not use <#channel_id> which links to the channel, not the message."""
        result = _starboard_content('\N{WHITE MEDIUM STAR}', 5, 'https://discord.com/channels/1/2/3')
        assert '<#' not in result

    def test_jump_url_is_plain(self):
        """Jump URL should be plain text (Discord auto-links it), not markdown."""
        url = 'https://discord.com/channels/1/2/3'
        result = _starboard_content('\N{WHITE MEDIUM STAR}', 5, url)
        assert f'| {url}' in result


# =====================================================================
# Default emoji parameter on all starboard commands
# =====================================================================


def _unwrap(attr):
    """Get the original function from a stubbed command decorator."""
    while hasattr(attr, '__wrapped__'):
        attr = attr.__wrapped__
    return attr


class TestDefaultEmojiParameter:
    """All starboard commands should default the emoji parameter to the star emoji."""

    # Commands with an explicit emoji parameter (admin/config commands)
    _COMMANDS_WITH_EMOJI_PARAM = [
        'add', 'delete', 'edit_threshold', 'edit_color',
        'here', 'clear', 'remove',
    ]

    # Commands using *args + _parse_starboard_args (leaderboard/top commands)
    _COMMANDS_WITH_ARGS = [
        'leaderboard', 'star_leaderboard', 'star_givers', 'top',
    ]

    @pytest.mark.parametrize('method_name', _COMMANDS_WITH_EMOJI_PARAM)
    def test_emoji_defaults_to_star(self, method_name):
        method = _unwrap(getattr(Starboard, method_name))
        sig = inspect.signature(method)
        assert 'emoji' in sig.parameters, f'{method_name} missing emoji parameter'
        param = sig.parameters['emoji']
        assert param.default == _DEFAULT_STAR, (
            f'{method_name}: emoji default is {param.default!r}, expected {_DEFAULT_STAR!r}'
        )

    @pytest.mark.parametrize('method_name', _COMMANDS_WITH_ARGS)
    def test_args_commands_use_varargs(self, method_name):
        """Leaderboard/top commands use *args and parse emoji via _parse_starboard_args."""
        method = _unwrap(getattr(Starboard, method_name))
        sig = inspect.signature(method)
        assert 'args' in sig.parameters, f'{method_name} should accept *args'

    @pytest.mark.parametrize('method_name', _COMMANDS_WITH_ARGS)
    def test_args_commands_call_parse_starboard_args(self, method_name):
        """Every *args command must call _parse_starboard_args to extract emoji/dlo/dhi."""
        method = _unwrap(getattr(Starboard, method_name))
        source = inspect.getsource(method)
        assert '_parse_starboard_args(' in source, (
            f'{method_name} does not call _parse_starboard_args — '
            f'emoji/dlo/dhi will be undefined'
        )

    def test_edit_threshold_required_arg_before_emoji(self):
        """threshold should come before the optional emoji."""
        sig = inspect.signature(_unwrap(Starboard.edit_threshold))
        params = list(sig.parameters.keys())
        assert params.index('threshold') < params.index('emoji')

    def test_edit_color_required_arg_before_emoji(self):
        """color should come before the optional emoji."""
        sig = inspect.signature(_unwrap(Starboard.edit_color))
        params = list(sig.parameters.keys())
        assert params.index('color') < params.index('emoji')

    def test_remove_required_arg_before_emoji(self):
        """original_message_id should come before the optional emoji."""
        sig = inspect.signature(_unwrap(Starboard.remove))
        params = list(sig.parameters.keys())
        assert params.index('original_message_id') < params.index('emoji')


# =====================================================================
# _resolve_emoji
# =====================================================================


class _FakeUserDbForResolve:
    """Minimal stub providing get_starboard_entry and resolve_alias for _resolve_emoji tests."""
    def __init__(self, entries=None, aliases=None):
        self._entries = entries or {}
        self._aliases = aliases or {}

    def get_starboard_entry(self, guild_id, emoji):
        return self._entries.get((str(guild_id), emoji))

    def resolve_alias(self, guild_id, emoji):
        return self._aliases.get((str(guild_id), emoji))


class _FakeEntry:
    def __init__(self, channel_id='123', threshold=3, color=0xffaa10):
        self.channel_id = channel_id
        self.threshold = threshold
        self.color = color


class TestResolveEmoji:
    def _patch_db(self, monkeypatch, entries=None, aliases=None):
        from tle.util import codeforces_common as cf
        fake = _FakeUserDbForResolve(entries, aliases)
        monkeypatch.setattr(cf, 'user_db', fake)

    def test_main_emoji_resolves_directly(self, monkeypatch):
        entry = _FakeEntry()
        self._patch_db(monkeypatch, entries={(str(GUILD_A), STAR): entry})
        main, resolved = Starboard._resolve_emoji(GUILD_A, STAR)
        assert main == STAR
        assert resolved is entry

    def test_alias_resolves_to_main(self, monkeypatch):
        entry = _FakeEntry()
        self._patch_db(monkeypatch,
                       entries={(str(GUILD_A), STAR): entry},
                       aliases={(str(GUILD_A), THUMBS_UP): STAR})
        main, resolved = Starboard._resolve_emoji(GUILD_A, THUMBS_UP)
        assert main == STAR
        assert resolved is entry

    def test_unknown_emoji_returns_none(self, monkeypatch):
        self._patch_db(monkeypatch)
        main, resolved = Starboard._resolve_emoji(GUILD_A, FIRE)
        assert main == FIRE
        assert resolved is None

    def test_alias_to_unconfigured_main(self, monkeypatch):
        """Alias points to a main emoji that isn't configured -> None."""
        self._patch_db(monkeypatch,
                       aliases={(str(GUILD_A), THUMBS_UP): STAR})
        main, resolved = Starboard._resolve_emoji(GUILD_A, THUMBS_UP)
        assert main == STAR
        assert resolved is None


# =====================================================================
# ;starboard show command
# =====================================================================

class TestStarboardShow:
    """Test the show command's data retrieval and formatting logic."""

    def test_show_single_emoji(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.set_starboard_channel(GUILD_A, STAR, '999888')
        entries = db.get_starboard_emojis_for_guild(GUILD_A)
        aliases = db.get_all_aliases_for_guild(GUILD_A)
        assert len(entries) == 1
        assert len(aliases) == 0
        assert entries[0].emoji == STAR
        assert entries[0].threshold == 3
        assert entries[0].color == 0xffaa10
        assert entries[0].channel_id == '999888'

    def test_show_multiple_emojis(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_emoji(GUILD_A, FIRE, 5, 0xff0000)
        db.set_starboard_channel(GUILD_A, STAR, '100')
        db.set_starboard_channel(GUILD_A, FIRE, '200')
        entries = db.get_starboard_emojis_for_guild(GUILD_A)
        assert len(entries) == 2

    def test_show_includes_aliases(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        db.add_starboard_alias(GUILD_A, THUMBS_UP, STAR)
        db.add_starboard_alias(GUILD_A, FIRE, STAR)
        aliases = db.get_all_aliases_for_guild(GUILD_A)
        alias_map = {}
        for a in aliases:
            alias_map.setdefault(a.main_emoji, []).append(a.alias_emoji)
        assert STAR in alias_map
        assert set(alias_map[STAR]) == {THUMBS_UP, FIRE}

    def test_show_empty_starboard(self, db):
        entries = db.get_starboard_emojis_for_guild(GUILD_A)
        assert entries == []

    def test_show_channel_not_set(self, db):
        db.add_starboard_emoji(GUILD_A, STAR, 3, 0xffaa10)
        entries = db.get_starboard_emojis_for_guild(GUILD_A)
        assert entries[0].channel_id is None


class TestLooksLikeEmoji:
    """Tests for the _looks_like_emoji helper used by ;starboard top."""

    def test_unicode_star(self):
        assert _looks_like_emoji(STAR) is True

    def test_unicode_fire(self):
        assert _looks_like_emoji(FIRE) is True

    def test_unicode_pill(self):
        assert _looks_like_emoji('\N{PILL}') is True

    def test_custom_emoji(self):
        assert _looks_like_emoji('<:custom:123456789>') is True

    def test_animated_custom_emoji(self):
        assert _looks_like_emoji('<a:dance:987654321>') is True

    def test_plain_username(self):
        assert _looks_like_emoji('nifeshe') is False

    def test_plain_username_with_numbers(self):
        assert _looks_like_emoji('user123') is False

    def test_timeline_keyword(self):
        assert _looks_like_emoji('week') is False

    def test_date_arg(self):
        assert _looks_like_emoji('d>=2024') is False
