"""Minimal AtCoder user-profile scraper for handle verification.

AtCoder has no public profile API, so ``get_user`` fetches the public user
page and extracts a few fields with lxml. Requests are serialized per event
loop (AtCoder rate-limits aggressive scrapers) and the parser imports lxml
lazily because the test harness stubs ``lxml``/``lxml.html`` in
``sys.modules`` with empty modules.
"""
import asyncio
import logging
import re
import weakref
from collections import namedtuple

import aiohttp

_AIOHTTP_CLIENT_ERROR = getattr(aiohttp, 'ClientError', OSError)

logger = logging.getLogger(__name__)

BASE_URL = 'https://atcoder.jp'
_USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')
_RETRY_STATUSES = {403, 429}
_MAX_RETRIES = 1
_RETRY_DELAY_SECONDS = 2

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
