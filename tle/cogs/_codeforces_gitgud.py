"""Codeforces gitgud backend — problem selection for the ``cf`` platform."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from tle.util import codeforces_api as cf
from tle.util import codeforces_common as cf_common
from tle.cogs._gitgud_args import GITGUD_USAGE, GitgudArgs, parse_args
from tle.cogs._gitgud_backend import run_pool
from tle.cogs._gitgud_protocol import ActiveChallenge, ContestId, GitgudProblem, PIndex
from tle.cogs._codeforces_helpers import (
    _GITGUD_CLAIM_MARGIN,
    CodeforcesCogError,
)
from tle.cogs._gitgud_scoring import CF_SCORE_MODEL

if TYPE_CHECKING:
    from discord.ext.commands import Context as DiscordContext
    from discord.ext.commands import Converter as DiscordConverter
    GitgudCtx = DiscordContext
    GitgudConverter = DiscordConverter
else:
    GitgudCtx = Any
    GitgudConverter = Any


def _rating_sort_key(problem: GitgudProblem) -> int:
    return problem.rating if problem.rating is not None else 0


_CF_RATING_MIN = cf_common.RATING_MIN
_CF_RATING_MAX = cf_common.RATING_MAX
_CF_RATING_STEP = 100


class _CfBackend:
    """Codeforces-flavoured problem acquisition and selection."""

    platform: str = 'cf'
    score_model = CF_SCORE_MODEL
    exact: bool = False

    def tag_vocabulary(self) -> Optional[Set[str]]:
        try:
            return {tag for prob in cf_common.cache2.problem_cache.problems
                    for tag in prob.tags}
        except AttributeError:
            return None

    def parse_request(self, args: Sequence[str], rating: int,
                      usage: str = GITGUD_USAGE) -> GitgudArgs:
        """Parse gitgud/gimme args; runs after the rating fetch it needs."""
        return parse_args(
            args, default_rating=rating, bounds=(_CF_RATING_MIN, _CF_RATING_MAX),
            vocabulary=self.tag_vocabulary(), exact=False, usage=usage)

    async def resolve_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> str:
        handle, = await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))
        return handle

    async def validate_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> None:
        await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))

    async def fetch_rating(self, handle: str) -> int:
        user = cf_common.user_db.fetch_cf_user(handle)
        assert user is not None
        return round(user.effective_rating, -2)

    def scale_rating(self, rating: int) -> Tuple[int, int]:
        user_rating = max(_CF_RATING_MIN, min(_CF_RATING_MAX, rating))
        delta_base = max(1100, min(3000, user_rating))
        return user_rating, delta_base

    async def fetch_solved(self, handle: str, *, only_ac: bool = True) -> Set[str]:
        submissions = await cf.user.status(handle=handle)
        if only_ac:
            return {sub.problem.name for sub in submissions if sub.verdict == 'OK'}
        return {sub.problem.name for sub in submissions}

    async def fetch_solve_times(
        self, handle: str, actives: Sequence[ActiveChallenge]
    ) -> Dict[int, float]:
        cutoff = actives[0].issue_time - _GITGUD_CLAIM_MARGIN
        try:
            submissions = await cf.user.status(handle=handle)
        except CodeforcesCogError:
            raise
        except Exception as exc:
            raise CodeforcesCogError(
                'Could not fetch your Codeforces submissions. '
                'Try again in a moment.') from exc
        first_by_id: Dict[Tuple[Any, Any], int] = {}
        first_by_name: Dict[str, int] = {}
        for sub in submissions:
            if sub.verdict != 'OK':
                continue
            ts = sub.creationTimeSeconds
            if ts < cutoff:
                continue
            cid = sub.problem.contestId
            idx = sub.problem.index
            norm = (None if cid is None else str(cid).strip(),
                    None if idx is None else str(idx).strip().upper())
            if norm[0] is not None and norm[1] is not None:
                if norm not in first_by_id or ts < first_by_id[norm]:
                    first_by_id[norm] = ts
            name = sub.problem.name
            if name not in first_by_name or ts < first_by_name[name]:
                first_by_name[name] = ts
        times: Dict[int, float] = {}
        for active in actives:
            norm_active = (None if active.contest_id is None else str(active.contest_id).strip(),
                           None if active.p_index is None else str(active.p_index).strip().upper())
            if norm_active[0] is not None and norm_active[1] is not None:
                if norm_active in first_by_id:
                    times[active.challenge_id] = first_by_id[norm_active]
                continue
            elif active.problem_key in first_by_name:
                times[active.challenge_id] = first_by_name[active.problem_key]
        return times

    async def _verify_single_claim(self, ctx: GitgudCtx, handle: str, active: ActiveChallenge, submission_url: Optional[str] = None) -> float:
        times = await self.fetch_solve_times(handle, [active])
        if active.challenge_id not in times:
            raise CodeforcesCogError('You haven\'t completed your challenge.')
        return times[active.challenge_id]

    async def verify_claims(
        self, ctx: GitgudCtx, handle: str, actives: Sequence[ActiveChallenge], submission_url: Optional[str] = None,
        partial: bool = False
    ) -> Tuple[List[ActiveChallenge], List[ActiveChallenge], Dict[int, float]]:
        if len(actives) == 1:
            ts = await self._verify_single_claim(ctx, handle, actives[0], submission_url)
            return [actives[0]], [], {actives[0].challenge_id: ts}
        times = await self.fetch_solve_times(handle, actives)
        done = [a for a in actives if a.challenge_id in times]
        missing = [a for a in actives if a.challenge_id not in times]
        if not partial and missing:
            names = [a.problem_key for a in missing]
            raise CodeforcesCogError(f"Not all solved — missing {len(names)}: " + ", ".join(names))
        if partial and not done:
            raise CodeforcesCogError("You haven't completed your challenge.")
        return done, missing, {a.challenge_id: times[a.challenge_id] for a in done}

    def nogud_set(self, user_id: int) -> Set[str]:
        return cf_common.user_db.get_nogud_problem_keys(user_id)

    @staticmethod
    def _start_of(prob: GitgudProblem) -> float:
        """Contest start for sort keys and the date window; 0 when unknown."""
        if prob.contestId is None:
            return 0
        contest = cf_common.cache2.contest_cache.get_contest(prob.contestId)
        return contest.startTimeSeconds

    def _extra(self, handle: str, *, exclude_nonstandard: bool = True):
        def predicate(prob: GitgudProblem) -> bool:
            if prob.contestId is None:
                return False
            if exclude_nonstandard and cf_common.is_nonstandard_problem(prob):
                return False
            return not cf_common.is_contest_writer(prob.contestId, handle)
        return predicate

    def _pool(self, req: GitgudArgs, solved: Set[str], noguds: Set[str],
              handle: str, exclude_nonstandard: bool) -> List[GitgudProblem]:
        return run_pool(
            cf_common.cache2.problem_cache.problems, req, solved, noguds,
            start_of=self._start_of,
            extra=self._extra(handle, exclude_nonstandard=exclude_nonstandard))

    def select(self, req: GitgudArgs, solved: Set[str], noguds: Set[str],
               handle: str, *, hint_climb: bool = False,
               exclude_nonstandard: bool = True
               ) -> Sequence[GitgudProblem]:
        """Shared ``;gitgud``/``;gimme`` pool; ``hint_climb`` retries higher
        rungs when a default-window request is empty, preserving
        ``exclude_nonstandard`` on every rung. ``;gimme`` passes
        ``exclude_nonstandard=False`` so nonstandard problems stay in its
        pool while ``;gitgud`` and progression exclude them."""
        problems = self._pool(req, solved, noguds, handle, exclude_nonstandard)
        if problems or not hint_climb:
            return problems
        for rating in range(req.erating + _CF_RATING_STEP,
                            _CF_RATING_MAX + 1, _CF_RATING_STEP):
            problems = self._pool(
                replace(req, erating=rating), solved, noguds, handle,
                exclude_nonstandard)
            if problems:
                return problems
        return []

    async def fetch_participated(self, handle: str) -> Set[ContestId]:
        resp = await cf.user.rating(handle=handle)
        return {change.contestId for change in resp}

    def select_upsolve_pool(
        self, solved: Set[str], participated: Set[ContestId]
    ) -> Sequence[GitgudProblem]:
        """Unsolved problems from contests the user took part in, sorted by
        difficulty. Empty when nothing fits — the caller raises."""
        problems: List[GitgudProblem] = [prob for prob in cf_common.cache2.problem_cache.problems
                                         if prob.name not in solved and prob.contestId in participated]
        problems.sort(key=_rating_sort_key)
        return problems

    def lookup_problem(self, problem_key: str) -> GitgudProblem:
        return cf_common.cache2.problem_cache.problem_by_name[problem_key]

    def active_url(self, contest_id: ContestId, problem_key: str, p_index: PIndex = None) -> str:
        return f'{cf.CONTEST_BASE_URL}{contest_id}/problem/{p_index}'
