# TLE-gf

TLE-gf is a fork of [TLE](https://github.com/cheran-senthil/TLE), a Discord bot for competitive programming communities. It integrates with Codeforces for problem recommendations, rating tracking, duels, and training. The bot uses discord.py v2, SQLite for persistence, and is structured around cogs (modular command groups).

## File size limit

**Hard rule: every file must be under 500 lines.** This is non-negotiable and applies to ALL files — source, tests, helpers, and scripts. When a file approaches 500 lines, split it before adding more. How to split, by layer:

- **Cogs**: extract cohesive command groups into mixin cogs (the cog class inherits from several `*Mixin` classes) and move pure/module-level helpers into `_`-prefixed sibling modules (e.g. `_minigame_akari.py`, `_starboard_render.py`).
- **DB layer**: extract method groups into `*DbMixin` classes in their own file. `UserDbConn` already composes `MinigameDbMixin`, `StarboardDbMixin`, `MigrationDbMixin` — follow that pattern.
- **Tests**: split by feature area into separate `test_*.py` files; pytest auto-collects them. Shared fixtures/fakes go in an imported helper module (not `conftest.py` unless they're true fixtures).

Splits MUST preserve public behavior: keep cog class names, `setup()`, and command/alias names stable, and re-export any moved symbol that something else imports. Run the test suite after every split.

## What was built

### DB Migration System (`tle/util/db/upgrades.py`, `tle/util/db/user_db_upgrades.py`)

TLE-gf had no schema migration system — every table used `CREATE TABLE IF NOT EXISTS`, so adding columns to existing DBs was silently ignored. We added an `UpgradeRegistry` that tracks a `db_version` table and runs versioned upgrade functions (1.0.0 through 1.4.0). Fresh DBs get stamped at the latest version; existing DBs run pending upgrades.

### Multi-Emoji Starboard (`tle/cogs/starboard.py`)

The original starboard was hardcoded to a single star emoji. We rewrote it to support multiple emojis per guild, each with its own threshold, color, and channel. The schema moved from `starboard`/`starboard_message` to `starboard_config_v1`, `starboard_emoji_v1`, `starboard_message_v1`.

### Starboard Leaderboards

Added `;starboard leaderboard <emoji>` (by message count) and `;starboard star-leaderboard <emoji>` (by total stars). Gated behind a `starboard_leaderboard` guild config flag enabled via `;meta config enable starboard_leaderboard`.

### Background Backfill

A one-time background task runs on startup to populate `author_id` and `star_count` for existing starboard messages by fetching them from Discord. Uses `author_id IS NULL` as a checkpoint — already-processed messages are skipped on restart. Unfetchable messages get an `__UNKNOWN__` sentinel to prevent infinite retries.

### Guild Config System

Key-value config per guild (`guild_config` table). Used for feature gating (e.g., `starboard_leaderboard`). Managed via `;meta config`.

## Key files

| File | What it does |
|---|---|
| `tle/util/db/upgrades.py` | Generic `UpgradeRegistry` class |
| `tle/util/db/user_db_upgrades.py` | Upgrade functions 1.0.0 - 1.4.0 |
| `tle/util/db/user_db_conn.py` | All DB methods (starboard, guild config, leaderboards) |
| `tle/cogs/starboard.py` | Starboard cog (reactions, commands, backfill) |
| `tle/cogs/meta.py` | Meta cog (guild config commands) |
| `tle/constants.py` | `_DEFAULT_STAR_COLOR`, `_DEFAULT_STAR`, `TLE_ADMIN` |
| `tests/conftest.py` | Test setup — stubs discord.py, aiohttp, etc. via `sys.modules` |

## Architecture notes

- **SQLite with namedtuple rows**: `user_db_conn.py` uses `namedtuple_factory` as the row factory, so query results use attribute access (`row.guild_id`). Non-identifier column names (like `SELECT 1`) get aliased to `col_0`.
- **Discord IDs are TEXT in SQLite**: Discord IDs are Python ints but stored as TEXT. All DB methods cast with `str()`.
- **Per-guild asyncio.Lock**: Starboard uses one lock per guild to prevent duplicate starboard posts from concurrent reactions.
- **`INSERT OR IGNORE` for messages, `ON CONFLICT DO UPDATE` for emojis**: Messages should never be overwritten; emoji config upserts must preserve `channel_id`.
- **Backfill checkpointing**: `author_id IS NULL` = pending. `__UNKNOWN__` = unfetchable (excluded from leaderboards). Already-set `author_id` = done.

## Running tests

```bash
python3 -m pytest tests/ -v
```

Tests stub out discord.py, aiohttp, and other heavy deps in `conftest.py` so they run against in-memory SQLite without the full bot environment.

## Workflow rules

**Always commit after completing a task.** Every discrete unit of work (feature, bugfix, refactor, test addition) must be committed immediately after tests pass. Do not wait for the user to ask — just commit. If multiple tasks are requested in sequence, commit after each one.

## Commits convention

Use imperative mood, short first line. `Co-Authored-By` trailer when AI-assisted. Do not use `$()` command substitution in commit messages — use a plain string with `-m`.
