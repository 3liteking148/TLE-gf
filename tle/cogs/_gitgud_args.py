"""Argument parsing for the gitgud-family commands.

``;gitgud``/``;gimme`` share one grammar (tags, an optional rating or range,
a contest-start date window); ``;gitgudprogression`` accepts tags only. Two
error shapes cover it: a bad rating spec raises ``Invalid rating ...`` with
the platform bounds, every other unclassifiable token is reported together
as ``Unexpected argument(s) ...``, and malformed dates raise
``ParamParseError`` from ``cf_format.parse_daterange``. ``vocabulary``/
``exact`` describe how a filter word is checked (AtCoder contest types match
exactly, Codeforces tags by word prefix); an empty vocabulary disables the
check while a cache is cold.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, NoReturn, Optional, Sequence, Set

from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.util._cf_api_types import cf_tag_matches
from tle.util.cf_format import parse_daterange
from tle.util.gitgud_progression import max_level

__all__ = [
    'GitgudArgs', 'parse_args', 'parse_tag_filters', 'split_platform',
    'USAGE_FILTERS', 'GITGUD_USAGE', 'GIMME_USAGE',
    'PROGRESSION_FILTERS', 'PROGRESSION_USAGE',
    'progression_filters', 'progression_usage',
]


@dataclass
class GitgudArgs:
    """Parsed ``;gitgud``/``;gimme`` request (platform flag already stripped)."""

    srating: int
    erating: int
    hidden: bool
    tags: List[str] = field(default_factory=list)
    bantags: List[str] = field(default_factory=list)
    rating_given: bool = False
    dlo: float = 0
    dhi: float = 10 ** 10


USAGE_FILTERS = '[+atcoder] [rating|lo-hi] [+tag] [~tag] [+divX] [~divX]'

GITGUD_USAGE = f'Usage: ;gitgud {USAGE_FILTERS} [d>=[[dd]mm]yyyy] [d<[[dd]mm]yyyy]'
GIMME_USAGE = f'Usage: ;gimme {USAGE_FILTERS} [d>=[[dd]mm]yyyy] [d<[[dd]mm]yyyy]'
PROGRESSION_FILTERS = '<level> [+tag] [~tag]'
PROGRESSION_USAGE = f'Usage: ;gitgudprogression {PROGRESSION_FILTERS}'


def progression_filters() -> str:
    """``PROGRESSION_FILTERS`` with ``<level>`` bound to the live valid range,
    so the level bound is never written down by hand."""
    return PROGRESSION_FILTERS.replace('<level>', f'<level 1..{max_level()}>')


def progression_usage() -> str:
    """Full ``;gitgudprogression`` usage line with the live level range."""
    return f'Usage: ;gitgudprogression {progression_filters()}'

_RATING_RE = re.compile(r'\d+(?:-\d+)?')

_GRAMMAR_HINT = ('Tags use `+tag`/`~tag` (quote multi-word tags, e.g. '
                 '`+"data structures"`), ratings `1200`/`1000-1400`, '
                 'dates `d>=yyyy`/`d<yyyy`.')
_TAG_ONLY_HINT = 'This command takes only `+tag`/`~tag` filters.'


def _fail(msg: str, usage: str) -> NoReturn:
    raise CodeforcesCogError(f'{msg}\n{usage}')


def split_platform(args: Sequence[str]) -> tuple[str, list[str]]:
    """Split raw args into ``(platform, rest)``.

    ``+atcoder`` selects the AtCoder backend in any casing; every occurrence
    is stripped so a backend only ever sees filter tokens.
    """
    platform = 'cf'
    rest: list[str] = []
    for arg in args:
        if arg.lower() == '+atcoder':
            platform = 'ac'
            continue
        rest.append(arg)
    return platform, rest


def _parse_rating(arg: str, bounds: tuple[int, int],
                  usage: str) -> tuple[int, int, bool]:
    """``(srating, erating, hidden)`` for a rating spec or raise."""
    lo, hi = bounds
    detail = (f'Invalid rating `{arg}`. Use a rating like `1200` or a range '
              f'like `1000-1400` ({lo}-{hi}).')
    if not _RATING_RE.fullmatch(arg):
        _fail(detail, usage)
    parts = arg.split('-')
    srating = int(parts[0])
    erating = int(parts[1]) if len(parts) > 1 else srating
    if srating > erating or erating < lo or srating > hi:
        _fail(detail, usage)
    return srating, erating, len(parts) > 1


def _scan(args: Sequence[str], *, tag_only: bool,
          vocabulary: Optional[Set[str]], exact: bool,
          bounds: tuple[int, int], usage: str
          ) -> tuple[list[str], list[str], Optional[tuple[int, int, bool]],
                     list[str]]:
    """Classify every token; bad ones raise together after the loop."""
    tags: list[str] = []
    bantags: list[str] = []
    bad: list[str] = []
    dates: list[str] = []
    rating: Optional[tuple[int, int, bool]] = None
    for arg in args:
        if not arg:
            continue
        if arg[0] in '+~':
            name = arg[1:]
            known = not vocabulary or (
                name in vocabulary if exact
                else any(cf_tag_matches(name, tag) for tag in vocabulary))
            if name and known:
                (tags if arg[0] == '+' else bantags).append(name)
            else:
                bad.append(arg)
        elif arg[0].isdigit():
            if tag_only:
                bad.append(arg)
            else:
                rating = _parse_rating(arg, bounds, usage)
        elif arg.startswith(('d<', 'd>=')):
            if tag_only:
                bad.append(arg)
            else:
                dates.append(arg)
        else:
            bad.append(arg)
    if bad:
        noun = 'argument' if len(bad) == 1 else 'arguments'
        joined = ', '.join(f'`{arg}`' for arg in bad)
        hint = _TAG_ONLY_HINT if tag_only else _GRAMMAR_HINT
        _fail(f'Unexpected {noun} {joined}. {hint}', usage)
    return tags, bantags, rating, dates


def parse_args(args: Sequence[str], *, default_rating: int,
               bounds: tuple[int, int], vocabulary: Optional[Set[str]] = None,
               exact: bool = False, usage: str = GITGUD_USAGE) -> GitgudArgs:
    """Parse ``;gitgud``/``;gimme`` args into a ``GitgudArgs``."""
    tags, bantags, rating, dates = _scan(
        args, tag_only=False, vocabulary=vocabulary, exact=exact,
        bounds=bounds, usage=usage)
    srating = erating = default_rating
    hidden = False
    if rating is not None:
        srating, erating, hidden = rating
    dlo, dhi = parse_daterange(dates)
    return GitgudArgs(srating=srating, erating=erating, hidden=hidden,
                      tags=tags, bantags=bantags,
                      rating_given=rating is not None, dlo=dlo, dhi=dhi)


def parse_tag_filters(args: Sequence[str], *,
                      vocabulary: Optional[Set[str]] = None,
                      exact: bool = False,
                      usage: str = PROGRESSION_USAGE
                      ) -> tuple[list[str], list[str]]:
    """Parse a ``+``/``~``-only list into ``(tags, bantags)``."""
    tags, bantags, _, _ = _scan(
        args, tag_only=True, vocabulary=vocabulary, exact=exact,
        bounds=(0, 0), usage=usage)
    return tags, bantags
