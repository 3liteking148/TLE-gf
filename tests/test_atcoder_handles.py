"""AtCoder handle linking tests — parser, fetch layer, DB, migration, identify flow.

Fixture HTML files in ``tests/fixtures/atcoder/`` are real snapshots
downloaded from atcoder.jp. To regenerate them:

    curl -s -A "Mozilla/5.0" https://atcoder.jp/users/tourist > tests/fixtures/atcoder/tourist.html
    curl -s -A "Mozilla/5.0" https://atcoder.jp/users/nullman > tests/fixtures/atcoder/nullman.html
    curl -s -A "Mozilla/5.0" https://atcoder.jp/users/drydock > tests/fixtures/atcoder/drydock.html

The harness stubs ``lxml``/``lxml.html`` with empty modules, so this module
pops the stubs before the parser's lazy import runs.
"""
import asyncio
import re
import sqlite3
import sys
from pathlib import Path

import pytest

for _mod in ('lxml', 'lxml.html'):
    sys.modules.pop(_mod, None)

from tle.util import atcoder_api  # noqa: E402
from tle.util import codeforces_common as cf_common  # noqa: E402
from tle.util import discord_common  # noqa: E402
from tle.util.db.user_db_conn import (  # noqa: E402
    UserDbConn, namedtuple_factory, UniqueConstraintFailed)
from tle.util.db.user_db_upgrades import registry  # noqa: E402
from tle.cogs.handles import HandleCogError  # noqa: E402
from tle.cogs._atcoder_handles import AtcoderHandlesMixin  # noqa: E402
import tle.cogs._atcoder_handles as atcoder_cog  # noqa: E402

FIXTURES = Path(__file__).parent / 'fixtures' / 'atcoder'


def _fixture(name):
    return (FIXTURES / f'{name}.html').read_bytes()


def _run(coro):
    return asyncio.run(coro)


def _make_db():
    return UserDbConn(':memory:')


# =====================================================================
# Parser (real lxml against real page snapshots)
# =====================================================================

class TestParseProfile:
    def test_tourist(self):
        user = atcoder_api._parse_profile(_fixture('tourist'))
        assert user.handle == 'tourist'
        assert user.affiliation == 'ITMO University'
        assert user.country == 'Belarus'
        assert user.rating == '3797'

    def test_nullman_no_affiliation(self):
        user = atcoder_api._parse_profile(_fixture('nullman'))
        assert user.handle == 'Nullman'
        assert user.affiliation == ''
        assert user.country == 'Philippines'
        assert user.rating == '683 (Provisional)'

    def test_404_page_parses_empty(self):
        user = atcoder_api._parse_profile(_fixture('drydock'))
        assert user.affiliation == ''
        assert user.country == ''
        assert user.rating == ''


# =====================================================================
# Fetch layer (fake aiohttp session)
# =====================================================================

class FakeResponse:
    def __init__(self, status, body=b''):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None):
        self.requests.append(url)
        status, body = self.responses.pop(0)
        return FakeResponse(status, body)

    async def close(self):
        pass


class TestGetUser:
    def test_not_found(self):
        session = FakeSession([(404, b'')])
        user = _run(atcoder_api.get_user('drydock', session=session))
        assert user is None
        assert session.requests == ['https://atcoder.jp/users/drydock']

    def test_parses_200(self):
        session = FakeSession([(200, _fixture('tourist'))])
        user = _run(atcoder_api.get_user('tourist', session=session))
        assert user.affiliation == 'ITMO University'

    def test_retries_429_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(atcoder_api, '_RETRY_DELAY_SECONDS', 0)
        session = FakeSession([(429, b''), (200, _fixture('tourist'))])
        user = _run(atcoder_api.get_user('tourist', session=session))
        assert user.affiliation == 'ITMO University'
        assert len(session.requests) == 2

    def test_gives_up_after_retries(self, monkeypatch):
        monkeypatch.setattr(atcoder_api, '_RETRY_DELAY_SECONDS', 0)
        session = FakeSession([(403, b''), (403, b'')])
        user = _run(atcoder_api.get_user('tourist', session=session))
        assert user is None
        assert len(session.requests) == 2


# =====================================================================
# DB layer
# =====================================================================

