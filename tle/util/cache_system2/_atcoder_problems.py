"""AtCoder problem cache built from kenkoooo's static datasets.

There is no official AtCoder API, so the gitgud pool is served from the
community-maintained AtCoder Problems datasets (``atcoder_api``). The cache
is memory-only in this version: the two JSON datasets (~6 MB) are refetched
on startup and every ``_RELOAD_INTERVAL``, which also self-heals failures.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from tle.util import atcoder_api
from tle.util import tasks
from tle.util._atcoder_api_types import AtCoderContest, AtCoderProblem


class AtcoderProblemCache:
    """Mirrors ``ProblemCache`` but for AtCoder problems.

    Exposes ``problems`` (AtCoderProblems with difficulty, sorted by contest
    start time) and ``problem_by_id``. Problems without a difficulty model and
    heuristic (``ahc``) contests are excluded from the pool.
    """

    _RELOAD_INTERVAL = 6 * 60 * 60

    def __init__(self) -> None:
        self.problems: List[AtCoderProblem] = []
        self.problem_by_id: Dict[str, AtCoderProblem] = {}
        self.problems_last_cache: float = 0

        self.reload_lock: asyncio.Lock = asyncio.Lock()

        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self) -> None:
        self._update_task.start()

    @tasks.task_spec(name='AtcoderProblemCacheUpdate',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_INTERVAL))
    async def _update_task(self, _: object) -> None:
        async with self.reload_lock:
            await self._reload()

    async def _reload(self) -> None:
        problems: Optional[List[AtCoderProblem]] = await atcoder_api.get_problems()
        models: Optional[Dict[str, int]] = await atcoder_api.get_problem_models()
        contests: Optional[Dict[str, AtCoderContest]] = await atcoder_api.get_contests()
        if problems is None or models is None or contests is None:
            raise RuntimeError('AtCoder datasets unavailable')
        await self._update(problems, models, contests)

    def _is_heuristic(self, contest_id: str) -> bool:
        return contest_id.startswith('ahc')

    def _merge(
        self,
        problems: List[AtCoderProblem],
        models: Dict[str, int],
        contests: Dict[str, AtCoderContest],
    ) -> List[AtCoderProblem]:
        """Merge the three datasets into a pool of rated, non-heuristic
        problems sorted by contest start, keyed by problem id."""
        contest_by_id: Dict[str, AtCoderContest] = {contest.id: contest for contest in contests.values()}
        pool: List[AtCoderProblem] = []
        for problem in problems:
            if self._is_heuristic(problem.contestId):
                continue
            difficulty: Optional[int] = models.get(problem.id)
            if difficulty is None:
                continue
            contest: Optional[AtCoderContest] = contest_by_id.get(problem.contestId)
            if contest is None:
                continue
            pool.append(atcoder_api.AtCoderProblem(
                problem.id, problem.contestId, contest.title, problem.problem_index,
                problem.name, difficulty, contest.start_epoch_second))
        return pool

    async def _update(
        self,
        problems: List[AtCoderProblem],
        models: Dict[str, int],
        contests: Dict[str, AtCoderContest],
    ) -> None:
        pool: List[AtCoderProblem] = self._merge(problems, models, contests)
        pool.sort(key=lambda problem: (problem.contest_start if problem.contest_start is not None else 0, problem.id))
        problem_by_id: Dict[str, AtCoderProblem] = {problem.id: problem for problem in pool}
        self.logger.info(f'Keeping {len(problem_by_id)} AtCoder problems')

        self.problems = pool
        self.problem_by_id = problem_by_id
        self.problems_last_cache = time.time()
