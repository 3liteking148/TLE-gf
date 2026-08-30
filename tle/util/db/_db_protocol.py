"""Checker-only protocols for DB mixins.

Mixins like ``HandleDbMixin`` use ``self.conn`` but never declare it.
``HasConn`` gives the checker the missing attribute without touching MRO
behaviour (``Protocol`` is empty at runtime).
"""
from __future__ import annotations

import sqlite3
from typing import Protocol


class HasConn(Protocol):
    conn: sqlite3.Connection
