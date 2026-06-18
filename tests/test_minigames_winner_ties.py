"""Tie handling for the `;<game> top` minigame winners leaderboard — users
tied on win count must share a rank (standard '1224' ranking)."""
import asyncio
from types import SimpleNamespace

from tle.cogs import _mgimpl_sharedcmd as shared
from tle.cogs.minigames import Minigames
from tle.util import codeforces_common as cf_common
from tle.util import paginator


def _ranks(description):
    return [line.split(' ', 1)[0] for line in description.splitlines()
            if line.startswith('**#')]


def test_minigame_winners_ties_share_rank(monkeypatch):
    cog = Minigames(bot=None)
    # Neutralize the heavy read path; winners are injected via compute_top.
    monkeypatch.setattr(cog, '_require_enabled', lambda *a, **k: None)
    monkeypatch.setattr(cog, '_sync_minigame_results_for_read',
                        lambda *a, **k: None)
    monkeypatch.setattr(cog, '_filter_minigame_banned_rows',
                        lambda gid, game, rows: rows)
    monkeypatch.setattr(cog, '_minigame_public_user_name',
                        lambda guild, game, uid: f'u{uid}')

    scoring = SimpleNamespace(is_eligible_winner=None, best_result_sort_key=None,
                              winner_result_sort_key=None, result_group_key=None)
    game = SimpleNamespace(name='akari', display_name='Akari')
    monkeypatch.setattr(shared, 'resolve_scoring',
                        lambda g, args: (list(args), None, scoring))
    # 3 users tied at 5 wins, 1 at 2 wins -> ranks 1, 1, 1, 4
    winners = [('1', 5), ('2', 5), ('3', 5), ('4', 2)]
    monkeypatch.setattr(shared, 'compute_top', lambda rows, **kw: list(winners))

    monkeypatch.setattr(cf_common, 'user_db', SimpleNamespace(
        get_minigame_results_for_guild=lambda *a, **k: []))
    captured = {}
    monkeypatch.setattr(paginator, 'paginate',
                        lambda bot, channel, pages, **kw: captured.update(pages=pages))

    ctx = SimpleNamespace(guild=SimpleNamespace(id=111), channel=object(),
                          author=SimpleNamespace(id=1))
    asyncio.run(cog._cmd_top(ctx, game))

    desc = captured['pages'][0][1].description
    assert _ranks(desc) == ['**#1**', '**#1**', '**#1**', '**#4**']
