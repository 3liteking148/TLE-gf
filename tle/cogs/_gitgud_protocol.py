from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, TypeAlias, runtime_checkable

from tle.cogs._gitgud_scoring import GitgudScoreModel
from tle.util.db.challenge_db import ActiveChallenge

if TYPE_CHECKING:
    from discord.ext.commands import Context as DiscordContext
    from discord.ext.commands import Converter as DiscordConverter
    GitgudCtx = DiscordContext
    GitgudConverter = DiscordConverter
else:
    GitgudCtx = Any
    GitgudConverter = Any

ContestId: TypeAlias = str | int
PIndex: TypeAlias = str | None
# Re-exported from the DB layer (see ``challenge_db.ActiveChallenge``).
# contest_id is str|int (AtCoder str, CF int); PIndex is plain str (legacy int dead).
# Field order is stable so positional indexing/unpacking keeps working for legacy sites.
ChallengeLogEntry: TypeAlias = Tuple[float, Optional[float], str, int, int, str, int]


class GitgudProblem(Protocol):
    """Minimal surface the generic layer expects from a problem."""

    @property
    def rating(self) -> Optional[int]: ...  # AtCoder difficulty alias

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def index(self) -> str: ...

    @property
    def contestId(self) -> Optional[ContestId]: ...

    @property
    def contest_name(self) -> str: ...

    @property
    def key(self) -> str: ...

    def get_matched_tags(self, tags: Sequence[str]) -> List[str]: ...


@runtime_checkable
class GitgudBackend(Protocol):
    """Data-only, platform-specific gitgud operations."""

    platform: str
    score_model: GitgudScoreModel

    def parse_args(
        self, args: Sequence[str], rating: int
    ) -> Tuple[int, int, bool, List[str], List[str]]:
        ...

    async def resolve_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> str:
        ...

    async def validate_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> None:
        ...

    async def fetch_rating(self, handle: str) -> int:
        ...

    def scale_rating(self, rating: int) -> Tuple[int, int]:
        ...

    async def fetch_solved(self, handle: str, *, only_ac: bool = True) -> Set[str]:
        ...

    async def verify_claims(
        self, ctx: GitgudCtx, handle: str, actives: Sequence[ActiveChallenge], submission_url: Optional[str] = None,
        partial: bool = False
    ) -> Tuple[List[ActiveChallenge], List[ActiveChallenge], Dict[int, float]]:
        """Batch-aware claim check; returns done, missing and solve times.

        Returns ``(done, missing, solve_times)`` preserving batch order, where
        ``solve_times`` maps each done ``challenge_id`` to its solve epoch
        (CF: first AC from the API at/after ``issue_time`` minus the claim
        margin; AtCoder: claim time, since the submission page timestamp
        is not parsed). On the strict path
        (``partial=False``) missing is always empty — any unsolved challenge
        raises instead. Callers resolve ``done`` as completions and ``missing``
        as skips.
        """
        ...

    def nogud_set(self, user_id: int) -> Set[str]:
        ...

    def select_pool(
        self,
        srating: int,
        erating: int,
        solved: Set[str],
        noguds: Set[str],
        tags: List[str],
        bantags: List[str],
        handle: str,
        *,
        hint_climb: bool = False,
    ) -> Sequence[GitgudProblem]:
        """Pool for ``[srating, erating]``.

        ``hint_climb`` tells the backend the window came from the default
        rating (no rating argument) so it may climb to higher ratings when
        the window is empty; backends without a ladder ignore it. Callers
        with a fixed rating window must leave it unset.
        """
        ...

    async def fetch_participated(self, handle: str) -> Set[int]:
        ...

    def select_upsolve_pool(
        self, solved: Set[str], participated: Set[int]
    ) -> Sequence[GitgudProblem]:
        ...

    def select_gimme_pool(
        self, args: Sequence[str], handle: str, solved: Set[str], rating: int
    ) -> Tuple[Sequence[GitgudProblem], List[str], bool]:
        ...

    def lookup_problem(self, problem_key: str) -> GitgudProblem:
        ...

    def active_url(self, contest_id: ContestId, problem_key: str, p_index: PIndex = None) -> str:
        ...