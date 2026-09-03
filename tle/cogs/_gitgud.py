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

# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportReturnType=false
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
from tle.util.gitgud_progression import (
    biased_choice,
    bonus_scores,
    compute_bonus_scores,
)
from tle.cogs._gitgud_progression import (
    build_progression_desc_lines,
    parse_progression_args,
    select_progression_problems,
)


def _split_gotgud_args(args):
    """Split ``;gotgud`` args into ``(submission_url, partial)``.

    ``+partial`` applies to any multi-challenge batch and is inert on
    singletons. The first non-flag arg is the AtCoder submission URL.
    """
    partial = '+partial' in args
    rest = [a for a in args if a != '+partial']
    return (rest[0] if rest else None), partial


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

    def _monthly_total(self, base, when):
        start_time, end_time = cf_common.get_start_and_end_of_month(when)
        now_time = int(when.timestamp())
        if self._check_more_points_active(now_time, start_time, end_time):
            return 2 * base
        return base

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
        stored gitgud point. The rate is the flat 5x base rate scaled by
        ``constants.GITGUD_COIN_EARN_MULTIPLIER`` (default 10, i.e. 50x) of
        the stored score — bonus-included for boosted batches — and never
        gets the end-of-month doubling the monthly ranklist points do.
        Returns the coins awarded, or None when there's no guild (e.g. a DM)
        so the caller can omit the wallet line."""
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
            return problem.name, problem.rating, problem.url
        except (KeyError, AttributeError):
            return problem_key, '?', None

    def _active_problem_name(self, backend, problem_key):
        """Pretty problem name for the active-challenge error; falls back to
        the raw key when the cache can't resolve it."""
        return self._problem_ref(backend, problem_key)[0]

    async def _validate_gitgud_status(self, ctx):
        user_id = ctx.message.author.id
        actives = cf_common.user_db.list_active_challenges(user_id)
        if actives:
            if len(actives) == 1:
                _, _, problem_key, contest_id, _, platform, p_index, _, _ = actives[0]
                backend = self._backend_for_platform(platform)
                name = self._active_problem_name(backend, problem_key)
                url = backend.active_url(contest_id, problem_key, p_index)
                raise CodeforcesCogError(f'You have an active challenge {name} at {url}')
            # Show all actives; error order is challenge.id
            parts = []
            for active in actives:
                _, _, problem_key, contest_id, _, platform, p_index, _, _ = active
                backend = self._backend_for_platform(platform)
                name = self._active_problem_name(backend, problem_key)
                url = backend.active_url(contest_id, problem_key, p_index)
                parts.append(f'{name} at {url}')
            raise CodeforcesCogError(f'You have {len(actives)} active challenge(s): ' + ', '.join(parts))

    def _batch_id_for_ctx(self, ctx, issue_time, prefix=""):
        try:
            return f"{prefix}snowflake-{ctx.message.id}"
        except Exception:
            pass

        # fallback
        return f"{prefix}{int(issue_time * 1000)}-{random.randint(0, 999999)}"

    async def _gitgud(self, ctx, handle, problem, delta, score, hidden, backend, now):
        # The caller of this function is responsible for calling `_validate_gitgud_status` first.
        user_id = ctx.author.id

        issue_time = now.timestamp()
        batch_id = self._batch_id_for_ctx(ctx, issue_time)
        rc = cf_common.user_db.new_challenge(
            user_id, issue_time, problem, delta, score, backend.platform, batch_id)
        if rc != 1:
            raise CodeforcesCogError('Your challenge has already been added to the database!')

        points = score
        monthlypoints = self._monthly_total(points, now)

        title = f'{problem.index}. {problem.name}'
        desc = problem.contest_name
        rating = problem.rating
        ratingStr = rating if not hidden else '||' + str(rating) + '||'
        pointsStr = points if not hidden else '||' + str(points) + '||'
        monthlyPointsStr = monthlypoints if not hidden else '||' + str(monthlypoints) + '||'
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=ratingStr)
        embed.add_field(name='Alltime points', value=pointsStr)
        embed.add_field(name='Monthly points', value=monthlyPointsStr)
        await ctx.send(f'Challenge problem for `{handle}`', embed=embed)

    async def _finalize_challenges(self, ctx, handle, actives, now, skips=()):
        """Resolve the whole batch at once (completions + ``+partial`` skips, atomically).

        Positional bonus math falls out of the single list. Single and batch
        claims share the challenge message; only a bonus line distinguishes
        a boosted batch.
        """
        cur_ts = now.timestamp()
        user_id = ctx.message.author.id
        base = [a.score for a in actives]
        issue_time = actives[0].issue_time
        batch_id = actives[0].batch_id

        # award progression bonus, if any — prefix-only: mults apply solely
        # to the unbroken solved streak starting at slot A, so skipping an
        # early slot voids the bonus for it and every later slot.
        skip_ids = {cid for cid, _ in skips}
        solved_mask = [a.challenge_id not in skip_ids for a in actives]
        scores_all, window, mults = compute_bonus_scores(
            base, batch_id, cur_ts, issue_time, solved_mask
        )
        kept = [i for i, a in enumerate(actives) if a.challenge_id not in skip_ids]
        scores_to_store = [scores_all[i] for i in kept]
        stored_base = [base[i] for i in kept]

        finish_time = int(cur_ts)
        completions = [(actives[i].challenge_id, finish_time, scores_all[i]) for i in kept]
        rc = cf_common.user_db.resolve_challenges(
            user_id, completions=completions, skips=list(skips))
        total = sum(scores_to_store)
        monthlyPoints = self._monthly_total(total, now)
        if rc == len(actives):
            duration = cf_common.pretty_time_format(finish_time - issue_time)
            msg = (f'Challenge completed in {duration}. {handle} gained {total} '
                   f'alltime ranklist points and {monthlyPoints} monthly ranklist points.')
            coins = self._award_gitgud_coins(ctx, user_id, total)
            if coins is not None and cf_common.user_db.bet_has_wagered(ctx.guild.id, user_id):
                msg += f' You also earned {coins} 🪙.'
            await ctx.send(msg)
            if scores_to_store != stored_base:
                assert mults is not None and window is not None
                await ctx.send(f"Bonus applied for solving within {window//60} min).")
        else:
            await ctx.send('You have already claimed your points')

    # ------------------------------------------------------------------
    # Command bodies
    # ------------------------------------------------------------------

    def _progression_embed(self, handle, level, theme, problems, scores, total, now):
        bonus_total = sum(bonus_scores(scores))
        desc_lines = build_progression_desc_lines(problems, scores)
        pub = discord.Embed(title=f"ThemeCP level {level} ({theme.time//60} min, {theme.perf} rating) for `{handle}`", description="\n".join(desc_lines))
        pub.add_field(name='Alltime points', value=str(total))
        pub.add_field(name='Monthly points', value=str(self._monthly_total(total, now)))
        pub.set_footer(text=f"Bonus needs an unbroken streak from A within {theme.time//60} min (bonus total {bonus_total}). ;gotgud checks all 4 at once (+partial claims a solved subset). ;nogud after 2h skips whole batch.")
        return pub

    async def _gitgudprogression_impl(self, ctx, args):
        now = datetime.datetime.now()
        backend = self._backend_for_platform('cf')
        level, theme, tags, bantags = parse_progression_args(args, backend)

        handle = await backend.resolve_handle(ctx, self.converter)
        _, delta_base = backend.scale_rating(
            await backend.fetch_rating(handle))
        solved = await backend.fetch_solved(handle, only_ac=False)
        noguds = backend.nogud_set(ctx.message.author.id)

        await self._validate_gitgud_status(ctx)

        problems = select_progression_problems(backend, level, solved, noguds, handle, tags, bantags)
        deltas: list[int] = []
        scores: list[int] = []
        for prob in problems:
            d, s = backend.score_model.delta_and_score(prob.rating, delta_base)
            deltas.append(d)
            scores.append(s)

        issue_time = now.timestamp()
        batch_id = self._batch_id_for_ctx(ctx, issue_time, prefix=f"prog-{level}-")
        items = [(prob, d, s, backend.platform) for prob, d, s in zip(problems, deltas, scores)]
        rc = cf_common.user_db.new_challenges(ctx.message.author.id, batch_id, issue_time, items)
        if rc != len(items):
            raise CodeforcesCogError('Your challenge has already been added to the database!')

        await ctx.send(embed=self._progression_embed(
            handle, level, theme, problems, scores, sum(scores), now))

    async def _gitgud_impl(self, ctx, args):
        now = datetime.datetime.now()
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

        choice = biased_choice(problems)

        # Penalised tags divide points by (tag count + 1), rounded up.
        # Hardening division filters such as +div1 and ~div3/~div4/~edu are
        # exempt on CF; on AtCoder the hard/easy contest types (arc/agc vs
        # abc) are the free filters. The raw delta is stored untouched; the
        # (possibly off-ladder) score goes into its own column.
        problem = problems[choice]
        delta, score = backend.score_model.delta_and_score(problem.rating, delta_base, tags, bantags)
        await self._gitgud(ctx, handle, problem, delta, score, hidden, backend, now)

    async def _gotgud_impl(self, ctx, *args, **kw):
        """Claim the active challenge(s) once solved.

        ``args`` are raw command args (AtCoder URL, ``+partial``), parsed
        here so every entry point shares one path. Keywords are a bug.
        """
        if kw:
            raise TypeError(
                f"_gotgud_impl takes raw command args, got keywords {sorted(kw)}.")
        submission_url, partial = _split_gotgud_args(args)
        now = datetime.datetime.now()
        user_id = ctx.message.author.id
        actives = cf_common.user_db.list_active_challenges(user_id)
        if not actives:
            raise CodeforcesCogError(f'You do not have an active challenge')
        backend = self._backend_for_platform(actives[0].platform)
        handle = await backend.resolve_handle(ctx, self.converter)
        _, missing = await backend.verify_claims(ctx, handle, actives, submission_url, partial)
        await self._finalize_challenges(
            ctx, handle, actives, now,
            skips=[(a.challenge_id, Gitgud.NOGUD) for a in missing])

    async def _nogud_impl(self, ctx):
        now = datetime.datetime.now()
        user_id = ctx.message.author.id
        actives = cf_common.user_db.list_active_challenges(user_id)
        if not actives:
            raise CodeforcesCogError(f'You do not have an active challenge')
        backend = self._backend_for_platform(actives[0].platform)
        await backend.validate_handle(ctx, self.converter)
        issue_time = actives[0].issue_time
        finish_time = int(now.timestamp())
        if finish_time - issue_time < _GITGUD_NO_SKIP_TIME:
            skip_time = cf_common.pretty_time_format(issue_time + _GITGUD_NO_SKIP_TIME - finish_time)
            await ctx.send(f'Think more. You can skip your challenge in {skip_time}.')
            return
        cids = [a.challenge_id for a in actives]
        rc = cf_common.user_db.resolve_challenges(user_id, completions=[], skips=[(cid, Gitgud.NOGUD) for cid in cids])
        if rc == len(actives):
            await ctx.send(f'Challenge skipped.')
        else:
            await ctx.send(f'Failed to skip challenge.')

    async def _force_nogud_impl(self, ctx, member):
        actives = cf_common.user_db.list_active_challenges(member.id)
        if not actives:
            await ctx.send(f'No active challenge found for user `{member.display_name}`.')
            return
        cids = [a.challenge_id for a in actives]
        rc = cf_common.user_db.resolve_challenges(member.id, completions=[], skips=[(cid, Gitgud.FORCED_NOGUD) for cid in cids])
        if rc == len(actives):
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
        now = datetime.datetime.now()
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
            delta, score = backend.score_model.delta_and_score(problem.rating, delta_base, (), ())
            await self._gitgud(ctx, handle, problem, delta, score,
                               False, backend, now)
        else:
            problems = problems[:500]

            def make_line(i, prob):
                data = (f'{i + 1}: [{prob.name}]({prob.url}) [{prob.rating}]')
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

        choice = biased_choice(problems, k=3)
        problem = problems[choice]

        title = f'{problem.index}. {problem.name}'
        desc = problem.contest_name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        rating = problem.rating
        ratingStr = rating if not hidden else '||' + str(rating) + '||'
        embed.add_field(name='Rating', value=ratingStr)
        if tags:
            tagslist = ', '.join(problem.get_matched_tags(tags))
            embed.add_field(name='Matched tags', value=tagslist)
        await ctx.send(f'Recommended problem for `{handle}`', embed=embed)
