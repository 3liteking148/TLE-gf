"""AtCoder profile scraper and problem/submission data client.

AtCoder has no public API, so this module talks to two third-party surfaces:

- ``atcoder.jp``: ``get_user`` scrapes the public user page (lxml) for
  handle verification and live rating lookups; ``get_submission`` scrapes a
  single submission detail page for gitgud claim verification.
- ``kenkoooo.com/atcoder`` (the AtCoder Problems project): ``get_problems``,
  ``get_problem_models``, ``get_contests`` and ``get_user_submissions`` read
  its public static datasets and v3 API, which are the community-standard
  source for the problem list, difficulty ratings and per-user submissions.

Requests are serialized per event loop (AtCoder rate-limits aggressive
scrapers) and the parser imports lxml lazily because the test harness stubs
``lxml``/``lxml.html`` in ``sys.modules`` with empty modules.
"""
import asyncio
import json
import logging
import re
import weakref
from collections import namedtuple

import aiohttp

_AIOHTTP_CLIENT_ERROR = getattr(aiohttp, 'ClientError', OSError)

logger = logging.getLogger(__name__)

BASE_URL = 'https://atcoder.jp'
KENKOOO_BASE_URL = 'https://kenkoooo.com/atcoder'
_PROBLEMS_URL = f'{KENKOOO_BASE_URL}/resources/problems.json'
_PROBLEM_MODELS_URL = f'{KENKOOO_BASE_URL}/resources/problem-models.json'
_CONTESTS_URL = f'{KENKOOO_BASE_URL}/resources/contests.json'
_SUBMISSIONS_URL = f'{KENKOOO_BASE_URL}/atcoder-api/v3/user/submissions'
_MAX_SUBMISSION_PAGES = 100
_DIFFICULTY_MIN = 0
_DIFFICULTY_MAX = 4199
# Bounds for ;gitgud rating requests (single source of truth shared with the
# gitgud backend). Deliberately narrower than _DIFFICULTY_MAX: requested
# ratings stop at 4000 while difficulty is clipped to 4199, leaving a dead
# band of difficulties no request can reach. The cap keeps validation text
# aligned with how AtCoder displays ratings.
RATING_MIN = 0
RATING_MAX = 4000
_USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')
_RETRY_STATUSES = {403, 429}
_MAX_RETRIES = 1
_RETRY_DELAY_SECONDS = 2
_REQUEST_TIMEOUT_SECONDS = 15

AtCoderUser = namedtuple('AtCoderUser', 'handle affiliation country rating')

_fetch_locks = weakref.WeakKeyDictionary()


def _fetch_lock():
    """Return the per-event-loop request lock (AtCoder throttles scrapers).

    Created lazily per running loop: a module-level ``asyncio.Lock`` would be
    bound to the first loop that awaited it and break later ``asyncio.run``
    calls in tests.
    """
    loop = asyncio.get_running_loop()
    lock = _fetch_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _fetch_locks[loop] = lock
    return lock


def _clean(value):
    return re.sub(r'\s+', ' ', value).strip() if value else ''


def _parse_profile(raw):
    """Parse an AtCoder user page into an ``AtCoderUser``.

    Imports lxml lazily so the module loads in the test harness, which
    stubs ``lxml`` as an empty module.
    """
    from lxml import html
    tree = html.fromstring(raw)

    def cell(label):
        nodes = tree.xpath(
            f'//th[normalize-space()="{label}"]/following-sibling::td[1]')
        return _clean(nodes[0].text_content()) if nodes else ''

    titles = tree.xpath('//title/text()')
    handle = titles[0].split(' - ')[0].strip() if titles else ''
    return AtCoderUser(handle, cell('Affiliation'), cell('Country/Region'),
                       cell('Rating'))


async def _fetch_page(session, url):
    async with session.get(url, headers={'User-Agent': _USER_AGENT}) as resp:
        return resp.status, await resp.read()


async def get_user(handle, *, session=None):
    """Fetch the public profile of ``handle``.

    Returns an ``AtCoderUser``, or ``None`` when the handle does not exist
    (AtCoder returns 404 for unknown users) or the fetch fails.
    """
    url = f'{BASE_URL}/users/{handle}'
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        async with _fetch_lock():
            for attempt in range(_MAX_RETRIES + 1):
                status, raw = await _fetch_page(session, url)
                if status == 404:
                    return None
                if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                if status != 200:
                    logger.warning('AtCoder GET %s returned status %d',
                                   url, status)
                    return None
                return _parse_profile(raw)
    except _AIOHTTP_CLIENT_ERROR as exc:
        logger.warning('AtCoder GET %s failed: %s', url, exc)
        return None
    finally:
        if own_session:
            await session.close()


