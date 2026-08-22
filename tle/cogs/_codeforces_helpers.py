"""Pure helpers and constants for the codeforces cog.

Split out of ``codeforces.py`` to keep each module under the line limit.
"""
import re
from typing import List

from discord.ext import commands

from tle import constants

_GITGUD_NO_SKIP_TIME = 2 * 60 * 60
_GITGUD_SCORE_DISTRIB = (1, 2, 3, 5, 8, 12, 17, 23)
_GITGUD_SCORE_DISTRIB_MIN = -400
_GITGUD_SCORE_DISTRIB_MAX = 300
_ONE_WEEK_DURATION = 7 * 24 * 60 * 60
_GITGUD_MORE_POINTS_START_TIME = 1680300000
# Completing a gitgud challenge also credits the betting wallet with this many
# coins per base gitgud point. Always applied to the *base* score, never the
# end-of-month-doubled monthly points. The base rate is 5x; the economy-wide
# GITGUD_COIN_EARN_MULTIPLIER (default 10) scales it so gitguds out-earn the
# flat daily claim — migration 1.58.0 applied the same factor retroactively.
_GITGUD_COIN_MULTIPLIER = 5 * constants.GITGUD_COIN_EARN_MULTIPLIER
_GITGUD_FREE_REQUIRED_TAGS = {'div1', 'atcoder', 'arc', 'agc'}
_GITGUD_FREE_BANNED_TAGS = {'div3', 'div4', 'edu', 'abc'}
# Suggestion appended when a gitgud/gimme argument cannot be classified —
# unquoted trailing words of a multi-word tag are the common cause.
_MULTIWORD_TAG_HINT = ('For multi-word Codeforces tags quote them, e.g. '
                       '"+data structures", or use just the distinctive first '
                       'word (+data already matches "data structures").')
# A rating spec is any number of digits, optionally ``-`` plus digits. Digit
# count is deliberately unrestricted: AtCoder ratings go down to 0, so ranges
# like ``0-1800`` and singles like ``50`` must parse (the old ``arg[0:3]``
# slice check silently discarded them).
_GITGUD_RATING_SPEC_RE = re.compile(r'\d+(?:-\d+)?')


def _calculateGitgudScoreForDelta(delta):
    if (delta <= _GITGUD_SCORE_DISTRIB_MIN):
        return _GITGUD_SCORE_DISTRIB[0]
    if (delta >= _GITGUD_SCORE_DISTRIB_MAX):
        return _GITGUD_SCORE_DISTRIB[-1]
    index = (delta - _GITGUD_SCORE_DISTRIB_MIN)//100
    return _GITGUD_SCORE_DISTRIB[index]


def _gitgudPenalisedTagCount(tags, bantags):
    """How many requested tags subtract points.

    Every ``+`` require and ``~`` ban counts like a topic tag unless it is a
    division filter that makes the pool harder instead of easier:

    * ``+div1`` is free because it restricts to the hardest division.
    * ``~div3``, ``~div4`` and ``~edu`` are free because they remove easier
      contest pools.

    Banning div1 (``~div1``) still counts, since that only makes the pool
    easier.
    """
    required = sum(1 for tag in tags
                   if tag.strip().lower() not in _GITGUD_FREE_REQUIRED_TAGS)
    banned = sum(1 for tag in bantags
                 if tag.strip().lower() not in _GITGUD_FREE_BANNED_TAGS)
    return required + banned


def _gitgudTagPenaltyScore(base_delta, num_tags):
    """Score for a challenge shrunk by the number of requested tags.

    Penalised tags divide the normal score by ``num_tags + 1``, rounded up,
    never below 1. The caller decides which parsed tags count; numeric rating
    arguments are never included here. The score is stored in the challenge
    row's ``score`` column; ``rating_delta`` keeps its raw meaning
    (``problem.rating - base``) and is never modified by the penalty.
    """
    base_score = _calculateGitgudScoreForDelta(base_delta)
    if num_tags <= 0:
        return base_score
    return max(1, (base_score + num_tags) // (num_tags + 1))


class CodeforcesCogError(commands.CommandError):
    pass


def _parseGitgudRatingArgs(args, default_rating, error_message,
                           bounds=None, junk_hint=''):
    """Extract an optional ``rating`` or ``lo-hi`` spec from gitgud/gimme
    args.

    Every argument must classify as one of:

    * a ``+``/``~`` tag filter (collected separately by ``parse_tags``),
    * a ``d<``/``d>=`` date filter (parsed by ``parse_daterange``),
    * a rating spec: digits, optionally ``-`` plus digits,

    otherwise ``CodeforcesCogError`` is raised — nothing silently falls back
    to the default range. A leading ``-`` keeps its historical dedicated
    error. The last rating spec wins; the ``X-Y`` form sets ``hidden`` even
    when both ends are equal. Inverted ranges always raise. With
    ``bounds=(lo, hi)`` a spec is rejected only when the whole range is
    unreachable (everything below ``lo`` or above ``hi``); partial overlaps
    stay legal and the untouched default bypasses validation.
    """
    srating = erating = default_rating
    hidden = False
    parsed = False
    for arg in args:
        if not arg or arg[0] in '+~' or arg.startswith(('d<', 'd>=')):
            continue
        if arg[0] == '-':
            raise CodeforcesCogError(error_message)
        if arg[0].isdigit():
            if not _GITGUD_RATING_SPEC_RE.fullmatch(arg):
                raise CodeforcesCogError(
                    f'{error_message} Invalid rating `{arg}`.')
            parts = arg.split('-')
            srating = int(parts[0])
            if len(parts) > 1:
                erating = int(parts[1])
                hidden = True
            else:
                erating = srating
            parsed = True
            continue
        detail = f' Unexpected argument `{arg}`.'
        if junk_hint:
            detail += ' ' + junk_hint
        raise CodeforcesCogError(error_message + detail)
    if parsed and (srating > erating or
                   (bounds is not None and
                    (erating < bounds[0] or srating > bounds[1]))):
        raise CodeforcesCogError(error_message)
    return srating, erating, hidden


def _unknownTagFilters(filters, vocabulary, *, exact=False):
    """Return the filter tokens that match nothing in ``vocabulary``.

    Mirrors how each platform matches tags so detection agrees with
    selection: Codeforces compares by substring against every cached problem
    tag (including the synthesized division tags), AtCoder compares exactly
    against contest types. An empty vocabulary (cache not loaded yet)
    disables the check — every filter would otherwise be a false positive.
    """
    if not vocabulary:
        return []
    known = list(vocabulary)
    unknown = []
    for tag in filters:
        if exact:
            found = tag in known
        else:
            found = any(tag in v for v in known)
        if not found:
            unknown.append(tag)
    return unknown


def _checkGitgudTags(tags, bantags, vocabulary, *, exact=False):
    """Raise ``CodeforcesCogError`` naming any ``+``/``~`` filter that
    matches nothing in ``vocabulary``, instead of letting it silently empty
    the pool into a misleading 'No problem to assign'."""
    unknown = ([f'+{tag}' for tag in _unknownTagFilters(tags, vocabulary,
                                                        exact=exact)] +
               [f'~{tag}' for tag in _unknownTagFilters(bantags, vocabulary,
                                                        exact=exact)])
    if unknown:
        raise CodeforcesCogError('Unknown tag(s): ' + ', '.join(unknown))


def getEloWinProbability(ra: float, rb: float) -> float:
    return 1.0 / (1 + 10**((rb - ra) / 400.0))


def composeRatings(left: float, right: float, ratings: List[float]) -> int:
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
