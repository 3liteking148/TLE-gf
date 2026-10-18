"""ThemeCP progression command helpers — cog layer for ``GitgudMixin``.

Parsing, problem selection and embed rendering for ``;gitgudprogression``.
Unlike ``tle/util/gitgud_progression.py`` (pure data), everything here may
raise ``CodeforcesCogError`` and speak the ``GitgudBackend``/``GitgudProblem``
protocol.
"""
from __future__ import annotations

from typing import List, Sequence, Set, Tuple

from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_args import (
    GitgudArgs,
    PROGRESSION_USAGE,
    parse_tag_filters,
    progression_usage,
)
from tle.cogs._gitgud_protocol import GitgudBackend, GitgudProblem
from tle.util.gitgud_progression import (
    THEME_MULTS,
    ThemeLevel,
    biased_choice,
    max_level,
    theme_of,
)


# ThemeCP background, shared by ``;help gitgudprogression`` and the
# no-argument usage error so the links are written down exactly once. Bare URLs
# rather than ``[label](url)``: Discord does not render markdown link syntax in
# message bodies or embed descriptions, it just shows the raw text, while a bare
# URL is auto-linked.
THEMECP_REFERENCES = (
    'More about ThemeCP: https://codeforces.com/blog/entry/136704\n'
    'ThemeCP level sheet: https://docs.google.com/spreadsheets/d/'
    '1gdD-syEpfy10Vz1f5UAm5eKiV_UAEdG-C4jrouN57bs'
)

# The full ``;help gitgudprogression`` body. Passed as ``help=`` rather than
# left as a docstring so it can reuse ``THEMECP_REFERENCES``; discord.py gives
# an explicit ``help`` precedence over ``__doc__``.
GITGUD_PROGRESSION_HELP = f"""ThemeCP-style progression batch: 4 problems (slots A-D) at the level's fixed ratings.

Claim with ;gotgud once all 4 are solved, or append +partial to claim
a solved subset (at least one required; the unsolved rest is skipped).
Each slot pays base ladder points; solving within the level's time
window upgrades the unbroken streak starting at A with mults
x1, x1, x1.5, x2 — so A+B+C+D earns the full bonus, but a lone D
(or any solved problem with an unsolved problem before it) scores base points only.
Optional +tag/~tag filters apply to every slot; ratings are fixed by the
level, so no rating or date argument is accepted.
;nogud after 2h skips the whole batch.

{THEMECP_REFERENCES}"""


def require_theme(level: int):
    """ThemeLevel for *level* or raise naming the valid range."""
    theme = theme_of(level)
    if theme is None:
        raise CodeforcesCogError(f"Invalid ThemeCP level {level}. Valid 1..{max_level()}")
    return theme


def parse_progression_args(args: Sequence[str], backend: GitgudBackend) -> Tuple[int, ThemeLevel, list[str], list[str]]:
    """Parse ``;gitgudprogression <level> [+tag] [~tag]``.

    Returns ``(level, theme, tags, bantags)``. Level is positional
    ``args[0]``; the remainder goes through ``parse_tag_filters``, which
    accepts tags only — progression ratings are fixed by the CSV, so a
    rating/range argument is rejected as a non-tag with the usage line.
    """
    if not args:
        raise CodeforcesCogError(f'{progression_usage()}\n\n{THEMECP_REFERENCES}')
    try:
        level = int(args[0])
    except ValueError:
        raise CodeforcesCogError(
            f"Invalid ThemeCP level `{args[0]}` — use 1..{max_level()}")

    theme = require_theme(level)
    tags, bantags = parse_tag_filters(
        list(args[1:]), vocabulary=backend.tag_vocabulary(),
        exact=backend.exact, usage=PROGRESSION_USAGE)
    return level, theme, tags, bantags


def build_progression_desc_lines(
    problems: Sequence[GitgudProblem], scores: Sequence[int]
) -> list[str]:
    """Four embed lines with per-slot bonus preview."""
    lines = []
    for prob, score, mult in zip(problems, scores, THEME_MULTS):
        bonus = round(score * mult)
        lines.append(
            f"**{prob.index}. [{prob.name}]({prob.url})** "
            f"[{prob.rating}] ×{mult:g} - {score} points ({bonus} with bonus)"
        )
    return lines


def select_progression_problems(
    backend: GitgudBackend,
    level: int,
    solved: Set[str],
    noguds: Set[str],
    handle: str,
    tags: Sequence[str] = (),
    bantags: Sequence[str] = (),
) -> list:
    """Select 4 problems, one per ThemeCP rating slot, optionally filtered by tags."""
    ratings = require_theme(level).ratings
    chosen: List = []
    chosen_keys: set = set()
    # Use a local mutable copy so the caller's ``solved`` set is not mutated
    seen = set(solved)
    for rating in ratings:
        req = GitgudArgs(srating=rating, erating=rating, hidden=False,
                         tags=list(tags), bantags=list(bantags))
        pool = [p for p in
                backend.select(req, seen, noguds, handle)
                if p.key not in chosen_keys and p.name not in chosen_keys]
        if not pool:
            tag_hint = ""
            if tags or bantags:
                parts: List[str] = []
                if tags:
                    parts.append("+" + " +".join(tags))
                if bantags:
                    parts.append("~" + " ~".join(bantags))
                tag_hint = " with " + " ".join(parts)
            raise CodeforcesCogError(
                f"No problem for progression level {level} rating {rating}{tag_hint}. "
                "Try another level or solve some noguds."
            )
        prob = pool[biased_choice(pool)]
        chosen.append(prob)
        chosen_keys.add(prob.key)
        chosen_keys.add(prob.name)
        seen.add(prob.name)
        seen.add(prob.key)
    return chosen
