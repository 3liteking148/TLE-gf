"""Tie handling for starboard leaderboards — `_make_leaderboard_pages` and the
`;starboard top` builder must use standard competition ('1224') ranking.
"""
from collections import namedtuple
from types import SimpleNamespace

from tle.cogs.starboard import Starboard


def _cog():
    # Bypass __init__ (which wires bot state); we only call pure page builders.
    return Starboard.__new__(Starboard)


class _Member:
    def __init__(self, uid):
        self.id = uid
        self.mention = f'<@{uid}>'


class _Guild:
    id = 111

    def get_member(self, uid):
        return _Member(uid)


def _ctx(author_id=1):
    return SimpleNamespace(guild=_Guild(), channel=object(),
                           author=SimpleNamespace(id=author_id))


def _ranks(description):
    return [line.split(' ', 1)[0] for line in description.splitlines()
            if line.startswith('**#')]


def test_star_leaderboard_ties_share_rank():
    Row = namedtuple('Row', 'author_id total_stars')
    rows = [Row('1', 40), Row('2', 40), Row('3', 40), Row('4', 10)]
    pages = _cog()._make_leaderboard_pages(_ctx(), rows, '⭐',
                                           'Star Leaderboard', 'stars')
    assert _ranks(pages[0][1].description) == \
        ['**#1**', '**#1**', '**#1**', '**#4**']


def test_personal_rank_line_reflects_tie():
    Row = namedtuple('Row', 'author_id total_stars')
    rows = [Row('1', 40), Row('2', 40), Row('3', 40), Row('4', 10)]
    pages = _cog()._make_leaderboard_pages(_ctx(author_id=3), rows, '⭐',
                                           'Star Leaderboard', 'stars')
    assert 'Your rank: **#1**' in pages[0][1].description


def test_top_messages_ties_share_rank():
    Row = namedtuple('Row', 'channel_id original_msg_id author_id star_count')
    rows = [Row(9, i, str(i), 12) for i in range(1, 4)] + \
           [Row(9, 99, '99', 3)]
    pages = _cog()._make_top_pages(_ctx(), rows, '⭐', None)
    assert _ranks(pages[0][1].description) == \
        ['**#1**', '**#1**', '**#1**', '**#4**']
