"""Cross-guild AtCoder handle sync + alt-account override tests.

Mirrors ``test_handle_sync.py`` for the Codeforces pipeline: covers migration
1.54.0 (``synced_at``), the AtCoder handle DB methods, the ``;atcoder`` branch
of the ``bot.before_invoke`` hook (``_handles_sync.maybe_sync_handle``), and
the provenance rule that lets ``;atcoder identify`` overwrite only auto-synced
rows.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from tle.cogs import _handles_sync as handle_sync
from tle.cogs._atcoder_handles import _check_atcoder_identify_allowed
from tle.cogs._handles_helpers import HandleCogError
from tle.util import codeforces_common as cf_common
from tle.util.db import atcoder_handle_db
from tle.util.db.user_db_conn import UserDbConn
from tle.util.db.user_db_upgrades import registry, upgrade_1_54_0

USER_ID = 42
GUILD_A, GUILD_B, GUILD_C = 1, 2, 3


def _run(coro):
    return asyncio.run(coro)


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class FakeCommand:
    def __init__(self, name, parents=None):
        self.name = name
        self.parents = list(parents or [])
        self.qualified_name = name


def _ctx(guild, author_id, command, *, bot=False):
    author = SimpleNamespace(id=author_id, bot=bot)
    return SimpleNamespace(guild=guild, author=author, command=command)


@pytest.fixture
def user_db(monkeypatch):
    database = UserDbConn(':memory:')
    monkeypatch.setattr(cf_common, 'user_db', database, raising=False)
    fake_time = SimpleNamespace()
    fake_time.value = 1000
    fake_time.time = lambda: fake_time.value
    monkeypatch.setattr(atcoder_handle_db, 'time', fake_time)
    return database


# ── migration ────────────────────────────────────────────────────────────

def test_1_54_migration_adds_column_and_is_idempotent():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        'CREATE TABLE user_atcoder_handle ('
        'user_id TEXT, guild_id TEXT, handle TEXT, active INTEGER, '
        'updated_at INTEGER, '
        'PRIMARY KEY (user_id, guild_id))')
    conn.execute(
        "INSERT INTO user_atcoder_handle VALUES ('42', '1', 'tourist', 1, 500)")
    conn.commit()

    upgrade_1_54_0(conn)
    columns = {row[1] for row in conn.execute(
        'PRAGMA table_info(user_atcoder_handle)').fetchall()}
    assert 'synced_at' in columns
    row = conn.execute(
        'SELECT handle, updated_at, synced_at FROM user_atcoder_handle'
    ).fetchone()
    assert row == ('tourist', 500, None)

    upgrade_1_54_0(conn)  # idempotent
    conn.close()


def test_sync_migration_is_registered_after_atcoder_table():
    versions = [version for version, _, _ in registry.upgrades]
    assert '1.53.0' in versions
    assert '1.54.0' in versions
    assert versions.index('1.54.0') > versions.index('1.53.0')
    assert registry.latest_version == '1.54.0'


def test_fresh_db_schema_has_sync_column():
    db = UserDbConn(':memory:')
    try:
        columns = {row[1] for row in db.conn.execute(
            'PRAGMA table_info(user_atcoder_handle)').fetchall()}
        assert 'synced_at' in columns
    finally:
        db.conn.close()


# ── DB methods ───────────────────────────────────────────────────────────

def test_set_atcoder_handle_writes_provenance_and_timestamps(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')
    assert user_db.get_atcoder_handle_row(USER_ID, GUILD_A) == ('tourist', None)

    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'alt', synced=True)
    handle, synced_at = user_db.get_atcoder_handle_row(USER_ID, GUILD_B)
    assert handle == 'alt'
    assert synced_at == 1000


def test_other_guild_atcoder_handle_picks_most_recent(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')
    atcoder_handle_db.time.value = 2000
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'alt')

    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_C) == 'alt'
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_A) == 'alt'
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_B) == 'tourist'
    assert user_db.get_other_guild_atcoder_handle(99, GUILD_A) is None


def test_other_guild_atcoder_handle_ignores_inactive_rows(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'alt')
    user_db.conn.execute(
        'UPDATE user_atcoder_handle SET active = 0 '
        'WHERE user_id = ? AND guild_id = ?', (str(USER_ID), str(GUILD_B)))

    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_C) == 'tourist'
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_A) is None


def test_legacy_rows_without_timestamps_sort_last(user_db):
    user_db.conn.execute(
        'INSERT INTO user_atcoder_handle (user_id, guild_id, handle, active) '
        'VALUES (?, ?, ?, 1)', (USER_ID, GUILD_A, 'legacy'))
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'fresh')

    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_C) == 'fresh'
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_B) == 'legacy'


def test_manual_identify_overrides_synced_row(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'tourist', synced=True)
    atcoder_handle_db.time.value = 2000
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'alt')

    assert user_db.get_atcoder_handle_row(USER_ID, GUILD_B) == ('alt', None)
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_C) == 'alt'


# ── hook ─────────────────────────────────────────────────────────────────

def test_hook_syncs_atcoder_handle(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) == 'tourist'
    handle, synced_at = user_db.get_atcoder_handle_row(USER_ID, GUILD_B)
    assert handle == 'tourist'
    assert synced_at == 1000


def test_hook_syncs_platforms_independently(user_db):
    user_db.set_handle(USER_ID, GUILD_A, 'main')
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_handle(USER_ID, GUILD_B) == 'main'
    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) == 'tourist'
    assert user_db.get_atcoder_handle_row(USER_ID, GUILD_B)[1] is not None


def test_hook_syncs_atcoder_even_without_cf_handle(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) == 'tourist'


def test_hook_is_idempotent_for_atcoder(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    guild = FakeGuild(GUILD_B)
    ctx = _ctx(guild, USER_ID, FakeCommand('gitgud'))
    _run(handle_sync.maybe_sync_handle(ctx))
    _run(handle_sync.maybe_sync_handle(ctx))

    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) == 'tourist'
    rows = user_db.conn.execute(
        'SELECT COUNT(*) FROM user_atcoder_handle '
        'WHERE user_id = ? AND guild_id = ?',
        (str(USER_ID), str(GUILD_B))).fetchone()[0]
    assert rows == 1


def test_hook_skips_atcoder_commands(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    guild = FakeGuild(GUILD_B)
    command = FakeCommand('identify', parents=[FakeCommand('atcoder')])
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, command)))

    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) is None


def test_hook_skips_dm_and_bots_for_atcoder(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    ctx = _ctx(None, USER_ID, FakeCommand('gitgud'))
    _run(handle_sync.maybe_sync_handle(ctx))
    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) is None

    guild = FakeGuild(GUILD_B)
    ctx = _ctx(guild, USER_ID, FakeCommand('gitgud'), bot=True)
    _run(handle_sync.maybe_sync_handle(ctx))
    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) is None


def test_hook_is_silent_when_atcoder_handle_taken_in_target_guild(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')
    user_db.set_atcoder_handle(USER_ID + 1, GUILD_B, 'tourist')

    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))

    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) is None


def test_hook_never_raises_on_db_errors(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')

    def boom(*args, **kwargs):
        raise RuntimeError('db down')

    user_db.get_other_guild_atcoder_handle = boom
    guild = FakeGuild(GUILD_B)
    _run(handle_sync.maybe_sync_handle(_ctx(guild, USER_ID, FakeCommand('gitgud'))))
    assert user_db.get_atcoder_handle(USER_ID, GUILD_B) is None


# ── full journey: sync via gitgud, then override with alt ────────────────

def test_gitgud_sync_then_identify_overrides_with_alt(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_A, 'tourist')
    guild = FakeGuild(GUILD_B)

    _run(handle_sync.maybe_sync_handle(
        _ctx(guild, USER_ID, FakeCommand('gitgud'))))
    assert user_db.get_atcoder_handle_row(USER_ID, GUILD_B) == ('tourist', 1000)

    _check_atcoder_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                    'alt', f'<@{USER_ID}>')
    atcoder_handle_db.time.value = 2000
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'alt')
    assert user_db.get_atcoder_handle_row(USER_ID, GUILD_B) == ('alt', None)
    assert user_db.get_other_guild_atcoder_handle(USER_ID, GUILD_C) == 'alt'


def test_atcoder_identify_allowed_when_no_row(user_db):
    _check_atcoder_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                    'tourist', f'<@{USER_ID}>')


def test_atcoder_identify_rejected_for_manual_row(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'tourist')
    with pytest.raises(HandleCogError, match='already set'):
        _check_atcoder_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                        'alt', f'<@{USER_ID}>')


def test_atcoder_identify_rejected_when_handle_claimed_by_other(user_db):
    user_db.set_atcoder_handle(USER_ID + 1, GUILD_B, 'tourist')
    with pytest.raises(HandleCogError, match='another user'):
        _check_atcoder_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                        'tourist', f'<@{USER_ID}>')


def test_atcoder_identify_allowed_for_own_synced_handle(user_db):
    user_db.set_atcoder_handle(USER_ID, GUILD_B, 'tourist', synced=True)
    _check_atcoder_identify_allowed(cf_common.user_db, USER_ID, GUILD_B,
                                    'tourist', f'<@{USER_ID}>')
