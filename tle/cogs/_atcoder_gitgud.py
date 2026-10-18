"""AtCoder gitgud backend — problem selection for the ``ac`` platform."""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING, cast

from tle.util import atcoder_api
from tle.util import codeforces_common as cf_common
from tle.cogs._gitgud_args import GITGUD_USAGE, GitgudArgs, parse_args
from tle.cogs._gitgud_backend import run_pool
from tle.cogs._gitgud_protocol import ActiveChallenge, ContestId, GitgudProblem, PIndex
from tle.cogs._codeforces_helpers import CodeforcesCogError
from tle.cogs._gitgud_scoring import AC_SCORE_MODEL
from tle.util._atcoder_api_types import AtCoderProblem

if TYPE_CHECKING:
    from discord.ext.commands import Context as DiscordContext
    from discord.ext.commands import Converter as DiscordConverter
    GitgudCtx = DiscordContext
    GitgudConverter = DiscordConverter
else:
    GitgudCtx = Any
    GitgudConverter = Any


def _widen_default(req: GitgudArgs) -> GitgudArgs:
    """AtCoder ratings are not rounded to hundreds, so a single-valued window
    asks for exactly one difficulty: widen it by ±100. This covers the bare
    default, an explicit single rating, and an equal-range spec like
    ``1200-1200``. A genuine ``lo-hi`` range (distinct ends) passes through
    untouched."""
    if req.srating != req.erating:
        return req
    return replace(
        req,
        srating=max(req.srating - 100, atcoder_api.RATING_MIN),
        erating=min(req.erating + 100, atcoder_api.RATING_MAX))


