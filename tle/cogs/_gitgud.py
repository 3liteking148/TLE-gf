"""Platform-agnostic gitgud implementation mixin for the codeforces cog.

Holds every gitgud command body (gitgud/gotgud/nogud/gitlog/nogudlog/upsolve/
gimme) plus the shared helpers (challenge issuing, claiming, coins, more-points
seasons). All Discord interaction — embeds, paginators, ``ctx.send`` and the
challenge-table writes — lives here and nowhere else.

Platform differences are behind two data-only backends (``_CfBackend`` in
``_codeforces_gitgud.py``, ``_AcBackend`` in ``_atcoder_gitgud.py``); they
fetch and select problems and return them, but never build a Discord object.

This is a plain mixin (NOT a ``commands.Cog``); ``Codeforces`` inherits from it
alongside ``commands.Cog``. ``_codeforces_gitgud.py`` and ``_atcoder_gitgud.py``
re-export this class as ``CodeforcesGitgudMixin`` / ``AtcoderGitgudMixin`` so
existing imports (the ``Codeforces`` cog and the gitgud tests) keep working.
"""
import datetime
import random

import discord

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.util.db.user_db_conn import Gitgud
from tle.util import paginator
from tle.cogs._codeforces_helpers import (
    CodeforcesCogError,
    _GITGUD_NO_SKIP_TIME,
    _ONE_WEEK_DURATION,
    _GITGUD_MORE_POINTS_START_TIME,
    _GITGUD_COIN_MULTIPLIER,
)


