"""AtCoder gitgud backend — problem selection for the ``ac`` platform.

AtCoder has no official API, so problem data comes from the kenkoooo datasets
(via ``atcoder_api``) and the ``AtcoderProblemCache``. ``_AcBackend`` is a
data-only backend for ``GitgudMixin`` (``tle/cogs/_gitgud.py``): it resolves
per-guild handles, fetches ratings/submissions and selects problem pools, then
returns plain problems for the generic layer to send. It never sends messages,
builds embeds, or writes challenges. Upsolve/gimme are not implemented for
AtCoder and raise explicitly.

``AtcoderGitgudMixin`` is re-exported here (from ``_gitgud.py``) so the
``Codeforces`` cog and the gitgud tests keep their existing imports.
"""
from tle.util import atcoder_api
from tle.util import codeforces_common as cf_common
from tle.cogs._gitgud import GitgudMixin
from tle.cogs._codeforces_helpers import CodeforcesCogError


class AtcoderGitgudMixin(GitgudMixin):
    """Marker subclass so the ``Codeforces`` cog and the gitgud tests can
    inherit the generic implementation under the original AtCoder name."""
    pass


class _AcBackend:
    """AtCoder-flavoured problem acquisition and selection."""

    platform = 'ac'

    def parse_args(self, args, rating):
        """Parse gitgud args in AtCoder mode: an optional rating or range.

        Tags, banned tags and division filters do not exist on AtCoder and are
        rejected. ``rating`` is the 0-4000-clamped user rating used as the
        default range. Returns ``(srating, erating, hidden, None, None)``."""
        srating = erating = None
        hidden = False
        for arg in args:
            if arg.startswith('+') or arg.startswith('~'):
                raise CodeforcesCogError(
                    'Tags and division filters are not supported for AtCoder '
                    'gitgud.')
            if arg.startswith('-'):
                raise CodeforcesCogError(
                    'Wrong rating requested. AtCoder gitgud uses rating '
                    f'({atcoder_api.RATING_MIN}-{atcoder_api.RATING_MAX}).')
            if arg[0:3].isdigit():
                ratings = arg.split('-')
                srating = int(ratings[0])
                if len(ratings) > 1:
                    erating = int(ratings[1])
                    hidden = True
                else:
                    erating = srating
        if srating is None:
            srating = max(rating - 100, atcoder_api.RATING_MIN)
            erating = min(rating + 100, atcoder_api.RATING_MAX)
        if erating < atcoder_api.RATING_MIN or srating > atcoder_api.RATING_MAX:
            raise CodeforcesCogError(
                'Wrong rating requested. AtCoder gitgud uses rating '
                f'({atcoder_api.RATING_MIN}-{atcoder_api.RATING_MAX}).')
        return srating, erating, hidden, None, None

    async def resolve_handle(self, ctx, converter=None):
        user_id = ctx.message.author.id
        if ctx.guild is None:
            raise CodeforcesCogError(
                'AtCoder handles are per-server; run `;atcoder identify '
                '<handle>` in a server first.')
        handle = cf_common.user_db.get_atcoder_handle(user_id, ctx.guild.id)
        if handle is None:
            raise CodeforcesCogError(
                f'No AtCoder handle found for you. Link one with '
                '`;atcoder identify <handle>`.')
        return handle

    async def validate_handle(self, ctx, converter=None):
        # AtCoder skips need no handle re-validation.
        return None

    async def fetch_rating(self, handle):
        user = await atcoder_api.get_user(handle)
        if user is None:
            raise CodeforcesCogError(f'AtCoder user `{handle}` not found')
        rating = atcoder_api.parse_rating(user.rating)
        if rating is None:
            return atcoder_api.RATING_MIN
        return rating

    def scale_rating(self, rating):
        scaled = max(atcoder_api.RATING_MIN,
                     min(atcoder_api.RATING_MAX, rating))
        return scaled, scaled

    async def fetch_solved(self, handle, *, only_ac=True):
        submissions = await atcoder_api.get_user_submissions(handle)
        if submissions is None:
            raise CodeforcesCogError(
                'Could not fetch your AtCoder submissions. Try again in a '
                'moment.')
        if not only_ac:
            return {s.problem_id for s in submissions}
        return {s.problem_id for s in submissions if s.is_ac}

    async def verify_claim(self, ctx, handle, active, submission_url=None):
        """Verify the invoker's pasted submission link proves the challenge
        is solved.

        Scrapes the single submission detail page from atcoder.jp instead of
        polling kenkoooo's lagging per-user API, so a claim works the moment
        the verdict is out. Raises ``CodeforcesCogError`` naming the failing
        check on any mismatch.

        Known gap (deliberate): the page's submission time is not checked
        against the challenge issue time. kenkoooo's solved-set lags AtCoder,
        so a problem issued after the user solved it (but before kenkoooo
        caught up) is legitimately claimable; that same lag window also lets
        a user solve a problem, get it issued, and claim with a pre-issue AC.
        If this ever needs closing, parse the ``Submission Time`` row
        (``<time class='fixtime fixtime-second'>``, JST) and reject
        submissions earlier than ``active[1]``; the offset needs normalizing
        to ``+09:00`` for Python <3.11 ``fromisoformat``.
        """
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
        # AtCoder problem ids are contest-prefixed (abc383_a), so a contest
        # mismatch is a guaranteed wrong-problem claim; fail before scraping.
        if parsed[0] != active[3]:
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
        # AtCoder user URLs are case-insensitive, so compare normalized.
        if submission.handle.lower() != handle.lower():
            raise CodeforcesCogError(
                'That submission is not from your linked AtCoder account '
                f'`{handle}`.')
        if submission.problem_id != active[2]:
            raise CodeforcesCogError(
                'That submission is for a different problem than your '
                'challenge.')

    def nogud_set(self, user_id):
        return cf_common.user_db.get_nogud_problem_keys(user_id)

    def select_pool(self, srating, erating, solved, noguds, tags, bantags, handle):
        """Filter the AtCoder problem cache by difficulty range and the
        solved/nogud sets; sorted by contest start. Empty when nothing fits —
        the caller raises 'No problem to assign'."""
        problems = [prob for prob in cf_common.cache2.atcoder_problem_cache.problems
                    if prob.difficulty >= srating and prob.difficulty <= erating
                    and prob.id not in solved and prob.id not in noguds and ('abc' in prob.contestId or 'arc' in prob.contestId)]
        problems.sort(key=lambda problem: problem.contest_start)
        return problems

    def delta(self, problem, base, tags=(), bantags=()):
        return problem.difficulty - base

    async def fetch_participated(self, handle):
        raise CodeforcesCogError(
            ';upsolve is not implemented for AtCoder yet')

    def select_upsolve_pool(self, solved, participated):
        raise CodeforcesCogError(
            ';upsolve is not implemented for AtCoder yet')

    def select_gimme_pool(self, args, handle, solved, rating):
        raise CodeforcesCogError(
            ';gimme is not implemented for AtCoder yet')

    def lookup_problem(self, problem_key):
        # Challenge rows are keyed by problem id on AtCoder.
        return cf_common.cache2.atcoder_problem_cache.problem_by_id[problem_key]

    def active_url(self, contest_id, problem_key, p_index=None):
        # The key is the problem id, so the URL is built from row data alone
        # with no cache access. p_index (the letter after the underscore) is
        # unused here — AtCoder task URLs need the full problem id.
        return f'{atcoder_api.BASE_URL}/contests/{contest_id}/tasks/{problem_key}'

    def rating_of(self, problem):
        return problem.difficulty

    def contest_name_of(self, problem):
        return problem.contest_name