class TestAtcoderHandleDb:
    def test_crud(self):
        db = _make_db()
        assert db.get_atcoder_handle(1, 2) is None
        db.set_atcoder_handle(1, 2, 'tourist')
        assert db.get_atcoder_handle(1, 2) == 'tourist'
        assert db.get_atcoder_user_id('TOURIST', 2) == 1
        assert db.get_atcoder_handles_for_guild(2) == [(1, 'tourist')]
        assert db.remove_atcoder_handle('tourist', 2) == 1
        assert db.get_atcoder_handle(1, 2) is None

    def test_unique_handle_per_guild(self):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        with pytest.raises(UniqueConstraintFailed):
            db.set_atcoder_handle(9, 2, 'TOURIST')

    def test_independent_of_cf_handles(self):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        db.set_handle(1, 2, 'tourist_cf')
        assert db.get_atcoder_handle(1, 2) == 'tourist'
        assert db.get_handle(1, 2) == 'tourist_cf'


# =====================================================================
# Migration 1.53.0
# =====================================================================

class TestMigration:
    def test_1_53_0_creates_table(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = namedtuple_factory
        registry.ensure_version_table(conn)
        registry.set_version(conn, '1.52.0')
        registry.run(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'user_atcoder_handle' in tables
        assert registry.get_current_version(conn) == '1.54.0'
        conn.close()


# =====================================================================
# ;atcoder identify flow
# =====================================================================

class FakeMember:
    def __init__(self, uid):
        self.id = uid

    @property
    def mention(self):
        return f'<@{self.id}>'

    def __str__(self):
        return f'user{self.id}'


class FakeGuild:
    def __init__(self, gid):
        self.id = gid


class FakeCtx:
    def __init__(self, author, guild):
        self.author = author
        self.guild = guild
        self.sent = []
        self.sent_kwargs = []
        self.help_calls = []
        from types import SimpleNamespace
        self.message = SimpleNamespace(author=author)

    async def send(self, *args, **kwargs):
        self.sent.append(args)
        self.sent_kwargs.append(kwargs)

    async def send_help(self, command):
        self.help_calls.append(command)


class FakeEmbed:
    def __init__(self, description=None):
        self.description = description


class _Cog(AtcoderHandlesMixin):
    pass


def _invoke(cmd_name, *args):
    """Invoke an ;atcoder callback through the harness's command stub.

    The stubbed ``commands.group`` decorator wraps callbacks in a
    no-op ``_StubGroupResult``; the real coroutine function lives at
    ``.__wrapped__`` (after the user_guard wrapper). Method names differ
    from the public command names (``atcoder_identify`` vs ``identify``)
    so the mixin commands survive discord.py's cog-command collection.
    """
    return getattr(_Cog(), cmd_name).__wrapped__(_Cog(), *args)


def _invoke_identify(ctx, handle):
    return _invoke('atcoder_identify', ctx, handle)


def _make_ctx(author_id=1, guild_id=2):
    return FakeCtx(FakeMember(author_id), FakeGuild(guild_id))


def _user(handle, affiliation, country='', rating=''):
    return atcoder_api.AtCoderUser(handle, affiliation, country, rating)


class TestIdentify:
    def test_success(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(cf_common, 'user_db', db)
        monkeypatch.setattr(atcoder_cog, '_POLL_INTERVAL', 0)
        embeds = []
        monkeypatch.setattr(discord_common, 'embed_success',
                            lambda desc: embeds.append(desc) or FakeEmbed(desc))
        ctx = _make_ctx()
        token = {}

        async def fake_get_user(handle):
            if not ctx.sent:
                return _user('tourist', '')
            m = re.search(r'\*\*`([^`]+)`\*\*', ctx.sent[0][0])
            token['v'] = m.group(1)
            return _user('tourist', token['v'])

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        _run(_invoke_identify(ctx, 'tourist'))
        assert token['v'].startswith('tle-')
        assert db.get_atcoder_handle(1, 2) == 'tourist'
        assert ctx.sent[-1][0] == 'You can now revert your affiliation.'
        assert embeds == ['AtCoder handle for <@1> successfully set to **tourist**']

    def test_timeout(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(cf_common, 'user_db', db)
        monkeypatch.setattr(atcoder_cog, '_POLL_INTERVAL', 0)
        ctx = _make_ctx()

        async def fake_get_user(handle):
            return _user('tourist', '')

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        _run(_invoke_identify(ctx, 'tourist'))
        assert db.get_atcoder_handle(1, 2) is None
        assert 'try again' in ctx.sent[-1][0]

    def test_handle_not_found(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(cf_common, 'user_db', db)
        ctx = _make_ctx()

        async def fake_get_user(handle):
            return None

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        with pytest.raises(HandleCogError):
            _run(_invoke_identify(ctx, 'nobody'))

    def test_handle_already_claimed(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(7, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)

        async def fake_get_user(handle):
            return _user('tourist', '')

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        ctx = _make_ctx(author_id=1)
        with pytest.raises(HandleCogError):
            _run(_invoke_identify(ctx, 'tourist'))

    def test_own_handle_blocks_reidentify(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)

        async def fake_get_user(handle):
            return _user('tourist', '')

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        ctx = _make_ctx()
        with pytest.raises(HandleCogError):
            _run(_invoke_identify(ctx, 'tourist'))


# =====================================================================
# ;atcoder get — message content
# =====================================================================

class TestGet:
    def test_no_handle(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(cf_common, 'user_db', db)
        ctx = _make_ctx()
        with pytest.raises(HandleCogError):
            _run(_invoke('atcoder_get', ctx))

    def test_with_handle(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)

        async def fake_get_user(handle):
            return _user('tourist', 'ITMO University', 'Belarus', '3797')

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        ctx = _make_ctx()
        _run(_invoke('atcoder_get', ctx))
        embed = ctx.sent_kwargs[0]['embed']
        assert 'https://atcoder.jp/users/tourist' in embed.description
        assert embed.fields == [
            {'name': 'Rating', 'value': '3797', 'inline': True},
            {'name': 'Country', 'value': 'Belarus', 'inline': True},
        ]

    def test_profile_unavailable(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)

        async def fake_get_user(handle):
            return None

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        ctx = _make_ctx()
        _run(_invoke('atcoder_get', ctx))
        embed = ctx.sent_kwargs[0]['embed']
        assert 'https://atcoder.jp/users/tourist' in embed.description
        assert embed.fields == []

    def test_empty_profile_fields_fall_back(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)

        async def fake_get_user(handle):
            return _user('tourist', '', '', '')

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        ctx = _make_ctx()
        _run(_invoke('atcoder_get', ctx))
        embed = ctx.sent_kwargs[0]['embed']
        assert embed.fields == [
            {'name': 'Rating', 'value': 'Unrated', 'inline': True},
            {'name': 'Country', 'value': 'Unknown', 'inline': True},
        ]


# =====================================================================
# ;atcoder remove — message content
# =====================================================================

class TestRemove:
    def test_removes_handle(self, monkeypatch):
        db = _make_db()
        db.set_atcoder_handle(1, 2, 'tourist')
        monkeypatch.setattr(cf_common, 'user_db', db)
        embeds = []
        monkeypatch.setattr(discord_common, 'embed_success',
                            lambda desc: embeds.append(desc) or FakeEmbed(desc))
        ctx = _make_ctx()
        _run(_invoke('atcoder_remove', ctx, 'tourist'))
        assert db.get_atcoder_handle(1, 2) is None
        assert db.get_atcoder_user_id('tourist', 2) is None
        assert embeds == ['Removed `tourist` from database']

    def test_unknown_handle(self, monkeypatch):
        db = _make_db()
        monkeypatch.setattr(cf_common, 'user_db', db)
        ctx = _make_ctx()
        with pytest.raises(HandleCogError):
            _run(_invoke('atcoder_remove', ctx, 'nobody'))


# =====================================================================
# ;atcoder group — help dispatch
# =====================================================================

class TestGroup:
    def test_bare_group_dispatches_help(self):
        ctx = _make_ctx()
        ctx.command = _Cog().atcoder
        _run(_invoke('atcoder', ctx))
        assert len(ctx.help_calls) == 1
        assert ctx.help_calls[0] is _Cog().atcoder
