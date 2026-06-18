"""End-to-end rendering tests for the great day `;greatday stats`
leaderboard — proves tied counts share a rank in the actual embed output.
"""
import asyncio
from types import SimpleNamespace

from tle.cogs import greatday as greatday_module
from tle.cogs.greatday import GreatDay
from tle.util import codeforces_common as cf_common

from tests.greatday_test_utils import GUILD, FakeGreatDayDb  # noqa: F401


class _Member:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f'<@{user_id}>'


class _Guild:
    def __init__(self, guild_id):
        self.id = guild_id

    def get_member(self, user_id):
        return _Member(user_id)


def _record(db, user_ids, message_id):
    db.greatday_record_picks(GUILD, [str(u) for u in user_ids], message_id, 0.0)


def _run_stats(db, author_id):
    cog = GreatDay(bot=None)
    guild = _Guild(int(GUILD))
    ctx = SimpleNamespace(guild=guild, channel=object(),
                          author=SimpleNamespace(id=author_id))
    captured = {}
    orig = greatday_module.paginator.paginate
    greatday_module.paginator.paginate = \
        lambda bot, channel, pages, **kw: captured.update(pages=pages)
    orig_db = cf_common.user_db
    cf_common.user_db = db
    try:
        # stats is wrapped by the stub group .command() decorator.
        asyncio.run(GreatDay.stats.__wrapped__(cog, ctx))
    finally:
        greatday_module.paginator.paginate = orig
        cf_common.user_db = orig_db
    return captured['pages']


def _ranks_in(description):
    return [line.split(' ', 1)[0] for line in description.splitlines()
            if line.startswith('**#')]


def test_three_way_tie_for_first_all_rank_one():
    db = FakeGreatDayDb()
    # A, B, C each picked 3 times; D once. counts: 3,3,3,1
    for msg_id, mid in enumerate((10, 11, 12)):
        _record(db, [100, 200, 300], mid)
    _record(db, [400], 20)

    pages = _run_stats(db, author_id=400)
    _, embed = pages[0]
    assert _ranks_in(embed.description) == ['**#1**', '**#1**', '**#1**', '**#4**']


def test_personal_line_reflects_tie():
    db = FakeGreatDayDb()
    for mid in (10, 11, 12):
        _record(db, [100, 200, 300], mid)
    _record(db, [400], 20)

    pages = _run_stats(db, author_id=300)
    personal = pages[0][0]
    assert 'Your rank: **#1**' in personal


def test_tie_spanning_page_boundary():
    """A tie group that straddles a 15-per-page boundary still shares a rank
    — ranks are computed over the whole list, not per page."""
    db = FakeGreatDayDb()
    # 20 users all picked exactly once -> all tied at count 1 -> all rank 1,
    # across both pages.
    for uid in range(1, 21):
        _record(db, [uid], 100 + uid)

    pages = _run_stats(db, author_id=1)
    assert len(pages) == 2
    page1_ranks = _ranks_in(pages[0][1].description)
    page2_ranks = _ranks_in(pages[1][1].description)
    assert set(page1_ranks) == {'**#1**'}
    assert set(page2_ranks) == {'**#1**'}
