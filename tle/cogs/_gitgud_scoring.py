"""Gitgud scoring — per-platform ladder and tag penalty.

``GitgudScoreModel`` is deliberately data-driven: each platform supplies its
own ``(min,max,step)`` and its free-tag sets.  Codeforces \"hardening\"
filters (``+div1``, ``~div3/4/edu``) are free because they make the pool
harder; AtCoder \"hard\" contests (``+arc/ agc``) and the easy ``~abc`` ban
are the analogous free filters.

Both scores and deltas are ints; the scaled AtCoder bounds are floats, so
``raw_score`` works in float and banks the result.  ``bankers round`` is kept
intentionally as in Codeforces rating scaling.
"""
from __future__ import annotations

from typing import Iterable

DISTRIB: tuple[int, ...] = (1, 2, 3, 5, 8, 12, 17, 23)


class GitgudScoreModel:
    """Ladder ``DISTRIB`` plus tag-penalty, parameterised per platform.

    ``distrib_min/max/step`` define the ladder.  Outside ``[min,max]`` the
    edge value is returned; inside, ``round((delta-min)/step)`` picks the
    nearest rung (bankers).
    """

    DISTRIB = DISTRIB

    def __init__(
        self,
        distrib_min: float,
        distrib_max: float,
        ladder_threshold: float,
        free_required_tags: Iterable[str],
        free_banned_tags: Iterable[str],
    ):
        self.distrib_min = distrib_min
        self.distrib_max = distrib_max
        self.ladder_threshold = ladder_threshold
        # Normalised for case/space-insensitive comparison.
        self.free_required: frozenset[str] = frozenset(
            t.strip().lower() for t in free_required_tags
        )
        self.free_banned: frozenset[str] = frozenset(
            t.strip().lower() for t in free_banned_tags
        )

    # -- ladder ----------------------------------------------------------

    def raw_score(self, delta: float) -> int:
        """Plain ladder value for ``delta`` (no tag penalty)."""
        if delta <= self.distrib_min:
            return self.DISTRIB[0]
        if delta >= self.distrib_max:
            return self.DISTRIB[-1]
        idx = round((delta - self.distrib_min) / self.ladder_threshold)
        # Guard float rounding at the edges (e.g. 299.999* scale).
        if idx < 0:
            idx = 0
        elif idx >= len(self.DISTRIB):
            idx = len(self.DISTRIB) - 1
        return self.DISTRIB[idx]

    # -- tag penalty -----------------------------------------------------

    def penalised_count(self, tags: Iterable[str], bantags: Iterable[str]) -> int:
        """How many requested tags actually cost points."""
        req = sum(1 for t in tags if t.strip().lower() not in self.free_required)
        ban = sum(1 for t in bantags if t.strip().lower() not in self.free_banned)
        return req + ban

    def penalty_score(self, base_delta: float, num_tags: int) -> int:
        """``base_delta`` ladder value divided by ``num_tags+1``, ceiling, >=1."""
        base_score = self.raw_score(base_delta)
        if num_tags <= 0:
            return base_score
        return max(1, (base_score + num_tags) // (num_tags + 1))

    def final_score(self, delta: float, tags: Iterable[str] = (), bantags: Iterable[str] = ()) -> int:
        """Ladder value for ``delta`` with tag penalty applied."""
        return self.penalty_score(delta, self.penalised_count(tags, bantags))

    # -- delta + score ---------------------------------------------------

    def delta_and_score(
        self,
        problem_rating: float,
        base: float,
        tags: Iterable[str] = (),
        bantags: Iterable[str] = (),
    ) -> tuple[int, int]:
        """``(delta, score)`` for a challenge.

        ``delta`` stays raw (``problem_rating - base``); only ``score`` is
        penalised.  ``problem_rating`` is ``problem.rating`` (forced uniform
        API — AtCoder ``AtCoderProblem.rating`` aliases ``difficulty``).
        """
        delta = int(problem_rating - base)
        # Keep delta int for storage; scoring may use float ladder for AC.
        score = self.final_score(delta, tags, bantags)
        return delta, score


# https://silverfoxxxy.github.io/converter.js
_CF_TO_AC_SCALE: float = 1031 / 780

CF_SCORE_MODEL = GitgudScoreModel(
    -400, 300, 100,
    free_required_tags={"div1"},
    free_banned_tags={"div3", "div4", "edu"},
)

AC_SCORE_MODEL = GitgudScoreModel(
    -400 * _CF_TO_AC_SCALE,
    300 * _CF_TO_AC_SCALE,
    100 * _CF_TO_AC_SCALE,
    free_required_tags={"arc", "agc"},
    free_banned_tags={"abc"},
)
