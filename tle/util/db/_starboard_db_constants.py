"""Discord-snowflake timestamp constants and helper for starboard queries.

Kept in their own module (with no intra-package imports) so both
``starboard_db`` and ``_starboard_db_queries`` can use them without a circular
import. Re-exported from ``starboard_db`` for backwards compatibility.
"""

# Discord snowflake constants for timestamp extraction.
# A Discord snowflake encodes a timestamp: (snowflake >> 22) + DISCORD_EPOCH_MS
# In SQL we use integer division instead of bitshift: snowflake / SNOWFLAKE_TIMESTAMP_DIVISOR
DISCORD_EPOCH_MS = 1420070400000   # 2015-01-01 00:00:00 UTC in milliseconds
SNOWFLAKE_TIMESTAMP_DIVISOR = 2 ** 22  # 4194304; dividing a snowflake by this gives ms since Discord epoch

# No time bound sentinel — used as default for unbounded date ranges
_NO_TIME_BOUND = 10 ** 10


def snowflake_to_unix_sql(col):
    """Return a SQL expression that converts a Discord snowflake column to a Unix timestamp (seconds).

    Discord snowflake format: (timestamp_ms - DISCORD_EPOCH_MS) << 22 | other_bits
    To extract: (snowflake / 2^22 + DISCORD_EPOCH_MS) / 1000.0 = Unix seconds
    We use integer division (/) instead of bitshift (>>) for SQLite compatibility.
    """
    return f'(CAST({col} AS INTEGER) / {SNOWFLAKE_TIMESTAMP_DIVISOR} + {DISCORD_EPOCH_MS}) / 1000.0'
