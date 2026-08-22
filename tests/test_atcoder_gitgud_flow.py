"""End-to-end AtCoder gitgud flow tests — drive the real mixin bodies.

``;gitgud +atcoder`` issues an AtCoder challenge (platform ``'ac'`` row),
``;gotgud <submission link>`` claims it by scraping the pasted submission
page, and ``;nogud`` skips it after the 2h window. Only the AtCoder API at
the edges is mocked; everything else runs against an in-memory SQLite DB,
mirroring ``test_gitgud_coins.py``.
"""
import datetime
from types import SimpleNamespace

import pytest  # noqa: F401

from tests.atcoder_test_utils import _ac_problem, _run  # noqa: F401
from tests.betting_test_utils import GUILD, USER_A, db  # noqa: F401

from tle import constants
from tle.util import atcoder_api
from tle.util import codeforces_common as cf_common
from tle.cogs._codeforces_helpers import CodeforcesCogError


class TestAtcoderGitgudFlow:
    @pytest.fixture
    def cog(self, db, monkeypatch):
        from tle.cogs._atcoder_gitgud import AtcoderGitgudMixin
        from tle.cogs._codeforces_gitgud import CodeforcesGitgudMixin
        monkeypatch.setattr(cf_common, 'user_db', db)
        monkeypatch.setattr(constants, 'BET_START_BALANCE', 1000, raising=False)
        monkeypatch.setattr(cf_common, 'cache2', SimpleNamespace(
            atcoder_problem_cache=SimpleNamespace(problems=[])))

        class _Cog(AtcoderGitgudMixin, CodeforcesGitgudMixin):
            pass

        cog = _Cog()
        cog.converter = None
        cog.bot = SimpleNamespace()
        return cog

    def _patch_atcoder(self, monkeypatch, rating='1200', submissions=()):
        async def fake_get_user(handle, **kw):
            return SimpleNamespace(rating=rating)

        async def fake_submissions(handle, **kw):
            return list(submissions)

        monkeypatch.setattr(atcoder_api, 'get_user', fake_get_user)
        monkeypatch.setattr(atcoder_api, 'get_user_submissions', fake_submissions)

    def _set_pool(self, cog, *problems):
        cf_common.cache2.atcoder_problem_cache.problems = list(problems)

    def _set_handle(self, db, uid=USER_A):
        assert db.set_atcoder_handle(uid, GUILD, 'tourist') == 1

    def _ctx(self, uid=USER_A):
        guild = SimpleNamespace(id=GUILD)

        class _Ctx:
            def __init__(self):
                self.author = SimpleNamespace(id=uid, display_name='user')
                self.message = SimpleNamespace(author=SimpleNamespace(id=uid))
                self.guild = guild
                self.channel = SimpleNamespace()
                self.sent = []

            async def send(self, msg, *a, **kw):
                self.sent.append((msg, kw.get('embed')))

        return _Ctx()

    def test_gitgud_atcoder_issues_ac_challenge(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)

        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))

        active = db.check_challenge(USER_A)
        assert active is not None
        assert active[2] == 'abc383_a'  # problem_name holds the problem id
        assert active[3] == 'abc383'
        assert active[4] == 0           # 1200 - 1200
        assert active[5] == 'ac'

        msg, embed = ctx.sent[0]
        assert msg == 'Challenge problem for `tourist`'
        assert embed.title == 'A. Test Task'
        assert embed.description == 'AtCoder ABC 383'
        assert embed.fields[0]['value'] == 1200

    def test_gitgud_atcoder_hides_rating_on_range(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)

        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1000-1400')))

        _, embed = ctx.sent[0]
        assert embed.fields[0]['value'] == '||1200||'

    def test_gitgud_atcoder_accepts_low_range(self, db, cog, monkeypatch):
        # '0-1800' used to be silently discarded by an arg[0:3].isdigit()
        # slice check; it must actually constrain the pool now.
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=100))
        self._patch_atcoder(monkeypatch)

        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '0-1800')))

        _, embed = ctx.sent[0]
        assert embed.fields[0]['value'] == '||100||'

    def test_gitgud_atcoder_rejects_tags(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        ctx = self._ctx()
        with pytest.raises(CodeforcesCogError):
            _run(cog._gitgud_impl(ctx, ('+atcoder', '+dp')))
        with pytest.raises(CodeforcesCogError):
            _run(cog._gitgud_impl(ctx, ('+atcoder', '~dp')))

    def test_gitgud_atcoder_rejects_negative_rating(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        with pytest.raises(CodeforcesCogError):
            _run(cog._gitgud_impl(self._ctx(), ('+atcoder', '-100')))

    def test_gitgud_atcoder_requires_handle(self, db, cog, monkeypatch):
        self._patch_atcoder(monkeypatch)
        with pytest.raises(CodeforcesCogError):
            _run(cog._gitgud_impl(self._ctx(), ('+atcoder', '1200')))

    def test_gitgud_atcoder_skips_solved_problem(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch, submissions=[
            atcoder_api.AtCoderSubmission(1, 'abc383_a', 'AC')])
        self._set_pool(cog,
                       _ac_problem('abc383_a', difficulty=1200),
                       _ac_problem('abc383_b', difficulty=1200,
                                   name='Keep it', index='b'))
        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        active = db.check_challenge(USER_A)
        assert active[2] == 'abc383_b'

    def test_gitgud_atcoder_skips_nogudded_problem(self, db, cog, monkeypatch):
        from tle.util.db.user_db_conn import Gitgud
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._set_pool(cog,
                       _ac_problem('abc383_a', difficulty=1200),
                       _ac_problem('abc383_b', difficulty=1200,
                                   name='Keep it', index='b'))
        # Nogud abc383_a via a direct challenge row, then both are excluded.
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        cid = db.check_challenge(USER_A)[0]
        db.skip_challenge(USER_A, cid, Gitgud.NOGUD)
        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        assert db.check_challenge(USER_A)[2] == 'abc383_b'

    def test_gitgud_atcoder_no_problems_left(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch, submissions=[
            atcoder_api.AtCoderSubmission(1, 'abc383_a', 'AC')])
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        ctx = self._ctx()
        with pytest.raises(CodeforcesCogError):
            _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))

    def _patch_submission(self, monkeypatch, submission=None):
        async def fake_get_submission(url, **kw):
            return submission

        monkeypatch.setattr(atcoder_api, 'get_submission', fake_get_submission)

    def _claim(self, cog, url='https://atcoder.jp/contests/abc383/'
                             'submissions/12345678'):
        return _run(cog._gotgud_impl(self._ctx(), url))

    def test_gotgud_completes_atcoder_challenge(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)

        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        # The claim scrapes the pasted link instead of polling the API.
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('tourist', 'abc383_a', 'AC'))
        _run(cog._gotgud_impl(ctx, 'https://atcoder.jp/contests/abc383/'
                                  'submissions/12345678'))

        msg = ctx.sent[1][0]
        assert '8 alltime' in msg
        assert db.check_challenge(USER_A) is None
        assert db.get_gudgitter_score(USER_A) == 8

    def test_gotgud_requires_actual_solve(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('tourist', 'abc383_a', 'WA'))

        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        with pytest.raises(CodeforcesCogError, match='not accepted'):
            _run(cog._gotgud_impl(ctx, 'https://atcoder.jp/contests/abc383/'
                                      'submissions/12345678'))
        assert db.check_challenge(USER_A) is not None

    def test_gotgud_requires_submission_link(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError,
                           match='Paste your AtCoder submission link'):
            _run(cog._gotgud_impl(self._ctx()))

    def test_gotgud_rejects_foreign_link(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError,
                           match='does not look like an AtCoder submission'):
            self._claim(cog, 'https://example.com/submissions/12345678')

    def test_gotgud_rejects_unreadable_submission(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._patch_submission(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError,
                           match='Could not read that submission'):
            self._claim(cog)

    def test_gotgud_rejects_pending_judge(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('tourist', 'abc383_a', 'WJ'))
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError, match='still being judged'):
            self._claim(cog)

    def test_gotgud_accepts_case_variant_handle(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('TOURIST', 'abc383_a', 'AC'))
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        self._claim(cog)
        assert db.check_challenge(USER_A) is None
        assert db.get_gudgitter_score(USER_A) == 8

    def test_gotgud_rejects_foreign_account(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('someone_else', 'abc383_a',
                                              'AC'))
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError,
                           match='not from your linked AtCoder account'):
            self._claim(cog)

    def test_gotgud_rejects_wrong_problem(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        self._patch_submission(
            monkeypatch,
            atcoder_api.AtCoderSubmissionPage('tourist', 'abc383_b', 'AC'))
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        with pytest.raises(CodeforcesCogError,
                           match='different problem than your challenge'):
            self._claim(cog)

    def test_gotgud_rejects_wrong_contest(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('abc383_a'), 0, platform='ac')
        # Fails before scraping: the contest in the link is checked against
        # the challenge row.
        with pytest.raises(CodeforcesCogError,
                           match='different contest than your challenge'):
            self._claim(cog,
                        'https://atcoder.jp/contests/abc382/submissions/'
                        '12345678')

    def _run_log(self, monkeypatch):
        from tle.cogs import _gitgud as gitgud_mod
        captured = {}

        def fake_embed(**kw):
            captured['desc'] = kw.get('description', '')
            return SimpleNamespace()

        def fake_paginate(*args, **kw):
            return None

        monkeypatch.setattr(gitgud_mod.discord_common, 'cf_color_embed',
                            fake_embed)
        monkeypatch.setattr(gitgud_mod.paginator, 'paginate', fake_paginate)
        return captured

    def test_gitlog_falls_back_for_unknown_problem(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('ghost_task'), 0,
                         platform='ac')
        captured = self._run_log(monkeypatch)
        _run(cog._gitlog_impl(self._ctx(), None))
        assert '`ghost_task`' in captured['desc']

    def test_nogudlog_falls_back_for_unknown_problem(self, db, cog,
                                                    monkeypatch):
        self._set_handle(db)
        self._patch_atcoder(monkeypatch)
        db.new_challenge(USER_A, 1, _ac_problem('ghost_task'), 0,
                         platform='ac')
        captured = self._run_log(monkeypatch)
        _run(cog._nogudlog_impl(self._ctx(), None))
        assert '`ghost_task`' in captured['desc']

    def test_nogud_skips_atcoder_challenge(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)
        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        # Challenge issued a moment ago -> within the 2h no-skip window.
        _run(cog._nogud_impl(ctx))
        assert db.check_challenge(USER_A) is not None

        # Age the challenge past the window, then skip succeeds.
        old = int(datetime.datetime.now().timestamp()) - 3 * 3600
        cid = db.check_challenge(USER_A)[0]
        db.conn.execute(
            'UPDATE challenge SET issue_time = ? WHERE id = ?', (old, cid))
        db.conn.execute(
            'UPDATE user_challenge SET issue_time = ? WHERE user_id = ?',
            (old, USER_A))
        db.conn.commit()
        _run(cog._nogud_impl(ctx))
        assert db.check_challenge(USER_A) is None
        assert db.get_nogud_problem_keys(USER_A) == {'abc383_a'}

    def test_validate_gitgud_status_builds_atcoder_url(self, db, cog, monkeypatch):
        self._set_handle(db)
        self._set_pool(cog, _ac_problem('abc383_a', difficulty=1200))
        self._patch_atcoder(monkeypatch)
        ctx = self._ctx()
        _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        with pytest.raises(CodeforcesCogError) as excinfo:
            _run(cog._gitgud_impl(ctx, ('+atcoder', '1200')))
        assert 'https://atcoder.jp/contests/abc383/tasks/abc383_a' in str(
            excinfo.value)

    def test_validate_gitgud_status_builds_cf_url_without_cache(
            self, db, cog, monkeypatch):
        # p_index lets the CF challenge URL be built from the row alone: the
        # cog fixture's cache2 has no problem_cache at all, so a URL that
        # depended on the cache would blow up here.
        prob = SimpleNamespace(name='Ghost Problem', contestId=1234, index='B2',
                           key='Ghost Problem')
        assert db.new_challenge(USER_A, 1, prob, 0) == 1
        with pytest.raises(CodeforcesCogError) as excinfo:
            _run(cog._validate_gitgud_status(self._ctx()))
        assert 'https://codeforces.com/contest/1234/problem/B2' in str(
            excinfo.value)