def parse_rating(value):
    """Parse the leading integer from a rating string like ``'3797'`` or
    ``'683 (Provisional)'``; returns None for unrated/empty values."""
    if not value:
        return None
    match = re.match(r'(\d+)', value)
    return int(match.group(1)) if match else None


_SUBMISSION_URL_RE = re.compile(
    r'https?://(?:www\.)?atcoder\.jp/contests/([^/?#]+)/submissions/(\d+)')


def parse_submission_url(url):
    """Extract ``(contest_id, submission_id)`` from an AtCoder submission
    link like ``https://atcoder.jp/contests/abc470/submissions/78313913``.

    Tolerates ``?lang=`` query strings and trailing slashes. Returns None
    when the URL is not an AtCoder submission link.
    """
    match = _SUBMISSION_URL_RE.search(url)
    if match is None:
        return None
    return match.group(1), match.group(2)


class AtCoderSubmissionPage(namedtuple('AtCoderSubmissionPage',
                                       'handle problem_id verdict')):
    """One parsed submission detail page from atcoder.jp."""
    __slots__ = ()

    @property
    def is_ac(self):
        return self.verdict == 'AC'


def _parse_submission_page(raw):
    """Parse an AtCoder submission detail page into an
    ``AtCoderSubmissionPage``.

    Imports lxml lazily (see ``_parse_profile``). Returns None when the
    page is not a submission detail page (e.g. a 404 page).
    """
    from lxml import html
    tree = html.fromstring(raw)

    verdict = tree.xpath("//td[@id='judge-status']/span/text()")
    verdict = _clean(verdict[0]) if verdict else ''

    problem_id = ''
    task = tree.xpath(
        "//th[normalize-space()='Task']/following-sibling::td//a/@href")
    if task:
        match = re.search(r'/tasks/([^/?#]+)$', task[0])
        if match:
            problem_id = match.group(1)

    handle = ''
    user = tree.xpath(
        "//th[normalize-space()='User']/following-sibling::td//a/@href")
    if user:
        match = re.search(r'/users/([^/?#]+)$', user[0])
        if match:
            handle = match.group(1)

    if not verdict and not problem_id and not handle:
        return None
    return AtCoderSubmissionPage(handle, problem_id, verdict)


async def get_submission(url, *, session=None):
    """Fetch and parse a single AtCoder submission detail page.

    Returns an ``AtCoderSubmissionPage``, or None when the page is not
    found (404), the fetch fails, or the page cannot be parsed. Owned
    sessions carry a request timeout so a hung page cannot stall
    ``_fetch_lock`` (which serializes every AtCoder request) forever.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS))
    try:
        async with _fetch_lock():
            for attempt in range(_MAX_RETRIES + 1):
                status, raw = await _fetch_page(session, url)
                if status == 404:
                    return None
                if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                if status != 200:
                    logger.warning('AtCoder GET %s returned status %d',
                                   url, status)
                    return None
                return _parse_submission_page(raw)
    except _AIOHTTP_CLIENT_ERROR as exc:
        logger.warning('AtCoder GET %s failed: %s', url, exc)
        return None
    finally:
        if own_session:
            await session.close()


class AtCoderProblem(namedtuple('AtCoderProblem',
                                'id contest_id problem_index name difficulty '
                                'contest_start contest_name')):
    """An AtCoder problem from kenkoooo's problems.json + problem-models.json.

    ``difficulty`` is the clipped estimated difficulty (None when the problem
    has no model — these are excluded from the gitgud pool)."""
    __slots__ = ()

    def __new__(cls, id, contest_id, problem_index, name,
                difficulty=None, contest_start=None, contest_name=None):
        return super().__new__(cls, id, contest_id, problem_index, name,
                               difficulty, contest_start, contest_name)

    @property
    def key(self):
        # The canonical challenge key, mirroring Problem.key on Codeforces.
        return self.id

    @property
    def contestId(self):
        # CF-vocabulary alias so uniform accessors work across platforms.
        return self.contest_id

    @property
    def index(self):
        return self.problem_index.upper()

    @property
    def url(self):
        return f'{BASE_URL}/contests/{self.contest_id}/tasks/{self.id}'

    def has_difficulty(self):
        return self.difficulty is not None


class AtCoderSubmission(namedtuple('AtCoderSubmission',
                                   'epoch_second problem_id result')):
    """One entry from kenkoooo's per-user submission API."""
    __slots__ = ()

    @property
    def is_ac(self):
        return self.result == 'AC'


