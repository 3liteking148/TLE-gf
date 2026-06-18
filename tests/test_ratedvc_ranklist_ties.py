"""Tie handling for the `;vcratings` table — VCers tied on rating must share
a rank (standard '1224' ranking). Also guards against the old 0-based '#0'."""
import asyncio
import re
import sys
import types
from types import SimpleNamespace

# The real contests cog imports tle.util.ranklist, which drags in numpy.fft
# (unavailable here). Stub it out before importing the cog — same approach as
# tests/test_contests_tables.py.
from tle import util as tle_util  # noqa: E402
from tle.util import cache_system2 as _cs  # noqa: E402
from tle.util import events as _events, tasks as _tasks  # noqa: E402

_ranklist_stub = types.ModuleType('tle.util.ranklist')
_ranklist_stub.RanklistError = type('RanklistError', (Exception,), {})
_ranklist_stub.HandleNotPresentError = type('HandleNotPresentError', (Exception,), {})
_ranklist_stub.Ranklist = object
sys.modules['tle.util.ranklist'] = _ranklist_stub
tle_util.ranklist = _ranklist_stub

if not hasattr(_tasks.Waiter, 'for_event'):
    _tasks.Waiter.for_event = staticmethod(lambda _event: _tasks.Waiter())
for _event_name in ('ContestListRefresh', 'RatingChangesUpdate'):
    if not hasattr(_events, _event_name):
        setattr(_events, _event_name, type(_event_name, (), {}))
for _err in ('CacheError', 'RanklistNotMonitored'):
    if not hasattr(_cs, _err):
        setattr(_cs, _err, type(_err, (Exception,), {}))

from tle.cogs.contests import Contests  # noqa: E402
from tle.util import codeforces_api as cf  # noqa: E402
from tle.util import codeforces_common as cf_common  # noqa: E402
from tle.util import discord_common  # noqa: E402
from tle.util import paginator  # noqa: E402


class _Member:
    def __init__(self, uid):
        self.id = uid
        self.display_name = f'u{uid}'


class _Guild:
    id = 111


class _Converter:
    async def convert(self, ctx, member_id):
        return _Member(int(member_id))


def _data_ranks(table_str):
    ranks = []
    for line in table_str.splitlines():
        m = re.match(r'\s*(\d+)\s+u\d+', line)
        if m:
            ranks.append(int(m.group(1)))
    return ranks


def test_vcratings_ties_share_rank_and_start_at_one(monkeypatch):
    ratings = {1: 1800, 2: 1800, 3: 1800, 4: 1500}
    fake_db = SimpleNamespace(
        get_handles_for_guild=lambda gid: [(uid, f'h{uid}') for uid in ratings],
        get_vc_rating=lambda uid, default_if_not_exist=False: ratings[uid],
    )
    monkeypatch.setattr(cf_common, 'user_db', fake_db)
    monkeypatch.setattr(cf, 'rating2rank',
                        lambda r: SimpleNamespace(title_abbr='X'), raising=False)
    monkeypatch.setattr(discord_common, 'cf_color_embed',
                        lambda **kw: kw.get('description'))
    captured = {}
    monkeypatch.setattr(paginator, 'paginate',
                        lambda bot, channel, pages, **kw: captured.update(pages=pages))

    ctx = SimpleNamespace(guild=_Guild(), channel=object(),
                          author=SimpleNamespace(id=1))
    cog = Contests.__new__(Contests)
    cog.bot = None
    cog.member_converter = _Converter()
    asyncio.run(Contests.vcratings.__wrapped__(cog, ctx))

    table_str = captured['pages'][0][1]
    ranks = _data_ranks(table_str)
    assert ranks == [1, 1, 1, 4]
    assert 0 not in ranks  # the old builder rendered the first row as '#0'
