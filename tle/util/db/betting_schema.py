"""Betting schema (DDL) — one shared base table + per-kind extensions.

The archived soccer game and user-created custom markets share a single
``bet_market`` base (lifecycle + display fields) and a single ``bet_wager``
table; kind-specific columns live in ``bet_market_soccer`` / ``bet_market_custom``
extensions keyed by ``market_id``. ``market_id`` is therefore globally unique,
which lets the shared store and leaderboards work across both kinds without
per-kind table parameters.

Soccer data is preserved verbatim (``commence_time`` becomes the base
``closes_at``); custom markets reuse the base and store their title in the
custom extension. The base carries a per-market ``removable`` flag (whether a
bettor may remove a wager); soccer defaults it on, custom markets open with it
off. Used by the fresh-schema path and migration 1.61.0 so the two schemas
cannot drift. Does not commit.
"""


def create_betting_tables(conn):
    """Create the shared betting tables (idempotent).

    On a pre-1.61.0 database the legacy soccer ``bet_market`` still exists
    without a ``kind`` column; the market base + indexes are then skipped here
    and left to migration 1.61.0, which rebuilds the table. The remaining tables
    (wallet, wager, custom choices) are safe to ensure in either case.
    """
    existing = _columns(conn, 'bet_market')
    legacy = bool(existing) and 'kind' not in existing

    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_wallet (
            guild_id    TEXT NOT NULL,
            user_id     TEXT NOT NULL,
            balance     INTEGER NOT NULL,
            last_daily  TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    ''')
    if not legacy:
        _create_market_base(conn)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_market_soccer (
            market_id   INTEGER PRIMARY KEY,
            sport_key   TEXT NOT NULL,
            home_team   TEXT NOT NULL,
            away_team   TEXT NOT NULL,
            odds_home   REAL NOT NULL,
            odds_draw   REAL NOT NULL,
            odds_away   REAL NOT NULL,
            result_home INTEGER,
            result_away INTEGER
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_market_custom (
            market_id   INTEGER PRIMARY KEY,
            title       TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_custom_choice (
            market_id   INTEGER NOT NULL,
            choice_key  TEXT NOT NULL,
            label       TEXT NOT NULL,
            position    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (market_id, choice_key)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_wager (
            market_id   INTEGER NOT NULL,
            user_id     TEXT NOT NULL,
            pick        TEXT NOT NULL,
            stake       INTEGER NOT NULL,
            placed_at   REAL NOT NULL,
            PRIMARY KEY (market_id, user_id, pick)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_wallet_txn (
            txn_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id      TEXT NOT NULL,
            user_id       TEXT NOT NULL,
            actor_id      TEXT,
            action        TEXT NOT NULL,
            amount        INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            market_id     INTEGER,
            note          TEXT,
            created_at    REAL NOT NULL
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bet_wallet_txn_user
            ON bet_wallet_txn (guild_id, user_id, created_at DESC)
    ''')


def _create_market_base(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS bet_market (
            market_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            kind            TEXT NOT NULL,
            guild_id        TEXT NOT NULL,
            channel_id      TEXT NOT NULL,
            message_id      TEXT,
            thread_id       TEXT,
            thread_intro_id TEXT,
            event_id        TEXT,
            fixture_key     TEXT,
            created_by      TEXT NOT NULL,
            created_at      REAL NOT NULL,
            closes_at       REAL NOT NULL,
            status          TEXT NOT NULL DEFAULT 'open',
            bets_closed     INTEGER NOT NULL DEFAULT 0,
            result          TEXT,
            settled_at      REAL,
            thread_locked   INTEGER NOT NULL DEFAULT 0,
            removable       INTEGER NOT NULL DEFAULT 1
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bet_market_active
            ON bet_market (guild_id, channel_id, status)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bet_market_pending
            ON bet_market (status, closes_at)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_bet_market_thread
            ON bet_market (guild_id, thread_id, status)
    ''')
    # Open-market dedup for soccer: one open market per provider event and per
    # canonical fixture. Custom rows leave both NULL (excluded by the partial
    # predicate), so they can open freely.
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bet_market_open_event
            ON bet_market (guild_id, event_id)
            WHERE status = 'open' AND event_id IS NOT NULL
    ''')
    conn.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bet_market_open_fixture
            ON bet_market (guild_id, fixture_key)
            WHERE status = 'open' AND fixture_key IS NOT NULL
    ''')


def _columns(conn, table):
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
