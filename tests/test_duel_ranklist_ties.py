"""Tie handling for the `;duel ranklist` table — duelists tied on rating
must share a rank (standard '1224' ranking)."""
import asyncio
import re
from types import SimpleNamespace

from tle.cogs import _duel_impl as duel_impl
from tle.cogs.duel import Dueling
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util import paginator


class _Member:
    def __init__(self, uid):
        self.id = uid
        self.display_name = f'u{uid}'


class _Guild:
    id = 111

    def get_member(self, uid):
        return _Member(uid)


def _data_ranks(table_str):
    """Pull the leading '#' column out of each rendered table data row."""
    ranks = []
    for line in table_str.splitlines():
        m = re.match(r'\s*(\d+)\s+u\d+', line)
        if m:
            ranks.append(int(m.group(1)))
    return ranks


def test_duel_ranklist_ties_share_rank(monkeypatch):
    duelists = [(1, 1900), (2, 1900), (3, 1900), (4, 1500)]
    fake_db = SimpleNamespace(
        get_duelists=lambda gid: list(duelists),
        get_handle=lambda uid, gid: f'h{uid}',
        get_num_duel_completed=lambda uid, gid: 1,
    )
    monkeypatch.setattr(cf_common, 'user_db', fake_db)
    # cf_color_embed is stubbed to None in conftest; surface the table text.
    monkeypatch.setattr(discord_common, 'cf_color_embed',
                        lambda **kw: kw.get('description'))
    captured = {}
    monkeypatch.setattr(paginator, 'paginate',
                        lambda bot, channel, pages, **kw: captured.update(pages=pages))

    ctx = SimpleNamespace(guild=_Guild(), channel=object(),
                          author=SimpleNamespace(id=1))
    cog = Dueling.__new__(Dueling)
    cog.bot = None
    asyncio.run(cog._ranklist_impl(ctx))

    table_str = captured['pages'][0][1]
    assert _data_ranks(table_str) == [1, 1, 1, 4]
