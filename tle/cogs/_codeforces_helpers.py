"""Pure helpers and constants for the codeforces cog.

Split out of ``codeforces.py`` to keep each module under the line limit.
"""
import re
from typing import List, Tuple

from discord.ext import commands

from tle import constants
from tle.util._cf_api_types import cf_tag_matches

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
    selection: Codeforces compares case-insensitively by word prefix against
    every cached problem tag (including the synthesized division tags),
    AtCoder compares exactly against contest types. An empty vocabulary
    (cache not loaded yet) disables the check — every filter would otherwise
    be a false positive.
    """
    if not vocabulary:
        return []
    known = list(vocabulary)
    unknown = []
    for tag in filters:
        if exact:
            found = tag in known
        else:
            found = any(cf_tag_matches(tag, v) for v in known)
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
