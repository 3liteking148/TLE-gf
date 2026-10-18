"""Pure helpers and constants for the codeforces cog.

Split out of ``codeforces.py`` to keep each module under the line limit.
"""
from typing import List, Tuple

from discord.ext import commands

from tle import constants

_GITGUD_NO_SKIP_TIME = 2 * 60 * 60

# Lenient claim window: a CF AC counts when at/after issue_time minus this
# margin (tolerate server clock desync)
_GITGUD_CLAIM_MARGIN = 10 * 60

_ONE_WEEK_DURATION = 7 * 24 * 60 * 60
_GITGUD_MORE_POINTS_START_TIME = 1680300000
# Completing a gitgud challenge also credits the betting wallet with this many
# coins per base gitgud point. Always applied to the *base* score, never the
# end-of-month-doubled monthly points. The base rate is 5x; the economy-wide
# GITGUD_COIN_EARN_MULTIPLIER (default 10) scales it so gitguds out-earn the
# flat daily claim — migration 1.58.0 applied the same factor retroactively.
_GITGUD_COIN_MULTIPLIER = 5 * constants.GITGUD_COIN_EARN_MULTIPLIER


class CodeforcesCogError(commands.CommandError):
    pass


def _split_gotgud_args(args):
    """Split ``;gotgud`` args into ``(submission_url, partial)``.

    ``+partial`` applies to any multi-challenge batch and is inert on
    singletons. The first non-flag arg is the AtCoder submission URL.
    """
    partial = '+partial' in args
    rest = [a for a in args if a != '+partial']
    return (rest[0] if rest else None), partial


def getEloWinProbability(ra: float, rb: float) -> float:
    return 1.0 / (1 + 10**((rb - ra) / 400.0))


def composeRatings(left: float, right: float, ratings: List[Tuple[float, int]]) -> int:
    for tt in range(20):
        r = (left + right) / 2.0

        rWinsProbability = 1.0
        for rating, count in ratings:
            rWinsProbability *= getEloWinProbability(r, rating)**count

        if rWinsProbability < 0.5:
            left = r
        else:
            right = r
    return round((left + right) / 2)