class AtCoderContest(namedtuple('AtCoderContest',
                                'id start_epoch_second title')):
    """One entry from kenkoooo's contests.json."""
    __slots__ = ()


async def _fetch_json(session, url):
    """GET ``url`` and return parsed JSON, or None on failure.

    Retries 403/429 once, like ``get_user``. Callers own the request lock.
    Owned sessions carry a request timeout so a hung kenkoooo request cannot
    stall ``_fetch_lock`` (which serializes every AtCoder request) for the
    aiohttp default of several minutes.
    """
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS))
    try:
        for attempt in range(_MAX_RETRIES + 1):
            status, raw = await _fetch_page(session, url)
            if status == 200:
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    logger.warning('AtCoder GET %s returned invalid JSON: %s',
                                   url, exc)
                    return None
            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            logger.warning('AtCoder GET %s returned status %d', url, status)
            return None
    except _AIOHTTP_CLIENT_ERROR as exc:
        logger.warning('AtCoder GET %s failed: %s', url, exc)
        return None
    finally:
        if own_session:
            await session.close()


async def get_problems(*, session=None):
    """Fetch the full AtCoder problem list from kenkoooo's static dataset.

    Returns a list of ``AtCoderProblem`` (without difficulty ratings), or
    None when the fetch fails.
    """
    data = await _fetch_json(session, _PROBLEMS_URL)
    if not data:
        return None
    problems = []
    for entry in data:
        problems.append(AtCoderProblem(
            entry['id'], entry['contest_id'], entry['problem_index'],
            entry['name']))
    return problems


async def get_problem_models(*, session=None):
    """Fetch kenkoooo's difficulty model map.

    Returns a dict mapping problem id -> clipped difficulty (int). Models
    flagged experimental are excluded, as are models without a difficulty.
    Returns None when the fetch fails.
    """
    data = await _fetch_json(session, _PROBLEM_MODELS_URL)
    if not data:
        return None
    models = {}
    for problem_id, model in data.items():
        if not isinstance(model, dict) or model.get('is_experimental'):
            continue
        difficulty = model.get('difficulty')
        if not isinstance(difficulty, (int, float)):
            continue
        models[problem_id] = max(_DIFFICULTY_MIN,
                                 min(_DIFFICULTY_MAX, int(difficulty)))
    return models


async def get_contests(*, session=None):
    """Fetch kenkoooo's contest list.

    Returns a dict mapping contest id -> ``AtCoderContest``, or None when the
    fetch fails.
    """
    data = await _fetch_json(session, _CONTESTS_URL)
    if not data:
        return None
    contests = {}
    for entry in data:
        contests[entry['id']] = AtCoderContest(
            entry['id'], entry['start_epoch_second'], entry['title'])
    return contests


async def get_user_submissions(handle, *, session=None, max_pages=_MAX_SUBMISSION_PAGES):
    """Fetch every submission of ``handle`` from kenkoooo's v3 API.

    The API returns at most 500 submissions after a given timestamp, so the
    results are walked backwards page by page. Returns a list of
    ``AtCoderSubmission``, or None when the first page fails to load.

    Deliberate limits: users with more than ``max_pages`` of submissions
    (~50k) are truncated — only the most recent pages are kept, so the
    solved-set of a very old account can miss early solves and gitgud may
    re-issue a solved problem (annoying, never harmful). Advancing by
    ``max(epoch_second) + 1`` also skips any stragglers submitted in that
    same exact second of a full page; verdict-keyed solved sets make a
    re-issue the only consequence.
    """
    submissions = []
    from_second = 0
    for _ in range(max_pages):
        url = f'{_SUBMISSIONS_URL}?user={handle}&from_second={from_second}'
        async with _fetch_lock():
            page = await _fetch_json(session, url)
        if page is None:
            return None if not submissions else submissions
        page = [AtCoderSubmission(s['epoch_second'],
                                  s['problem_id'], s['result'])
                for s in page if isinstance(s, dict)]
        submissions.extend(page)
        if len(page) < 500:
            break
        from_second = max(s.epoch_second for s in page) + 1
    return submissions
