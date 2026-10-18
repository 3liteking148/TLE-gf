from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Sequence, Set, Tuple, TypeAlias

from tle.cogs._gitgud_args import GitgudArgs
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


class GitgudProblem(Protocol):
    """Minimal surface the generic layer expects from a problem."""

    @property
    def rating(self) -> Optional[int]: ...

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

    def matches_all_tags(self, tags: Sequence[str]) -> bool: ...

    def matches_any_tag(self, tags: Sequence[str]) -> bool: ...


class GitgudBackend(Protocol):
    """Platform adapter: fetching/selection only, no Discord output."""

    platform: str
    score_model: GitgudScoreModel
    # Filter-word matching: AtCoder contest types are exact, CF tags prefix.
    exact: bool

    def tag_vocabulary(self) -> Optional[Set[str]]: ...

    def parse_request(self, args: Sequence[str], rating: int,
                      usage: str) -> GitgudArgs: ...

    async def resolve_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> str: ...

    async def validate_handle(self, ctx: GitgudCtx, converter: Optional[GitgudConverter] = None) -> None: ...

    async def fetch_rating(self, handle: str) -> int: ...

    def scale_rating(self, rating: int) -> Tuple[int, int]: ...

    async def fetch_solved(self, handle: str, *, only_ac: bool = True) -> Set[str]: ...

    async def verify_claims(
        self, ctx: GitgudCtx, handle: str, actives: Sequence[ActiveChallenge], submission_url: Optional[str] = None,
        partial: bool = False
    ) -> Tuple[List[ActiveChallenge], List[ActiveChallenge], Dict[int, float]]: ...

    def nogud_set(self, user_id: int) -> Set[str]: ...

    def select(self, req: GitgudArgs, solved: Set[str], noguds: Set[str],
               handle: str, *, hint_climb: bool = False,
               exclude_nonstandard: bool = True,
               ) -> Sequence[GitgudProblem]:
        """Shared ``;gitgud``/``;gimme`` pool for an already-parsed request.

        ``;gimme`` passes ``exclude_nonstandard=False`` so recommendations
        may fall on nonstandard problems; the nogud/solved/tag/date filters
        are shared by both commands.
        """
        ...

    async def fetch_participated(self, handle: str) -> Set[ContestId]: ...

    def select_upsolve_pool(
        self, solved: Set[str], participated: Set[ContestId]
    ) -> Sequence[GitgudProblem]: ...

    def lookup_problem(self, problem_key: str) -> GitgudProblem: ...

    def active_url(self, contest_id: ContestId, problem_key: str, p_index: PIndex = None) -> str: ...
