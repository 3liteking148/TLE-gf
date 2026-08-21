"""Smoke tests for the undocumented ;gotgud / ;gitgud easter-egg aliases.

Each hidden command must dispatch into the same impl its canonical command
uses, through the real ``user_guard`` wrapper. Nothing else is pinned on
purpose -- the decorators are literal in ``codeforces.py`` and visible there.
"""
import asyncio
from types import SimpleNamespace

import pytest

from tle.cogs.codeforces import Codeforces


def _ctx(user_id):
    return SimpleNamespace(message=SimpleNamespace(author=SimpleNamespace(id=user_id)))


@pytest.mark.parametrize('alias,impl', [
    ('gotshit', '_gotgud_impl'),
    ('gotfucked', '_gotgud_impl'),
    ('gotfkd', '_gotgud_impl'),
    ('gitshit', '_gitgud_impl'),
    ('gitfucked', '_gitgud_impl'),
    ('gitfkd', '_gitgud_impl'),
])
def test_alias_reaches_impl(alias, impl):
    cog = Codeforces(object())
    seen = []

    async def rec(ctx, *rest):
        seen.append(rest)

    setattr(cog, impl, rec)
    asyncio.run(getattr(cog, alias)(_ctx(42), 'ARG'))
    assert len(seen) == 1
