from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Protocol

    from discord.ext import commands

    class HasBot(Protocol):
        bot: commands.Bot

        def __getattr__(self, name: str) -> Any:  # type: ignore[no-redef]
            ...
else:
    class HasBot:
        bot: Any  # type: ignore[no-redef]
