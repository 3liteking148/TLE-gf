"""AtCoder API data classes.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Sequence

BASE_URL = 'https://atcoder.jp'


class AtCoderUser(NamedTuple):
    """AtCoder user page scrape."""
    handle: str
    affiliation: Optional[str]
    country: Optional[str]
    rating: str


class AtCoderSubmissionPage(NamedTuple):
    """One parsed submission detail page from atcoder.jp."""
    handle: str
    problem_id: str
    verdict: str

    @property
    def is_ac(self) -> bool:
        return self.verdict == 'AC'


class AtCoderProblem(NamedTuple):
    """An AtCoder problem from kenkoooo's problems.json + problem-models.json.

    ``difficulty`` is the clipped estimated difficulty (None when the problem
    has no model — these are excluded from the gitgud pool). Stored as a
    ``NamedTuple`` to match ``Problem`` and to keep the challenge row as a
    plain tuple; implements ``GitgudProblem`` structurally (no inheritance)
    so ``pyright`` sees it as a concrete type, not an abstract ``Protocol``.
    """
    id: str
    contestId: str
    contest_name: str
    problem_index: str
    name: str
    difficulty: Optional[int] = None
    contest_start: Optional[int] = None

    @property
    def key(self) -> str:
        # The canonical challenge key, mirroring Problem.key on Codeforces.
        return self.id

    @property
    def index(self) -> str:  # type: ignore[reportIncompatibleMethodOverride]
        return self.problem_index.upper()

    @property
    def url(self) -> str:
        return f'{BASE_URL}/contests/{self.contestId}/tasks/{self.id}'

    @property
    def contest_type(self) -> str:
        m = re.match(r'^([a-z]+)', self.contestId)
        assert m is not None
        return m.group(1)

    def has_difficulty(self) -> bool:
        return self.difficulty is not None

    @property
    def rating(self) -> Optional[int]:
        return self.difficulty

    def get_matched_tags(self, tags: Sequence[str]) -> List[str]:
        # AtCoder gitgud filters by contest_type (abc/arc/agc), not CF tags.
        # The generic layer calls this for gimme display; return the matching
        # contest type when requested.
        if not tags:
            return []
        ctype = self.contest_type
        return [t for t in tags if t.strip().lower() == ctype.lower()]


class AtCoderSubmission(NamedTuple):
    """One entry from kenkoooo's per-user submission API."""
    epoch_second: int
    problem_id: str
    result: str

    @property
    def is_ac(self) -> bool:
        return self.result == 'AC'


class AtCoderContest(NamedTuple):
    """One entry from kenkoooo's contests.json."""
    id: str
    start_epoch_second: int
    title: str