class GitgudMixin:
    """Generic gitgud command bodies; the per-platform work is delegated to a
    ``_CfBackend``/``_AcBackend`` instance chosen per invocation."""

    # more points seasons start at April 1st 2023 (timestamp: 1680300000) and is only active in the last 7 days of the month
    # @@@ add issue and finish time constraint (both times need to be within the more points range)
    def _check_more_points_active(self, now_time, start_time, end_time):
        morePointsActive = False
        morePointsTime = end_time - _ONE_WEEK_DURATION
        if start_time >= _GITGUD_MORE_POINTS_START_TIME and now_time >= morePointsTime:
            morePointsActive = True
        return morePointsActive

    # ------------------------------------------------------------------
    # Backend selection
    # ------------------------------------------------------------------

    def _backend_for_args(self, args, marker='+atcoder'):
        return self._backend_for_platform('ac' if marker in args else 'cf')

    def _backend_for_platform(self, platform):
        # Lazy imports keep the re-export cycle (the platform files re-export
        # this class) and the 500-line rule both happy; by command time all
        # modules are fully loaded.
        if platform == 'ac':
            from tle.cogs._atcoder_gitgud import _AcBackend
            return _AcBackend()
        from tle.cogs._codeforces_gitgud import _CfBackend
        return _CfBackend()

    # ------------------------------------------------------------------
    # Shared gitgud helpers
    # ------------------------------------------------------------------

    def _award_gitgud_coins(self, ctx, user_id, score):
        """Credit the betting wallet with ``_GITGUD_COIN_MULTIPLIER`` coins per
        base gitgud point. The rate is a flat 5x of the *base* score and never
        gets the end-of-month doubling the monthly ranklist points do. Returns
        the coins awarded, or None when there's no guild (e.g. a DM) so the
        caller can omit the wallet line."""
        guild = ctx.guild
        if guild is None:
            return None
        coins = _GITGUD_COIN_MULTIPLIER * score
        start_balance = (constants.BET_START_BALANCE
                         + cf_common.user_db.bet_get_start_bonus(guild.id))
        cf_common.user_db.bet_adjust_balance(
            guild.id, user_id, coins, start_balance,
            actor_id=user_id, action='gitgud', note=f'score={score}')
        return coins

    def _problem_ref(self, backend, problem_key):
        """``(name, rating, url)`` for a challenge row; falls back to the raw
        key without a link when the cache cannot resolve it — e.g. an AtCoder
        problem that dropped out of kenkoooo's datasets."""
        try:
            problem = backend.lookup_problem(problem_key)
            return problem.name, backend.rating_of(problem), problem.url
        except (KeyError, AttributeError):
            return problem_key, '?', None

    def _active_problem_name(self, backend, problem_key):
        """Pretty problem name for the active-challenge error; falls back to
        the raw key when the cache can't resolve it."""
        return self._problem_ref(backend, problem_key)[0]

    async def _validate_gitgud_status(self, ctx):
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if active is not None:
            _, _, problem_key, contest_id, _, platform, p_index, _ = active
            backend = self._backend_for_platform(platform)
            name = self._active_problem_name(backend, problem_key)
            url = backend.active_url(contest_id, problem_key, p_index)
            raise CodeforcesCogError(f'You have an active challenge {name} at {url}')

    async def _gitgud(self, ctx, handle, problem, delta, score, hidden, backend):
        # The caller of this function is responsible for calling `_validate_gitgud_status` first.
        user_id = ctx.author.id

        issue_time = datetime.datetime.now().timestamp()
        rc = cf_common.user_db.new_challenge(
            user_id, issue_time, problem, delta, score, backend.platform)
        if rc != 1:
            raise CodeforcesCogError('Your challenge has already been added to the database!')

        # Calculate time range of given month (d=) or current month
        now = datetime.datetime.now()
        start_time, end_time = cf_common.get_start_and_end_of_month(now)
        now_time = int(now.timestamp())
        # more points seasons start at April 1st 2023 (timestamp: 1680300000) and is only active in the last 7 days of the month
        morePointsActive = self._check_more_points_active(now_time, start_time, end_time)

        points = score
        monthlypoints = 2 * points if morePointsActive else points

        title = f'{problem.index}. {problem.name}'
        desc = backend.contest_name_of(problem)
        rating = backend.rating_of(problem)
        ratingStr = rating if not hidden else '||' + str(rating) + '||'
        pointsStr = points if not hidden else '||' + str(points) + '||'
        monthlyPointsStr = monthlypoints if not hidden else '||' + str(monthlypoints) + '||'
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=ratingStr)
        embed.add_field(name='Alltime points', value=pointsStr)
        embed.add_field(name='Monthly points', value=monthlyPointsStr)
        await ctx.send(f'Challenge problem for `{handle}`', embed=embed)

    async def _claim_challenge(self, ctx, handle, active):
        """Shared completion tail for both platforms: complete the challenge,
        credit points (with month-end doubling) and award betting coins."""
        user_id = ctx.message.author.id
        challenge_id, issue_time = active[0], active[1]
        score = active[7]
        finish_time = int(datetime.datetime.now().timestamp())
        rc = cf_common.user_db.complete_challenge(user_id, challenge_id, finish_time, score)

        now = datetime.datetime.now()
        start_time, end_time = cf_common.get_start_and_end_of_month(now)
        now_time = int(now.timestamp())

        morePointsActive = self._check_more_points_active(now_time, start_time, end_time)

        monthlyPoints = 2 * score if morePointsActive else score

        if rc == 1:
            duration = cf_common.pretty_time_format(finish_time - issue_time)
            msg = (f'Challenge completed in {duration}. {handle} gained {score} '
                   f'alltime ranklist points and {monthlyPoints} monthly ranklist points.')
            # Coins are always credited to the betting wallet, but we only
            # mention them to users who are already playing the betting game
            # (have placed at least one bet) — same bar as showing up on the
            # ;bet leaderboard. Everyone else just banks them silently.
            coins = self._award_gitgud_coins(ctx, user_id, score)
            if coins is not None and \
                    cf_common.user_db.bet_has_wagered(ctx.guild.id, user_id):
                msg += f' You also earned {coins} 🪙.'
            await ctx.send(msg)
        else:
            await ctx.send('You have already claimed your points')

    # ------------------------------------------------------------------
    # Command bodies
    # ------------------------------------------------------------------

    async def _gitgud_impl(self, ctx, args):
        backend = self._backend_for_args(args)

        args = [arg for arg in args if arg != "+atcoder"]
        
        handle = await backend.resolve_handle(ctx, self.converter)
        user_rating, delta_base = backend.scale_rating(
            await backend.fetch_rating(handle))
        solved = await backend.fetch_solved(handle, only_ac=False)
        noguds = backend.nogud_set(ctx.message.author.id)

        srating, erating, hidden, tags, bantags = backend.parse_args(
            args, user_rating)

        await self._validate_gitgud_status(ctx)

        problems = backend.select_pool(
            srating, erating, solved, noguds, tags, bantags, handle)
        if not problems:
            raise CodeforcesCogError('No problem to assign')

        choice = max(random.randrange(len(problems)) for _ in range(5))

        # Penalised tags divide points by (tag count + 1), rounded up.
        # Hardening division filters such as +div1 and ~div3/~div4/~edu are
        # exempt; other requested tags and bans count (see
        # _gitgudPenalisedTagCount). The raw delta is stored untouched; the
        # (possibly off-ladder) score goes into its own column. AtCoder has no
        # tags, so its delta stays raw too.
        problem = problems[choice]
        delta, score = backend.delta(problem, delta_base, tags, bantags)
        await self._gitgud(ctx, handle, problem, delta, score, hidden, backend)

    async def _gotgud_impl(self, ctx, submission_url=None):
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError(f'You do not have an active challenge')

        backend = self._backend_for_platform(active[5])
        handle = await backend.resolve_handle(ctx, self.converter)
        await backend.verify_claim(ctx, handle, active, submission_url)

        await self._claim_challenge(ctx, handle, active)

    async def _nogud_impl(self, ctx):
        user_id = ctx.message.author.id
        active = cf_common.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError(f'You do not have an active challenge')

        backend = self._backend_for_platform(active[5])
        await backend.validate_handle(ctx, self.converter)

        challenge_id, issue_time = active[0], active[1]
        finish_time = int(datetime.datetime.now().timestamp())
        if finish_time - issue_time < _GITGUD_NO_SKIP_TIME:
            skip_time = cf_common.pretty_time_format(issue_time + _GITGUD_NO_SKIP_TIME - finish_time)
            await ctx.send(f'Think more. You can skip your challenge in {skip_time}.')
            return
        cf_common.user_db.skip_challenge(user_id, challenge_id, Gitgud.NOGUD)
        await ctx.send(f'Challenge skipped.')

    async def _force_nogud_impl(self, ctx, member):
        active = cf_common.user_db.check_challenge(member.id)
        if not active:
            await ctx.send(f'No active challenge found for user `{member.display_name}`.')
            return
        rc = cf_common.user_db.skip_challenge(member.id, active[0], Gitgud.FORCED_NOGUD)
        if rc == 1:
            await ctx.send(f'Challenge skip forced.')
        else:
            await ctx.send(f'Failed to force challenge skip.')

    async def _gitlog_impl(self, ctx, member):
        def make_line(entry):
            issue, finish, problem_key, _, _, platform, score = entry
            name, rating, url = self._problem_ref(
                self._backend_for_platform(platform), problem_key)
            line = f'[{name}]({url})\N{EN SPACE}[{rating}]' if url else f'`{name}`\N{EN SPACE}[{rating}]'
            if finish:
                time_str = cf_common.days_ago(finish)
                points = f'{int(score):+}'
                line += f'\N{EN SPACE}{time_str}\N{EN SPACE}[{points}]'
            return line

        def make_page(chunk,score):
            message = discord.utils.escape_mentions(f'Gitgud log for {member.display_name} (total score: {score})')
            log_str = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        member = member or ctx.author
        data = cf_common.user_db.gitlog(member.id)
        if not data:
            raise CodeforcesCogError(f'{member.mention} has no gitgud history.')
        score = 0
        for entry in data:
            if entry[1]:
                score += int(entry[6])


        pages = [make_page(chunk, score) for chunk in paginator.chunkify(data, 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, author_id=ctx.author.id)

    async def _nogudlog_impl(self, ctx, member):
        def make_line(entry):
            issue, finish, problem_key, _, _, platform, _ = entry
            name, rating, url = self._problem_ref(
                self._backend_for_platform(platform), problem_key)
            line = f'[{name}]({url})\N{EN SPACE}[{rating}]' if url else f'`{name}`\N{EN SPACE}[{rating}]'
            if finish:
                time_str = cf_common.days_ago(finish)
                points = f'{int(entry[6]):+}'
                line += f'\N{EN SPACE}{time_str}\N{EN SPACE}[{points}]'
            return line

        def make_page(chunk):
            message = discord.utils.escape_mentions(f'Nogud log for {member.display_name}')
            log_str = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        member = member or ctx.author
        data = cf_common.user_db.gitlog(member.id)
        if not data:
            raise CodeforcesCogError(f'{member.mention} has no gitgud history.')

        data = [entry for entry in data if entry[1] is None]

        pages = [make_page(chunk) for chunk in paginator.chunkify(data, 10)]
        paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, author_id=ctx.author.id)

    async def _upsolve_impl(self, ctx, args):
        choice = -1
        platform_args = []
        for arg in args:
            if arg.startswith('+'):
                platform_args.append(arg)
                continue
            try:
                choice = int(arg)
            except ValueError:
                raise CodeforcesCogError(f'Invalid choice `{arg}`.')

        backend = self._backend_for_args(platform_args)
        handle = await backend.resolve_handle(ctx, self.converter)
        _, delta_base = backend.scale_rating(await backend.fetch_rating(handle))
        participated = await backend.fetch_participated(handle)
        solved = await backend.fetch_solved(handle)
        problems = backend.select_upsolve_pool(solved, participated)

        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        if choice > 0 and choice <= len(problems):
            await self._validate_gitgud_status(ctx)
            problem = problems[choice - 1]
            delta, score = backend.delta(problem, delta_base, (), ())
            await self._gitgud(ctx, handle, problem, delta, score,
                               False, backend)
        else:
            problems = problems[:500]

            def make_line(i, prob):
                data = (f'{i + 1}: [{prob.name}]({prob.url}) [{backend.rating_of(prob)}]')
                return data

            def make_page(chunk, pi, num):
                title = f'Select a problem to upsolve (1-{num}):'
                msg = '\n'.join(make_line(10*pi+i, prob) for i, prob in enumerate(chunk))
                embed = discord_common.cf_color_embed(description=msg)
                return title, embed

            pages = [make_page(chunk, pi, len(problems)) for pi, chunk in enumerate(paginator.chunkify(problems, 10))]
            paginator.paginate(self.bot, ctx.channel, pages, wait_time=5 * 60, set_pagenum_footers=True, author_id=ctx.author.id)

    async def _gimme_impl(self, ctx, args):
        backend = self._backend_for_args(args)
        args = [arg for arg in args if arg != '+atcoder']
        handle = await backend.resolve_handle(ctx, self.converter)
        rating = await backend.fetch_rating(handle)
        solved = await backend.fetch_solved(handle)

        problems, tags, hidden = backend.select_gimme_pool(
            args, handle, solved, rating)
        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        choice = max([random.randrange(len(problems)) for _ in range(3)])
        problem = problems[choice]

        title = f'{problem.index}. {problem.name}'
        desc = backend.contest_name_of(problem)
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        rating_of = backend.rating_of(problem)
        ratingStr = rating_of if not hidden else '||'+str(rating_of)+'||'
        embed.add_field(name='Rating', value=ratingStr)
        if tags:
            tagslist = ', '.join(problem.get_matched_tags(tags))
            embed.add_field(name='Matched tags', value=tagslist)
        await ctx.send(f'Recommended problem for `{handle}`', embed=embed)