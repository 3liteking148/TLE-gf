"""Shared pool helpers for the platform gitgud backends."""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Set

from tle.cogs._gitgud_args import GitgudArgs
from tle.cogs._gitgud_protocol import GitgudProblem

StartOf = Callable[[GitgudProblem], float]
ExtraPred = Callable[[GitgudProblem], bool]


def matches_common(prob: GitgudProblem, req: GitgudArgs, solved: Set[str],
                   noguds: Set[str]) -> bool:
    """Rating band, solved/nogud membership and tag include/ban filters."""
    if prob.rating is None or not req.srating <= prob.rating <= req.erating:
        return False
    if prob.key in solved or prob.key in noguds:
        return False
    return (prob.matches_all_tags(req.tags)
            and not prob.matches_any_tag(req.bantags))


def run_pool(problems: Sequence[GitgudProblem], req: GitgudArgs,
             solved: Set[str], noguds: Set[str], *, start_of: StartOf,
             extra: Optional[ExtraPred] = None) -> List[GitgudProblem]:
    """Filter by the shared predicate + date window, sorted by contest start."""
    pool = [prob for prob in problems
            if matches_common(prob, req, solved, noguds)
            and (extra is None or extra(prob))
            and req.dlo <= start_of(prob) < req.dhi]
    pool.sort(key=start_of)
    return pool
