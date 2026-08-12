"""Cross-guild handle sync hook + alt-account override tests.

Covers migration 1.52.0 (``updated_at``/``synced_at``), the new handle DB
methods, the ``bot.before_invoke`` hook (``_handles_sync.maybe_sync_handle``),
and the provenance rule that lets ``;handle identify`` overwrite only
auto-synced rows.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import tle.util.codeforces_api as cf
from tle.cogs import _handles_sync as handle_sync
from tle.cogs.handles import _check_identify_allowed, HandleCogError
from tle.util import codeforces_common as cf_common
from tle.util.db import handle_db
from tle.util.db.user_db_conn import UserDbConn
from tle.util.db.user_db_upgrades import registry, upgrade_1_52_0

USER_ID = 42
GUILD_A, GUILD_B, GUILD_C = 1, 2, 3


def _run(coro):
    return asyncio.run(coro)


class FakeRole:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, member_id, roles=None):
        self.id = member_id
        self.roles = list(roles or [])
        self.added = []
        self.removed = []

    async def add_roles(self, *roles, reason=None):
        self.added.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        self.removed.extend(roles)


class FakeGuild:
    def __init__(self, guild_id, members=None, roles=None):
        self.id = guild_id
        self.members = members or []
        self.roles = roles or []
        self._members = {member.id: member for member in self.members}

    def get_member(self, member_id):
        return self._members.get(member_id)


class FakeCommand:
    def __init__(self, name, parents=None):
        self.name = name
        self.parents = list(parents or [])
        self.qualified_name = name


def _ctx(guild, author_id, command, *, bot=False):
    author = SimpleNamespace(id=author_id, bot=bot)
    return SimpleNamespace(guild=guild, author=author, command=command)


class _RankedUser(cf.User):
    @property
    def rank(self):
        title = 'Expert' if (self.rating or 0) >= 1600 else 'Newbie'
        return SimpleNamespace(title=title)


def _make_user(handle, rating):
    values = [None] * len(_RankedUser._fields)
    values[0] = handle
    values[7] = rating
    values[8] = rating
    values[-1] = '//avatar.png'
    return _RankedUser._make(values)


@pytest.fixture
def user_db(monkeypatch):
    database = UserDbConn(':memory:')
    monkeypatch.setattr(cf_common, 'user_db', database, raising=False)
    fake_time = SimpleNamespace()
    fake_time.value = 1000
    fake_time.time = lambda: fake_time.value
    monkeypatch.setattr(handle_db, 'time', fake_time)
    monkeypatch.setattr(cf, 'User', _RankedUser, raising=False)
    monkeypatch.setattr(cf, 'RATED_RANKS', [
        SimpleNamespace(title=title) for title in (
            'Newbie', 'Pupil', 'Specialist', 'Expert', 'Candidate Master')],
        raising=False)
    monkeypatch.setattr(cf, 'UNRATED_RANK',
                        SimpleNamespace(title='Unrated'), raising=False)
    return database


# ── migration ────────────────────────────────────────────────────────────

def test_1_52_migration_adds_columns_and_is_idempotent():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        'CREATE TABLE user_handle ('
        'user_id TEXT, guild_id TEXT, handle TEXT, active INTEGER, '
        'PRIMARY KEY (user_id, guild_id))')
    conn.execute(
        "INSERT INTO user_handle VALUES ('42', '1', 'tourist', 1)")
    conn.commit()

    upgrade_1_52_0(conn)
    columns = {row[1] for row in conn.execute(
        'PRAGMA table_info(user_handle)').fetchall()}
    assert {'updated_at', 'synced_at'} <= columns
    row = conn.execute(
        'SELECT handle, updated_at, synced_at FROM user_handle'
    ).fetchone()
    assert row == ('tourist', None, None)

    upgrade_1_52_0(conn)  # idempotent
    conn.close()


def test_sync_migration_is_registered_after_cooldown_scopes():
    versions = [version for version, _, _ in registry.upgrades]
    assert '1.51.0' in versions
    assert '1.52.0' in versions
    assert '1.53.0' in versions
    assert '1.54.0' in versions
    assert versions.index('1.52.0') > versions.index('1.51.0')
    assert versions.index('1.53.0') > versions.index('1.52.0')
    assert versions.index('1.54.0') > versions.index('1.53.0')
    assert registry.latest_version == '1.55.0'


def test_fresh_db_schema_has_sync_columns():
    db = UserDbConn(':memory:')
    try:
        columns = {row[1] for row in db.conn.execute(
            'PRAGMA table_info(user_handle)').fetchall()}
        assert {'updated_at', 'synced_at'} <= columns
    finally:
        db.conn.close()


# ── DB methods ───────────────────────────────────────────────────────────

def test_set_handle_writes_provenance_and_timestamps(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    assert user_db.get_handle_row(USER_ID, GUILD_A) == ('main', None)

    user_db.set_handle(USER_ID, GUILD_B, 'alt', synced=True)
    handle, synced_at = user_db.get_handle_row(USER_ID, GUILD_B)
    assert handle == 'alt'
    assert synced_at == 1000


def test_other_guild_handle_picks_most_recent(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    handle_db.time.value = 2000
    user_db.set_handle(USER_ID, GUILD_B, 'alt')

    assert user_db.get_other_guild_handle(USER_ID, GUILD_C) == 'alt'
    assert user_db.get_other_guild_handle(USER_ID, GUILD_A) == 'alt'
    assert user_db.get_other_guild_handle(USER_ID, GUILD_B) == 'main'
    assert user_db.get_other_guild_handle(99, GUILD_A) is None


def test_other_guild_handle_ignores_inactive_rows(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.set_handle(USER_ID, GUILD_B, 'alt')
    user_db.set_inactive([(GUILD_B, USER_ID)])

    assert user_db.get_other_guild_handle(USER_ID, GUILD_C) == 'main'
    assert user_db.get_other_guild_handle(USER_ID, GUILD_A) is None


def test_legacy_rows_without_timestamps_sort_last(user_db):
    user_db.conn.execute(
        'INSERT INTO user_handle (user_id, guild_id, handle, active) '
        'VALUES (?, ?, ?, 1)', (USER_ID, GUILD_A, 'legacy'))
    user_db.set_handle(USER_ID, GUILD_B, 'fresh')

    assert user_db.get_other_guild_handle(USER_ID, GUILD_C) == 'fresh'
    assert user_db.get_other_guild_handle(USER_ID, GUILD_B) == 'legacy'


def test_manual_identify_overrides_synced_row(user_db):
    user_db.set_handle(USER_ID, GUILD_B, 'main', synced=True)
    handle_db.time.value = 2000
    user_db.set_handle(USER_ID, GUILD_B, 'alt')

    assert user_db.get_handle_row(USER_ID, GUILD_B) == ('alt', None)
    assert user_db.get_other_guild_handle(USER_ID, GUILD_C) == 'alt'


# ── hook ─────────────────────────────────────────────────────────────────

def test_hook_syncs_handle_and_assigns_role(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.cache_cf_user(_make_user('main', 1800))
    user_db.enable_auto_role_update(GUILD_B)

    member = FakeMember(USER_ID)
    guild = FakeGuild(GUILD_B, members=[member],
                      roles=[FakeRole('Admin'), FakeRole('Expert')])
    ctx = _ctx(guild, USER_ID, FakeCommand('gitgud'))
    _run(handle_sync.maybe_sync_handle(ctx))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    handle, synced_at = user_db.get_handle_row(USER_ID, GUILD_B)
    assert handle == 'main'
    assert synced_at is not None
    assert [role.name for role in member.added] == ['Expert']


def test_hook_is_idempotent(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.cache_cf_user(_make_user('main', 1800))
    user_db.enable_auto_role_update(GUILD_B)

    member = FakeMember(USER_ID)
    guild = FakeGuild(GUILD_B, members=[member], roles=[FakeRole('Expert')])
    _run(handle_sync.maybe_sync_handle(ctx_for(guild)))
    _run(handle_sync.maybe_sync_handle(ctx_for(guild)))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    assert len(member.added) == 1
    rows = user_db.conn.execute(
        'SELECT COUNT(*) FROM user_handle '
        'WHERE user_id = ? AND guild_id = ?',
        (str(USER_ID), str(GUILD_B))).fetchone()[0]
    assert rows == 1


def ctx_for(guild):
    return _ctx(guild, USER_ID, FakeCommand('gitgud'))


def test_hook_skips_handle_commands(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')

    guild = FakeGuild(GUILD_B)
    command = FakeCommand('identify', parents=[FakeCommand('handle')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, command)))

    assert user_db.get_handle(USER_ID, GUILD_B) is None


def test_hook_skips_atcoder_commands(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')

    guild = FakeGuild(GUILD_B)
    command = FakeCommand('identify', parents=[FakeCommand('atcoder')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, command)))

    assert user_db.get_handle(USER_ID, GUILD_B) is None


def test_hook_skips_dm_and_bots(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')

    ctx = _ctx(None, USER_ID, FakeCommand('gitgud'))
    _run(handle_sync.maybe_sync_handle(ctx))
    assert user_db.get_handle(USER_ID, GUILD_B) is None

    guild = FakeGuild(GUILD_B)
    ctx = _ctx(guild, USER_ID, FakeCommand('gitgud'), bot=True)
    _run(handle_sync.maybe_sync_handle(ctx))
    assert user_db.get_handle(USER_ID, GUILD_B) is None


def test_hook_is_silent_when_handle_taken_in_target_guild(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.set_handle(USER_ID + 1, GUILD_B, 'main')

    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_handle(USER_ID, GUILD_B) is None


def test_hook_does_not_assign_roles_without_auto_role_update(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.cache_cf_user(_make_user('main', 1800))

    member = FakeMember(USER_ID)
    guild = FakeGuild(GUILD_B, members=[member], roles=[FakeRole('Expert')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    assert member.added == []


def test_hook_is_silent_when_rank_role_missing(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.cache_cf_user(_make_user('main', 1800))
    user_db.enable_auto_role_update(GUILD_B)

    member = FakeMember(USER_ID)
    guild = FakeGuild(GUILD_B, members=[member], roles=[FakeRole('Admin')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    assert member.added == []


def test_hook_is_silent_when_profile_not_cached(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.enable_auto_role_update(GUILD_B)

    member = FakeMember(USER_ID)
    guild = FakeGuild(GUILD_B, members=[member], roles=[FakeRole('Expert')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    assert member.added == []


def test_hook_never_raises_on_db_errors(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')

    def boom(*args, **kwargs):
        raise RuntimeError('db down')

    user_db.get_other_guild_handle = boom
    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))
    assert user_db.get_handle(USER_ID, GUILD_B) is None


# ── full journey: sync via gitgud, then override with alt ────────────────

def test_gitgud_sync_then_identify_overrides_with_alt(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    guild = FakeGuild(GUILD_B)

    _run(handle_sync.maybe_sync_handle(
        _ctx(guild, USER_ID, FakeCommand('gitgud'))))
    assert user_db.get_handle_row(USER_ID, GUILD_B) == ('main', 1000)

    _check_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                            'alt', f'<@{USER_ID}>')
    handle_db.time.value = 2000
    user_db.set_handle(USER_ID, GUILD_B, 'alt')
    assert user_db.get_handle_row(USER_ID, GUILD_B) == ('alt', None)
    assert user_db.get_other_guild_handle(USER_ID, GUILD_C) == 'alt'


def test_identify_allowed_when_no_row(user_db):
    _check_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                            'alt', f'<@{USER_ID}>')


def test_identify_rejected_for_manual_row(user_db):
    user_db.set_handle(USER_ID, GUILD_B, 'main')
    with pytest.raises(HandleCogError, match='already set'):
        _check_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                'alt', f'<@{USER_ID}>')


def test_identify_rejected_when_handle_claimed_by_other(user_db):
    user_db.set_handle(USER_ID + 1, GUILD_B, 'main')
    with pytest.raises(HandleCogError, match='another user'):
        _check_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                'main', f'<@{USER_ID}>')


def test_identify_allowed_for_own_synced_handle(user_db):
    user_db.set_handle(USER_ID, GUILD_B, 'main', synced=True)
    _check_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                            'main', f'<@{USER_ID}>')