class _AcBackend:
    """AtCoder-flavoured problem acquisition and selection."""

    platform: str = 'ac'
    score_model = AC_SCORE_MODEL
    exact: bool = True

    def tag_vocabulary(self) -> Optional[Set[str]]:
        try:
            return {prob.contest_type for prob
                    in cf_common.cache2.atcoder_problem_cache.problems}
        except AttributeError:
            return None

    def parse_request(self, args: Sequence[str], rating: int,
                      usage: str = GITGUD_USAGE) -> GitgudArgs:
        req = parse_args(
            args, default_rating=rating,
            bounds=(atcoder_api.RATING_MIN, atcoder_api.RATING_MAX),
            vocabulary=self.tag_vocabulary(), exact=True, usage=usage)
        return _widen_default(req)

    async def resolve_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> str:
        user_id: int = ctx.message.author.id
        if ctx.guild is None:
            raise CodeforcesCogError(
                'AtCoder handles are per-server; run `;atcoder identify '
                '<handle>` in a server first.')
        handle: Optional[str] = cf_common.user_db.get_atcoder_handle(user_id, ctx.guild.id)
        if handle is None:
            raise CodeforcesCogError(
                f'No AtCoder handle found for you. Link one with '
                '`;atcoder identify <handle>`.')
        return handle

    async def validate_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> None:
        return None

    async def fetch_rating(self, handle: str) -> int:
        user = await atcoder_api.get_user(handle)
        if user is None:
            raise CodeforcesCogError(f'AtCoder user `{handle}` not found')
        rating: Optional[int] = atcoder_api.parse_rating(user.rating)
        if rating is None:
            return atcoder_api.RATING_MIN
        return rating

    def scale_rating(self, rating: int) -> Tuple[int, int]:
        scaled = max(atcoder_api.RATING_MIN,
                     min(atcoder_api.RATING_MAX, rating))
        return scaled, scaled

    async def fetch_solved(self, handle: str, *, only_ac: bool = True) -> Set[str]:
        submissions = await atcoder_api.get_user_submissions(handle)
        if submissions is None:
            raise CodeforcesCogError(
                'Could not fetch your AtCoder submissions. Try again in a '
                'moment.')
        if not only_ac:
            return {s.problem_id for s in submissions}
        return {s.problem_id for s in submissions if s.is_ac}

    async def verify_claims(
        self, ctx: GitgudCtx, handle: str, actives: Sequence[ActiveChallenge], submission_url: Optional[str] = None,
        partial: bool = False
    ) -> Tuple[List[ActiveChallenge], List[ActiveChallenge], Dict[int, float]]:
        if len(actives) != 1:
            raise CodeforcesCogError('AtCoder only supports single claims')
        ts = await self._verify_single_claim(ctx, handle, actives[0], submission_url)
        return [actives[0]], [], {actives[0].challenge_id: ts}

    async def _verify_single_claim(
        self, ctx: GitgudCtx, handle: str, active: ActiveChallenge, submission_url: Optional[str] = None
    ) -> float:
        if submission_url is None:
            raise CodeforcesCogError(
                'Paste your AtCoder submission link after the command, e.g. '
                '`;gotgud https://atcoder.jp/contests/abc383/submissions/'
                '12345678`.')
        parsed = atcoder_api.parse_submission_url(submission_url)
        if parsed is None:
            raise CodeforcesCogError(
                'That does not look like an AtCoder submission link. Paste '
                'the URL of your submission, e.g. '
                '`https://atcoder.jp/contests/abc383/submissions/12345678`.')
        if parsed[0] != active.contest_id:
            raise CodeforcesCogError(
                'That submission is from a different contest than your '
                'challenge.')
        submission = await atcoder_api.get_submission(submission_url)
        if submission is None:
            raise CodeforcesCogError(
                'Could not read that submission. Check the link and try '
                'again in a moment.')
        if submission.verdict == 'WJ':
            raise CodeforcesCogError(
                'Your submission is still being judged. Wait a moment and '
                'try again.')
        if not submission.is_ac:
            raise CodeforcesCogError(
                f'That submission is not accepted (verdict '
                f'{submission.verdict}).')
        if submission.handle.lower() != handle.lower():
            raise CodeforcesCogError(
                'That submission is not from your linked AtCoder account '
                f'`{handle}`.')
        if submission.problem_id != active.problem_key:
            raise CodeforcesCogError(
                'That submission is for a different problem than your '
                'challenge.')
        return time.time()

    def nogud_set(self, user_id: int) -> Set[str]:
        return cf_common.user_db.get_nogud_problem_keys(user_id)

    @staticmethod
    def _start(prob: GitgudProblem) -> float:
        return cast(AtCoderProblem, prob).contest_start or 0

    @staticmethod
    def _family(prob: GitgudProblem) -> bool:
        contest_id = cast(AtCoderProblem, prob).contestId
        return ('abc' in contest_id or 'arc' in contest_id
                or 'agc' in contest_id)

    def select(self, req: GitgudArgs, solved: Set[str], noguds: Set[str],
               handle: str = '', *, hint_climb: bool = False,
               exclude_nonstandard: bool = True
               ) -> Sequence[GitgudProblem]:
        """Shared ``;gitgud``/``;gimme`` pool.

        ``hint_climb`` and ``exclude_nonstandard`` are accepted for protocol
        parity and ignored: AtCoder has no rating ladder and its only
        exclusion is the contest-family gate.
        """
        return run_pool(
            cf_common.cache2.atcoder_problem_cache.problems, req, solved, noguds,
            start_of=self._start, extra=self._family)

    async def fetch_participated(self, handle: str) -> Set[ContestId]:
        raise CodeforcesCogError(
            ';upsolve is not implemented for AtCoder yet')

    def select_upsolve_pool(
        self, solved: Set[str], participated: Set[ContestId]
    ) -> Sequence[GitgudProblem]:
        raise CodeforcesCogError(
            ';upsolve is not implemented for AtCoder yet')

    def lookup_problem(self, problem_key: str) -> GitgudProblem:
        return cf_common.cache2.atcoder_problem_cache.problem_by_id[problem_key]

    def active_url(self, contest_id: ContestId, problem_key: str, p_index: PIndex = None) -> str:
        return f'{atcoder_api.BASE_URL}/contests/{contest_id}/tasks/{problem_key}'
