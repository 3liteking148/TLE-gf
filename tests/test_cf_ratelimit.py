"""Tests for the shared cf_ratelimit budget across all cf API wrappers.

Regression: each @cf_ratelimit decoration used to capture its own `last`
deque, so _query_api and _query_api_anonymous_get burned through 2 rps
collectively rather than the documented 1 rps CF allows.
"""
import asyncio
import importlib.util
import os
import sys
import types

import pytest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_real_cf_api():
    """Load codeforces_api.py under a private name (conftest stubs the
    public name). Provides minimal aiohttp stubs the module needs at import
    time but is not at runtime — the body of _query_api is not exercised
    here, only the @cf_ratelimit wrapper layer."""
    aiohttp = sys.modules.get('aiohttp') or types.ModuleType('aiohttp')
    if not hasattr(aiohttp, 'ClientSession'):
        aiohttp.ClientSession = type('ClientSession', (), {})
    if not hasattr(aiohttp, 'ClientError'):
        aiohttp.ClientError = type('ClientError', (Exception,), {})
    if not hasattr(aiohttp, 'ContentTypeError'):
        aiohttp.ContentTypeError = type('ContentTypeError', (Exception,), {})
    sys.modules['aiohttp'] = aiohttp

    path = os.path.join(_ROOT, 'tle', 'util', 'codeforces_api.py')
    spec = importlib.util.spec_from_file_location('_real_cf_api_ratelimit', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSharedRateLimitBudget:
    """The rate-limit deque must be shared across all @cf_ratelimit
    wrappers. Otherwise each wrapper independently allows 1 rps, and the
    bot collectively exceeds CF's true 1 rps cap."""

    def test_both_wrappers_share_a_single_deque(self):
        cf = _load_real_cf_api()
        # Both wrappers' closure must reference the same deque object.
        # We expose the shared state at module scope as _cf_ratelimit_last.
        assert hasattr(cf, '_cf_ratelimit_last'), (
            'Expected _cf_ratelimit_last to be a module-level shared deque')

        shared = cf._cf_ratelimit_last
        initial_state = list(shared)

        # Stub the underlying coroutines so calls don't hit the network.
        # We don't care what they return — we care that the deque advances.
        async def fake_query(*args, **kwargs):
            return None

        # Replace the wrapped functions' inner callable. Since cf_ratelimit
        # is applied at module load time, monkey-patching the module-level
        # symbols just replaces the wrapped versions. Instead, drive the
        # rate limiter via a fresh decoration that touches the shared deque.
        @cf.cf_ratelimit
        async def probe_a():
            return 'a'

        @cf.cf_ratelimit
        async def probe_b():
            return 'b'

        _run(probe_a())
        after_a = list(shared)
        _run(probe_b())
        after_b = list(shared)

        # If the deque is shared, each call advances the same object.
        assert after_a != initial_state, 'first call did not update shared deque'
        assert after_b != after_a, (
            'second call did not update shared deque — budgets are NOT shared')

    def test_concurrent_wrappers_serialize_correctly(self):
        """Two coroutines entering @cf_ratelimit concurrently must space
        themselves ≥1s apart. asyncio is single-threaded so the
        read-compute-mutate sequence is already atomic (no await between
        the deque ops), but the test pins that contract — if anyone
        later adds an await in the critical section the race opens up
        and this test catches it."""
        cf = _load_real_cf_api()
        call_times = []

        @cf.cf_ratelimit
        async def probe():
            call_times.append(asyncio.get_event_loop().time())
            return None

        async def main():
            cf._cf_ratelimit_last.clear()
            cf._cf_ratelimit_last.extend([0.0] * cf._CF_RATELIMIT_PER_SECOND)
            # Five concurrent calls; with 1 rps budget they must finish
            # spread across ≥4 seconds.
            await asyncio.gather(*(probe() for _ in range(5)))

        _run(main())
        assert len(call_times) == 5
        call_times.sort()
        for i in range(1, len(call_times)):
            spacing = call_times[i] - call_times[i - 1]
            assert spacing >= 0.95, (
                f'Calls {i-1} and {i} fired only {spacing:.3f}s apart — '
                f'rate limiter does not serialize concurrent callers.')

    def test_existing_module_wrappers_advance_the_shared_deque(self):
        """Sanity: invoking the actual production wrappers _query_api and
        _query_api_anonymous_get must advance the *same* deque, not two
        independent ones."""
        cf = _load_real_cf_api()

        # Stub a fake session whose post/get return an awaitable async
        # context manager that yields a JSON-like response with status 200.
        class _FakeResp:
            status = 200
            async def json(self):
                return {'result': None}

        class _FakeCtx:
            async def __aenter__(self): return _FakeResp()
            async def __aexit__(self, *a): return False

        class _FakeSession:
            def post(self, *a, **kw): return _FakeCtx()
            def get(self, *a, **kw): return _FakeCtx()

        cf._session = _FakeSession()

        snap0 = list(cf._cf_ratelimit_last)
        _run(cf._query_api('contest.list'))
        snap1 = list(cf._cf_ratelimit_last)
        _run(cf._query_api_anonymous_get('contest.standings', {'contestId': 1}))
        snap2 = list(cf._cf_ratelimit_last)

        # Each call must have advanced the shared deque. If the wrappers
        # had independent deques, snap2 would equal snap1 (the GET path
        # would advance its own private deque while the shared one stays).
        assert snap1 != snap0, '_query_api did not advance the shared deque'
        assert snap2 != snap1, (
            '_query_api_anonymous_get did not advance the shared deque — '
            'budgets are NOT shared')
