"""ThemeCP progression data — CSV loader and positional bonus math.

Pure utility: stdlib only, no discord, no ``cogs`` imports. ``themecp.csv``
columns ``P1..P4`` are ThemeCP ratings A-D in order, matching ``THEME_MULTS``
index 0..3 (1.0, 1.0, 1.5, 2.0). Mults use ``round(score*mult)`` and apply to
any multi-challenge batch whose id carries a known level (``prog-{level}-…``)
when each slot is solved within the per-level window. Cog-level concerns (arg parsing,
problem selection, embeds — all of which raise ``CodeforcesCogError``) live
in ``tle/cogs/_gitgud_progression.py``.
"""
from __future__ import annotations

import csv
import logging
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Sequence, Tuple

logger = logging.getLogger(__name__)


# Fixed bonus per position A-D
THEME_MULTS: Tuple[float, float, float, float] = (1.0, 1.0, 1.5, 2.0)


@dataclass(frozen=True)
class ThemeLevel:
    ratings: Tuple[int, int, int, int]
    time: int  # seconds
    perf: int


THEME: Dict[int, ThemeLevel] = {}


def _csv_path() -> Path:
    return Path(__file__).resolve().with_name("themecp.csv")


def _load_csv() -> None:
    if THEME:
        return
    csv_path = _csv_path()
    if not csv_path.exists():
        logger.warning("gitgud_progression: %s not found, ThemeCP disabled", csv_path)
        return
    try:
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    lvl = int(row["Level"])
                    t_sec = int(row["Time"]) * 60
                    perf = int(row["Perf"])
                    ratings = (int(row["P1"]), int(row["P2"]), int(row["P3"]), int(row["P4"]))
                except Exception:
                    logger.warning("gitgud_progression: skipping malformed row %r", row)
                    continue
                THEME[lvl] = ThemeLevel(ratings=ratings, time=t_sec, perf=perf)
    except Exception as e:
        logger.warning("gitgud_progression: failed to load %s: %s", csv_path, e)


@lru_cache(maxsize=1)
def _loaded() -> bool:
    _load_csv()
    return True


def theme_of(level: int) -> ThemeLevel | None:
    """ThemeLevel for *level*, or None when unknown."""
    _loaded()
    return THEME.get(level)


def max_level() -> int:
    _loaded()
    return max(THEME) if THEME else 0


def is_progression_batch(batch_id: str) -> bool:
    return isinstance(batch_id, str) and batch_id.startswith("prog-")


def _parse_progression_level(batch_id: str) -> int | None:
    if not is_progression_batch(batch_id):
        return None
    try:
        # prog-{level}-{rest}
        return int(batch_id.split("-", 2)[1])
    except Exception:
        return None


def bonus_scores(scores: Sequence[int], mults: Sequence[float] = THEME_MULTS) -> list[int]:
    """Apply per-position multipliers with ``round(score*mult)``."""
    return [round(s * m) for s, m in zip(scores, mults)]


def get_progression_bonus_context(batch_id: str) -> Tuple[int, Tuple[float, float, float, float]] | None:
    """Return ``(window, mults)`` for a batch with a known level, else None."""
    lvl = _parse_progression_level(batch_id)
    if lvl is None:
        return None
    theme = theme_of(lvl)
    if theme is None:
        return None
    return theme.time, THEME_MULTS


def compute_bonus_scores(
    base_scores: Sequence[int], batch_id: str, solve_times: Sequence[float], issue_time: float,
    solved: Sequence[bool] | None = None,
) -> tuple[list[int], int | None, Tuple[float, ...] | None]:
    """Bonus decision for ``_finalize_challenges``.

    Returns ``(scores, window, mults)``. Mults apply only when the batch has
    a known level and they change the total; otherwise the base scores pass
    through with ``(None, None)`` context. Singletons never get a bonus.
    Callers compare stored vs base scores to decide whether a bonus line is
    shown, so partial claims of flat slots correctly show none.

    ``solve_times`` holds each batch position's solve epoch in batch order
    (CF first-AC times from the API); a slot is bonused only when solved
    within ``issue_time + window``. Late slots keep base points without
    voiding earlier slots' bonuses. ``solved`` marks which batch positions
    were solved, in batch order; ``None`` means all solved (strict claim /
    legacy callers). Mults apply only to the unbroken solved streak starting
    at slot A: the first unsolved position voids the bonus for it and every
    later slot, so a lone D (or any gapped set) scores base points.
    """
    base = list(base_scores)
    if len(base) <= 1:
        return base, None, None
    bonus_ctx = get_progression_bonus_context(batch_id)
    if bonus_ctx is None:
        return base, None, None
    window, mults = bonus_ctx
    deadline = issue_time + window
    cand = list(base)
    for i, (s, m) in enumerate(zip(base, mults)):
        if solved is not None and not solved[i]:
            break
        if solve_times[i] <= deadline:
            cand[i] = round(s * m)
    if cand != base:
        return cand, window, mults
    return base, window, mults


def biased_choice(pool: Sequence, k: int = 5) -> int:
    """Index bias toward the end of the pool, as in ``;gitgud``/``;gimme``."""
    return max(random.randrange(len(pool)) for _ in range(k))
