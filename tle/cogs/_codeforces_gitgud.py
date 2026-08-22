"""Codeforces gitgud backend — problem selection for the ``cf`` platform.

``_CfBackend`` is a data-only backend for ``GitgudMixin`` (``tle/cogs/_gitgud.py``):
it resolves handles, fetches ratings/submissions and selects problem pools, then
returns plain problems for the generic layer to send. It never sends messages,
builds embeds, or writes challenges.

``CodeforcesGitgudMixin`` is re-exported here (from ``_gitgud.py``) so the
``Codeforces`` cog and the gitgud tests keep their existing imports.
"""
from tle.util import codeforces_api as cf
from tle.util import codeforces_common as cf_common
from tle.cogs._gitgud import GitgudMixin
from tle.cogs._codeforces_helpers import (
    _gitgudTagPenaltyScore,
    _gitgudPenalisedTagCount,
    _checkGitgudTags,
    _MULTIWORD_TAG_HINT,
    _parseGitgudRatingArgs,
    CodeforcesCogError,
)


class CodeforcesGitgudMixin(GitgudMixin):
    """Marker subclass so the ``Codeforces`` cog and the gitgud tests can
    inherit the generic implementation under the original Codeforces name."""
    pass


def _cfTagVocabulary():
    """Every tag string on any cached Codeforces problem, including the
    division tags the cache synthesizes (div1..div4, edu)."""
    return {tag for prob in cf_common.cache2.problem_cache.problems
            for tag in prob.tags}


class _CfBackend:
    """Codeforces-flavoured problem acquisition and selection."""

    platform = 'cf'

    def parse_args(self, args, rating):
        """Parse gitgud args: an optional rating or range plus optional
        ``+``/``~`` tag and division filters. ``rating`` is the 800-3500-clamped
        user rating used as the default range. Returns
        ``(srating, erating, hidden, tags, bantags)``."""
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')
        error = ('Wrong rating requested. Remember gitgud now uses rating '
                 '(800-3500) instead of delta.')
        srating, erating, hidden = _parseGitgudRatingArgs(
            args, rating, error, bounds=(800, 3500),
            junk_hint=_MULTIWORD_TAG_HINT)
        _checkGitgudTags(tags, bantags, _cfTagVocabulary())
        return srating, erating, hidden, tags, bantags

    async def resolve_handle(self, ctx, converter):
        handle, = await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))
        return handle

    async def validate_handle(self, ctx, converter):
        # ;nogud re-validates the invoker's CF handle before allowing a skip.
        await cf_common.resolve_handles(
            ctx, converter, ('!' + str(ctx.message.author.id),))

    async def fetch_rating(self, handle):
        user = cf_common.user_db.fetch_cf_user(handle)
        return round(user.effective_rating, -2)

    def scale_rating(self, rating):
        # user_rating clamps the search range default; delta_base clamps the
        # rating difference used to award points.
        user_rating = max(800, min(3500, rating))
        delta_base = max(1100, min(3000, user_rating))
        return user_rating, delta_base

    async def fetch_solved(self, handle, *, only_ac=True):
        submissions = await cf.user.status(handle=handle)
        if only_ac:
            return {sub.problem.name for sub in submissions if sub.verdict == 'OK'}
        return {sub.problem.name for sub in submissions}

    async def verify_claim(self, ctx, handle, active, submission_url=None):
        """Codeforces claims stay API-based; any pasted link is ignored."""
        solved = await self.fetch_solved(handle)
        if active[2] not in solved:
            raise CodeforcesCogError('You haven\'t completed your challenge.')

    def nogud_set(self, user_id):
        return cf_common.user_db.get_nogud_problem_keys(user_id)

    def select_pool(self, srating, erating, solved, noguds, tags, bantags, handle):
        """Filter the CF problem cache by rating range, solved/nogud sets and
        tag filters; excludes nonstandard problems and problems the user wrote.
        Returns a pool sorted by contest start time. Empty when nothing fits —
        the caller raises 'No problem to assign'."""
        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.rating >= srating and prob.rating <= erating
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

    def delta(self, problem, base, tags=(), bantags=()):
        """Return ``(rating_delta, score)`` for the challenge.

        ``rating_delta`` stays raw (``problem.rating - base``) — the penalty
        only affects the score, which is stored in the ``score`` column.
        """
        delta = problem.rating - base
        return delta, _gitgudTagPenaltyScore(
            delta, _gitgudPenalisedTagCount(tags, bantags))

    async def fetch_participated(self, handle):
        resp = await cf.user.rating(handle=handle)
        return {change.contestId for change in resp}

    def select_upsolve_pool(self, solved, participated):
        """Unsolved problems from contests the user took part in, sorted by
        difficulty. Empty when nothing fits — the caller raises."""
        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.name not in solved and prob.contestId in participated]
        problems.sort(key=lambda problem: problem.rating)
        return problems

    def select_gimme_pool(self, args, handle, solved, rating):
        """Parse gimme args (tags, bans, date range, optional rating) and
        return ``(problems, tags, hidden)`` — a pool sorted by contest start,
        the tags to display, and whether the rating is hidden (a range was
        requested). ``rating`` is the rounded effective rating used as the
        default range."""
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')

        srating, erating, _ = _parseGitgudRatingArgs(
            args, rating, 'Wrong rating requested.',
            junk_hint=_MULTIWORD_TAG_HINT)
        _checkGitgudTags(tags, bantags, _cfTagVocabulary())
        dlo, dhi = cf_common.parse_daterange(args)

        problems = [prob for prob in cf_common.cache2.problem_cache.problems
                    if prob.rating >= srating and prob.rating <= erating and prob.name not in solved
                    and not cf_common.is_contest_writer(prob.contestId, handle)
                    and prob.matches_all_tags(tags)
                    and not prob.matches_any_tag(bantags)
                    and dlo <= cf_common.cache2.contest_cache.get_contest(
                        prob.contestId).startTimeSeconds < dhi]
        problems.sort(key=lambda problem: cf_common.cache2.contest_cache.get_contest(
            problem.contestId).startTimeSeconds)
        return problems, tags, srating != erating

    def lookup_problem(self, problem_key):
        # Challenge rows are keyed by problem name on Codeforces.
        return cf_common.cache2.problem_cache.problem_by_name[problem_key]

    def active_url(self, contest_id, problem_key, p_index=None):
        # The problem index is stored on every row (legacy column, kept
        # filled), so the URL is built from row data alone with no cache
        # access — a challenge stays linkable even if the cache misses the
        # problem.
        return f'{cf.CONTEST_BASE_URL}{contest_id}/problem/{p_index}'

    def rating_of(self, problem):
        return problem.rating

    def contest_name_of(self, problem):
        return cf_common.cache2.contest_cache.get_contest(problem.contestId).name
