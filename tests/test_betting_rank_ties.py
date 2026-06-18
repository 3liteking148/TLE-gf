"""Tie handling for betting leaderboards — `rank_line` and the rendered
`;bet leaderboard` pages must use standard competition ('1224') ranking.
"""
import asyncio
from collections import namedtuple
from types import SimpleNamespace

from tle.cogs import _betting_helpers as bh
from tle.cogs.betting import Betting
from tle.util import codeforces_common as cf_common
from tle.util import paginator


BalRow = namedtuple('BalRow', 'user_id balance')


def test_rank_line_ties_share_rank():
    rows = [BalRow('1', 500), BalRow('2', 500), BalRow('3', 500),
            BalRow('4', 100)]
    assert '#1' in bh.rank_line(rows, 1, 'balance', 'wallet')
    assert '#1' in bh.rank_line(rows, 2, 'balance', 'wallet')
    assert '#1' in bh.rank_line(rows, 3, 'balance', 'wallet')
    line4 = bh.rank_line(rows, 4, 'balance', 'wallet')
    assert '#4' in line4
    assert '#2' not in line4


def test_rank_line_distinct_values_unchanged():
    rows = [BalRow('1', 30), BalRow('2', 20), BalRow('3', 10)]
    assert '#1' in bh.rank_line(rows, 1, 'balance', 'wallet')
    assert '#2' in bh.rank_line(rows, 2, 'balance', 'wallet')
    assert '#3' in bh.rank_line(rows, 3, 'balance', 'wallet')


class _Member:
    def __init__(self, uid):
        self.id = uid
        self.mention = f'<@{uid}>'


class _Guild:
    id = 111

    def get_member(self, uid):
        return _Member(uid)


def _ranks(description):
    return [line.split(' ', 1)[0] for line in description.splitlines()
            if line.startswith('**#')]


def test_leaderboard_rendering_ties(monkeypatch):
    rows = [BalRow('1', 500), BalRow('2', 500), BalRow('3', 500),
            BalRow('4', 100)]
    fake_db = SimpleNamespace(
        bet_balance_leaderboard=lambda gid: list(rows),
        bet_profit_leaderboard=lambda gid: [],
    )
    monkeypatch.setattr(cf_common, 'user_db', fake_db)
    captured = {}
    monkeypatch.setattr(paginator, 'paginate',
                        lambda bot, channel, pages, **kw: captured.update(pages=pages))

    ctx = SimpleNamespace(guild=_Guild(), channel=object(),
                          author=SimpleNamespace(id=4))
    asyncio.run(Betting.leaderboard.__wrapped__(Betting(bot=None), ctx, None))

    desc = captured['pages'][0][1].description
    assert _ranks(desc) == ['**#1**', '**#1**', '**#1**', '**#4**']
