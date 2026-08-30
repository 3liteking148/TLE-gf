from __future__ import annotations

from typing import Any, Protocol

from discord.ext import commands


class HasBot(Protocol):
    bot: commands.Bot

    def __getattr__(self, name: str) -> Any:  # type: ignore[no-redef]
        ...
