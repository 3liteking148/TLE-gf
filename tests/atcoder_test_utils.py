"""Shared fakes for the AtCoder gitgud test suite.

Split out of ``test_atcoder_gitgud.py`` / ``test_atcoder_gitgud_flow.py`` so
both stay under the 500-line limit (AGENTS.md). The fetch-layer fakes mirror
``test_atcoder_handles.py``'s fake aiohttp session.
"""
import asyncio
import json

from tle.util import atcoder_api


def _run(coro):
    return asyncio.run(coro)


def _ac_problem(pid, difficulty=1200, contest_id='abc383', index='a',
                name='Test Task', start=1000, contest_name='AtCoder ABC 383'):
    return atcoder_api.AtCoderProblem(
        pid, contest_id, index, name, difficulty, start, contest_name)


class FakeResponse:
    def __init__(self, status, body=b''):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None):
        self.requests.append(url)
        status, body = self.responses.pop(0)
        return FakeResponse(status, body)

    async def close(self):
        pass


def _json_resp(payload, status=200):
    return (status, json.dumps(payload).encode())
