"""Shared fakes for gitgud progression tests.

Single helper module (per repo convention ``*_test_utils.py``) so the
progression test files import one set of fakes via
``from tests.gitgud_test_utils import ...``.
"""
import asyncio
import datetime
import random
from types import SimpleNamespace

import pytest

from tests.betting_test_utils import GUILD, USER_A, _make_market  # noqa: F401
from tle import constants
from tle.util import codeforces_common as cf_common


def _run(coro):
    return asyncio.run(coro)


def _cf_prob(name, cid, rating, index='A'):
    return SimpleNamespace(
        name=name,
        contestId=cid,
        contest_name=f'Round {cid}',
        index=index,
        key=name,
        rating=rating,
        url=f'https://codeforces.com/contest/{cid}/problem/{index}',
        tags=[],
        matches_all_tags=lambda tags: True if not tags else all(t in [] for t in tags),
        matches_any_tag=lambda tag: False,
        get_matched_tags=lambda tags: [],
    )


def _solo_prob(name='Solo'):
    return _cf_prob(name, 9999, 800)


def _ctx(uid=USER_A):
    guild = SimpleNamespace(id=GUILD)

    class _Ctx:
        def __init__(self):
            self.author = SimpleNamespace(id=uid, display_name='user')
            self.message = SimpleNamespace(author=SimpleNamespace(id=uid), id=999999)
            self.guild = guild
            self.channel = SimpleNamespace()
            self.sent = []

        async def send(self, content=None, *a, **kw):
            self.sent.append((content, kw.get('embed'), kw.get('view')))

            class _Msg:
                async def edit(self, **k):
                    pass
            return _Msg()

    return _Ctx()


@pytest.fixture
def cog(db, monkeypatch):
    from tle.cogs._gitgud import GitgudMixin
    monkeypatch.setattr(cf_common, 'user_db', db)
    monkeypatch.setattr(constants, 'BET_START_BALANCE', 1000, raising=False)
    # CF cache with 4 distinct 800 problems for level 1
    probs = [_cf_prob(f'CF800_{i}', 1000 + i, 800, chr(ord('A') + i)) for i in range(4)]
    # add a few extra for fallback robustness
    probs += [_cf_prob(f'CF800_x{i}', 2000 + i, 900, 'A') for i in range(2)]
    monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
        problem_cache=SimpleNamespace(problems=probs, problem_by_name={p.name: p for p in probs}),
        contest_cache=SimpleNamespace(get_contest=lambda cid: SimpleNamespace(startTimeSeconds=1000, name=f'Contest {cid}')),
        atcoder_problem_cache=SimpleNamespace(problems=[]),
    ))
    # prevent writer/nonstandard filtering from hiding problems
    monkeypatch.setattr(cf_common, 'is_nonstandard_problem', lambda p: False)
    monkeypatch.setattr(cf_common, 'is_contest_writer', lambda cid, h: False)
    monkeypatch.setattr(random, 'randrange', lambda n: 0)

    class C(GitgudMixin):
        pass

    c = C()
    c.converter = None
    c.bot = SimpleNamespace()
    return c


def _patch_cf_handle(monkeypatch, rating=800, solved=None):
    solved = solved or set()

    async def fake_resolve(ctx, converter, handles, **kw):
        return ['handleA']

    async def fake_status(*, handle):
        return [SimpleNamespace(verdict='OK', problem=SimpleNamespace(name=n)) for n in solved]

    from tle.util import codeforces_api as cf
    monkeypatch.setattr(cf_common, 'resolve_handles', fake_resolve)
    monkeypatch.setattr(cf, 'user', SimpleNamespace(status=fake_status), raising=False)
    monkeypatch.setattr(cf_common.user_db, 'fetch_cf_user', lambda h: SimpleNamespace(effective_rating=rating, rating=rating))


def _issue_level1(db, cog, monkeypatch, args=('1',)):
    """Issue a ThemeCP level-1 batch; returns its actives."""
    _patch_cf_handle(monkeypatch, rating=800, solved=set())
    _run(cog._gitgudprogression_impl(_ctx(), args))
    return db.list_active_challenges(USER_A)


def _solve_names(monkeypatch, names):
    _patch_cf_handle(monkeypatch, rating=800, solved=set(names))


def _backdate(db, seconds):
    """Shift every active challenge issue_time back so windows/guards expire."""
    old = int(datetime.datetime.now().timestamp()) - seconds
    for row in db.conn.execute('SELECT id FROM challenge WHERE finish_time IS NULL').fetchall():
        db.conn.execute('UPDATE challenge SET issue_time=? WHERE id=?', (old, row[0]))
    db.conn.execute('UPDATE user_challenge SET issue_time=? WHERE issue_time IS NOT NULL', (old,))
    db.conn.commit()


def _make_bettor(db, uid=USER_A):
    mid = _make_market(db, commence=1e12)
    db.bet_place(GUILD, mid, uid, 'home', 100, 1.0, 1000)
    assert db.bet_get_balance(GUILD, uid) == 900


def _more_points_on(cog, monkeypatch):
    monkeypatch.setattr(cog, '_check_more_points_active', lambda *a, **k: True)
