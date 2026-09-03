"""Codeforces gitgud backend — problem selection for the ``cf`` platform.

``_CfBackend`` is a data-only backend for ``GitgudMixin`` (``tle/cogs/_gitgud.py``):
it resolves handles, fetches ratings/submissions and selects problem pools, then
returns plain problems for the generic layer to send. It never sends messages,
builds embeds, or writes challenges.

``CodeforcesGitgudMixin`` is re-exported here (from ``_gitgud.py``) so the
``Codeforces`` cog and the gitgud tests keep their existing imports.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from tle.util import codeforces_api as cf
from tle.util import codeforces_common as cf_common
from tle.cogs._gitgud import GitgudMixin
from tle.cogs._gitgud_protocol import ActiveChallenge, ContestId, GitgudProblem, PIndex
from tle.cogs._codeforces_helpers import (
    _checkGitgudTags,
    _GITGUD_CLAIM_MARGIN,
    _MULTIWORD_TAG_HINT,
    _parseGitgudRatingArgs,
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


class CodeforcesGitgudMixin(GitgudMixin):
    """Marker subclass so the ``Codeforces`` cog and the gitgud tests can
    inherit the generic implementation under the original Codeforces name."""
    pass


def _cfTagVocabulary() -> Set[str]:
    """Every tag string on any cached Codeforces problem, including the
    division tags the cache synthesizes (div1..div4, edu)."""
    return {tag for prob in cf_common.cache2.problem_cache.problems
            for tag in prob.tags}


class _CfBackend:
    """Codeforces-flavoured problem acquisition and selection."""

    platform: str = 'cf'
    score_model = CF_SCORE_MODEL

    def parse_args(
        self, args: Sequence[str], rating: int
    ) -> Tuple[int, int, bool, List[str], List[str]]:
        """Parse gitgud args: an optional rating or range plus optional
        ``+``/``~`` tag and division filters. ``rating`` is the 800-3500-clamped
        user rating used as the default range. Returns
        ``(srating, erating, hidden, tags, bantags)``."""
        tags: List[str] = cf_common.parse_tags(args, prefix='+')
        bantags: List[str] = cf_common.parse_tags(args, prefix='~')
        error = ('Wrong rating requested. Remember gitgud now uses rating '
                 '(800-3500) instead of delta.')
        srating, erating, hidden = _parseGitgudRatingArgs(
            args, rating, error, bounds=(800, 3500),
            junk_hint=_MULTIWORD_TAG_HINT)
        _checkGitgudTags(tags, bantags, _cfTagVocabulary())
        return srating, erating, hidden, tags, bantags

    async def resolve_handle(self, ctx: GitgudCtx, converter: GitgudConverter) -> str:
        handle, = await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))
        return handle

    async def validate_handle(self, ctx: GitgudCtx, converter: GitgudConverter) -> None:
        # ;nogud re-validates the invoker's CF handle before allowing a skip.
        await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))

    async def fetch_rating(self, handle: str) -> int:
        user = cf_common.user_db.fetch_cf_user(handle)
        assert user is not None
        return round(user.effective_rating, -2)

    def scale_rating(self, rating: int) -> Tuple[int, int]:
        # user_rating clamps the search range default; delta_base clamps the
        # rating difference used to award points.
        user_rating = max(800, min(3500, rating))
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
        """First AC epoch per active ``challenge_id``.

        Single ``user.status`` call per claim:
        only ``OK`` verdicts at/after ``issue_time`` minus a margin. ``creationTimeSeconds``
        is a UTC epoch compared directly against the stored epoch
        ``issue_time`` — no timezone conversion needed. Problems match on
        ``(contestId, index)`` (normalized to strings, index uppercased), with
        a name fallback only for legacy rows lacking an id. Any API failure raises
        ``CodeforcesCogError`` so the claim fails closed instead of falling
        back to wall-clock time.
        """
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
                # Fresh rows must id-match: a same-name solve in another
                # contest must not credit the assigned problem.
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
        """Batch-aware claim check; singletons share the single-claim path (flag inert).

        Returns ``(done, missing, solve_times)`` preserving batch order, where
        ``solve_times`` maps done ``challenge_id`` to its first AC
        epoch. One ``user.status`` call covers the whole batch."""
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

    def select_pool(
        self,
        srating: int,
        erating: int,
        solved: Set[str],
        noguds: Set[str],
        tags: List[str],
        bantags: List[str],
        handle: str,
    ) -> List[GitgudProblem]:
        """Filter the CF problem cache by rating range, solved/nogud sets and
        tag filters; excludes nonstandard problems and problems the user wrote.
        Returns a pool sorted by contest start time. Empty when nothing fits —
        the caller raises 'No problem to assign'."""
        problems: List[GitgudProblem] = [prob for prob in cf_common.cache2.problem_cache.problems
                                          if prob.rating is not None
                                          and prob.rating >= srating and prob.rating <= erating
                                          and prob.contestId is not None
                                          and prob.name not in solved
                                          and prob.name not in noguds
                                          and prob.matches_all_tags(tags)
                                          and not prob.matches_any_tag(bantags)]
        problems = [prob for prob in problems
                    if (not cf_common.is_nonstandard_problem(prob) and
                        not cf_common.is_contest_writer(prob.contestId, handle))]
        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)
        return problems

    async def fetch_participated(self, handle: str) -> Set[int]:
        resp = await cf.user.rating(handle=handle)
        return {change.contestId for change in resp}

    def select_upsolve_pool(
        self, solved: Set[str], participated: Set[int]
    ) -> List[GitgudProblem]:
        """Unsolved problems from contests the user took part in, sorted by
        difficulty. Empty when nothing fits — the caller raises."""
        problems: List[GitgudProblem] = [prob for prob in cf_common.cache2.problem_cache.problems
                                          if prob.name not in solved and prob.contestId in participated]
        problems.sort(key=_rating_sort_key)
        return problems

    def select_gimme_pool(
        self, args: Sequence[str], handle: str, solved: Set[str], rating: int
    ) -> Tuple[List[GitgudProblem], List[str], bool]:
        """Parse gimme args (tags, bans, date range, optional rating) and
        return ``(problems, tags, hidden)`` — a pool sorted by contest start,
        the tags to display, and whether the rating is hidden (a range was
        requested). ``rating`` is the rounded effective rating used as the
        default range."""
        tags: List[str] = cf_common.parse_tags(args, prefix='+')
        bantags: List[str] = cf_common.parse_tags(args, prefix='~')

        srating, erating, _ = _parseGitgudRatingArgs(
            args, rating, 'Wrong rating requested.',
            junk_hint=_MULTIWORD_TAG_HINT)
        _checkGitgudTags(tags, bantags, _cfTagVocabulary())
        dlo, dhi = cf_common.parse_daterange(args)

        problems: List[GitgudProblem] = [prob for prob in cf_common.cache2.problem_cache.problems
                                         if prob.rating is not None
                                         and prob.rating >= srating and prob.rating <= erating and prob.name not in solved
                                         and not cf_common.is_contest_writer(prob.contestId, handle)
                                         and prob.matches_all_tags(tags)
                                         and not prob.matches_any_tag(bantags)
                                         and dlo <= cf_common.cache2.contest_cache.get_contest(
                                             prob.contestId).startTimeSeconds < dhi]
        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)
        return problems, tags, srating != erating

    def lookup_problem(self, problem_key: str) -> GitgudProblem:
        # Challenge rows are keyed by problem name on Codeforces.
        return cf_common.cache2.problem_cache.problem_by_name[problem_key]

    def active_url(self, contest_id: ContestId, problem_key: str, p_index: PIndex = None) -> str:
        # The problem index is stored on every row (legacy column, kept
        # filled), so the URL is built from row data alone with no cache
        # access — a challenge stays linkable even if the cache misses the
        # problem.
        return f'{cf.CONTEST_BASE_URL}{contest_id}/problem/{p_index}'
