"""Regression tests for the threshold gate in check_and_add_to_starboard.

Below-threshold reactions must still update an *already* starboarded message
— only the *initial* post is threshold-gated.  Uses lightweight bot/guild/
channel/payload fakes built around the in-memory FakeUserDb.
"""
import asyncio
from datetime import datetime

import pytest

import discord

from tests.starboard_test_utils import FakeUserDb, GUILD_A, STAR, _FakeAuthor
from tle.cogs.starboard import Starboard


@pytest.fixture
def db():
    d = FakeUserDb()
    yield d
    d.close()


class _FakeReaction:
    def __init__(self, emoji, count):
        self.emoji = emoji
        self.count = count

    def __str__(self):
        return self.emoji


class _FakeAuthorWithId(_FakeAuthor):
    id = 42


class _FakeMessageWithReactions:
    def __init__(self, msg_id, channel_id, reactions, content='hi'):
        self.id = msg_id
        self.content = content
        self.embeds = []
        self.attachments = []
        self.created_at = datetime(2025, 1, 1)
        self.jump_url = f'https://discord.com/channels/1/{channel_id}/{msg_id}'
        self.author = _FakeAuthorWithId()
        self.reference = None
        self.type = discord.MessageType.default
        self.reactions = reactions


class _FakeChannelWithMessage:
    def __init__(self, channel_id, message):
        self.id = channel_id
        self.nsfw = False
        self._message = message

    def get_channel(self, _id):
        return self

    async def fetch_message(self, _id):
        return self._message


class _FakeGuildForStarboard:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


class _FakeBotForStarboard:
    def __init__(self, guild, source_channel):
        self._guild = guild
        self._source_channel = source_channel

    def get_guild(self, _id):
        return self._guild

    def get_channel(self, _id):
        return self._source_channel

    async def fetch_channel(self, _id):
        return self._source_channel


class _FakePayload:
    def __init__(self, guild_id, channel_id, message_id, user_id):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.user_id = user_id


class TestCheckAndAddThresholdGate:
    """Regression tests for the threshold gate in check_and_add_to_starboard.

    Below-threshold reactions must still update an *already* starboarded
    message — otherwise the displayed count freezes once it falls below
    threshold. Only the *initial* post must be threshold-gated.
    """

    def _build_cog(self, monkeypatch, db, source_channel_id, message):
        from tle.util import codeforces_common as cf
        monkeypatch.setattr(cf, 'user_db', db)
        source_channel = _FakeChannelWithMessage(source_channel_id, message)
        starboard_channel = type('SBChan', (), {'send': lambda *a, **kw: None})()
        guild = _FakeGuildForStarboard(starboard_channel)
        bot = _FakeBotForStarboard(guild, source_channel)
        cog = Starboard.__new__(Starboard)
        cog.bot = bot
        cog.locks = {}
        update_calls = []

        async def fake_update(*args, **kwargs):
            update_calls.append((args, kwargs))

        cog._update_starboard_message = fake_update
        return cog, update_calls

    def test_below_threshold_updates_existing_starboard_message(self, db, monkeypatch):
        """The exact bug: count dropped to 2, threshold is 4, message is on
        starboard, user re-adds a reaction → display must update."""
        guild_id = GUILD_A
        message_id = 5001
        source_channel_id = 999
        threshold = 4
        db.add_starboard_emoji(guild_id, STAR, threshold, 0xffaa10)
        db.set_starboard_channel(guild_id, STAR, '888')
        db.add_starboard_message_v1(
            message_id, 7777, guild_id, STAR,
            author_id='123', channel_id=str(source_channel_id),
        )
        for uid in ('u1', 'u2'):
            db.add_reactor(message_id, STAR, uid)

        message = _FakeMessageWithReactions(
            message_id, source_channel_id,
            reactions=[_FakeReaction(STAR, 3)],
        )
        cog, update_calls = self._build_cog(monkeypatch, db, source_channel_id, message)
        payload = _FakePayload(guild_id, source_channel_id, message_id, 'u3')

        asyncio.run(cog.check_and_add_to_starboard(
            starboard_channel_id=888,
            threshold=threshold,
            color=0xffaa10,
            emoji_str=STAR,
            payload=payload,
            raw_emoji=STAR,
        ))

        assert len(update_calls) == 1, 'starboard message should be re-rendered'
        args, _ = update_calls[0]
        assert args[3] == 3, f'updated count should be 3 (was 2 + new reactor), got {args[3]}'

    def test_below_threshold_does_not_post_new_message(self, db, monkeypatch):
        """Counterpart: a not-yet-starboarded message below threshold must
        still be skipped (no new starboard post)."""
        guild_id = GUILD_A
        message_id = 5002
        source_channel_id = 999
        threshold = 4
        db.add_starboard_emoji(guild_id, STAR, threshold, 0xffaa10)
        db.set_starboard_channel(guild_id, STAR, '888')

        message = _FakeMessageWithReactions(
            message_id, source_channel_id,
            reactions=[_FakeReaction(STAR, 1)],
        )
        cog, update_calls = self._build_cog(monkeypatch, db, source_channel_id, message)
        payload = _FakePayload(guild_id, source_channel_id, message_id, 'u1')

        asyncio.run(cog.check_and_add_to_starboard(
            starboard_channel_id=888,
            threshold=threshold,
            color=0xffaa10,
            emoji_str=STAR,
            payload=payload,
            raw_emoji=STAR,
        ))

        assert update_calls == [], 'no edit should happen for a non-starboarded message below threshold'
        assert not db.check_exists_starboard_message_v1(message_id, STAR), \
            'message must not be inserted into starboard below threshold'
