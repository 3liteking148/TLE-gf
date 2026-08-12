"""AtCoder problem cache built from kenkoooo's static datasets.

There is no official AtCoder API, so the gitgud pool is served from the
community-maintained AtCoder Problems datasets (``atcoder_api``). The cache
is memory-only in this version: the two JSON datasets (~6 MB) are refetched
on startup and every ``_RELOAD_INTERVAL``, which also self-heals failures.
"""
import asyncio
import logging
import time

from tle.util import atcoder_api
from tle.util import tasks


class AtcoderProblemCache:
    """Mirrors ``ProblemCache`` but for AtCoder problems.

    Exposes ``problems`` (AtCoderProblems with difficulty, sorted by contest
    start time) and ``problem_by_id``. Problems without a difficulty model and
    heuristic (``ahc``) contests are excluded from the pool.
    """

    _RELOAD_INTERVAL = 6 * 60 * 60

    def __init__(self):
        self.problems = []
        self.problem_by_id = {}
        self.problems_last_cache = 0

        self.reload_lock = asyncio.Lock()

        self.logger = logging.getLogger(self.__class__.__name__)

    async def run(self):
        self._update_task.start()

    @tasks.task_spec(name='AtcoderProblemCacheUpdate',
                     waiter=tasks.Waiter.fixed_delay(_RELOAD_INTERVAL))
    async def _update_task(self, _):
        async with self.reload_lock:
            await self._reload()

    async def _reload(self):
        problems = await atcoder_api.get_problems()
        models = await atcoder_api.get_problem_models()
        contests = await atcoder_api.get_contests()
        if problems is None or models is None or contests is None:
            raise RuntimeError('AtCoder datasets unavailable')
        await self._update(problems, models, contests)

    def _is_heuristic(self, contest_id):
        return contest_id.startswith('ahc')

    def _merge(self, problems, models, contests):
        """Merge the three datasets into a pool of rated, non-heuristic
        problems sorted by contest start, keyed by problem id."""
        contest_by_id = {contest.id: contest for contest in contests.values()}
        pool = []
        for problem in problems:
            if self._is_heuristic(problem.contest_id):
                continue
            difficulty = models.get(problem.id)
            if difficulty is None:
                continue
            contest = contest_by_id.get(problem.contest_id)
            if contest is None:
                continue
            pool.append(atcoder_api.AtCoderProblem(
                problem.id, problem.contest_id, problem.problem_index,
                problem.title, difficulty, contest.start_epoch_second,
                contest.title))
        return pool

    async def _update(self, problems, models, contests):
        pool = self._merge(problems, models, contests)
        pool.sort(key=lambda problem: (problem.contest_start, problem.id))
        problem_by_id = {problem.id: problem for problem in pool}
        self.logger.info(f'Keeping {len(problem_by_id)} AtCoder problems')

        self.problems = pool
        self.problem_by_id = problem_by_id
        self.problems_last_cache = time.time()
