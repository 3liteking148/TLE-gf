"""Shared helpers for the minigame DB mixins.

Split out of ``minigame_db`` so the result/query and link mixins can share the
union-merge SQL and date helpers without a circular import. Re-exported from
``minigame_db`` for backwards compatibility.
"""

import datetime as dt


_NO_TIME_BOUND = 10 ** 10


def _timestamp_to_date_text(timestamp):
    if timestamp <= 0:
        return None
    return dt.datetime.fromtimestamp(timestamp).date().isoformat()


# Standalone copy of the live∪imported "first attempt per (user, puzzle)" merge
# used by the rating engine and the leaderboards, written so it can run against
# *any* sqlite connection (e.g. an uploaded backup file) rather than only the
# live DB.  Live rows beat imported rows for the same (message_id, puzzle), then
# the earliest message per (user, puzzle) wins — matching
# ``_minigame_filtered_union_query``.  Kept as one statement so the two snapshots
# being diffed are computed identically.
_MERGED_WINNERS_SQL = '''
    WITH minigame_all AS (
        SELECT message_id, user_id, puzzle_number, accuracy, time_seconds, is_perfect
        FROM minigame_result WHERE guild_id = ? AND game = ?
        UNION ALL
        SELECT message_id, user_id, puzzle_number, accuracy, time_seconds, is_perfect
        FROM minigame_import_result imp WHERE guild_id = ? AND game = ?
          AND NOT EXISTS (
              SELECT 1 FROM minigame_result live
              WHERE live.message_id = imp.message_id AND live.game = imp.game
                AND live.puzzle_number = imp.puzzle_number
          )
    ),
    first_per AS (
        SELECT user_id, puzzle_number, MIN(CAST(message_id AS INTEGER)) AS mid
        FROM minigame_all GROUP BY user_id, puzzle_number
    )
    SELECT a.user_id, a.puzzle_number, a.time_seconds, a.is_perfect, a.accuracy
    FROM minigame_all a JOIN first_per f
      ON a.user_id = f.user_id AND a.puzzle_number = f.puzzle_number
     AND CAST(a.message_id AS INTEGER) = f.mid
'''


def merged_minigame_winners(conn, guild_id, game):
    """Return ``{(user_id, puzzle_number): (time_seconds, is_perfect, accuracy)}``.

    The merged first-attempt winner per (user, puzzle) for ``game`` in
    ``guild_id``, computed against the given sqlite ``conn`` independent of its
    row factory — so it works on both the live DB and an uploaded backup file.
    """
    out = {}
    for user_id, puzzle_number, t, perf, acc in conn.execute(
            _MERGED_WINNERS_SQL, (str(guild_id), game, str(guild_id), game)):
        out[(str(user_id), int(puzzle_number))] = (
            int(t), int(bool(perf)), int(acc))
    return out


def diff_merged_winners(old, new):
    """Diff two ``merged_minigame_winners`` dicts (old vs new).

    Returns ``(added, removed, changed)`` where each item is
    ``(key, old_value, new_value)`` and ``key`` is ``(user_id, puzzle_number)``.
    ``added`` are keys only in ``new``; ``removed`` only in ``old``; ``changed``
    are present in both with a different value.  Each list is sorted by puzzle
    number then user id for stable display.
    """
    added, removed, changed = [], [], []
    for key in old.keys() | new.keys():
        o, n = old.get(key), new.get(key)
        if o is None:
            added.append((key, None, n))
        elif n is None:
            removed.append((key, o, None))
        elif o != n:
            changed.append((key, o, n))
    sort_key = lambda item: (item[0][1], item[0][0])
    return (sorted(added, key=sort_key),
            sorted(removed, key=sort_key),
            sorted(changed, key=sort_key))
