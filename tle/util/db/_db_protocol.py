"""Checker-only protocols for DB mixins.

Mixins like ``HandleDbMixin`` use ``self.conn`` but never declare it.
``HasConn`` gives the checker the missing attribute without touching MRO
behaviour (``Protocol`` would bring ``_ProtocolMeta``/``ABCMeta`` into the
MRO and conflict with ``CogMeta``).
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Protocol

    class HasConn(Protocol):
        conn: sqlite3.Connection
else:
    class HasConn:
        conn: Any  # type: ignore[no-redef]
