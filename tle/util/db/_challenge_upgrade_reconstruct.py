"""Offline-first repair helpers for upgrade 1.56.0 (challenge scores/deltas).

Between Jul 3 2026 and this migration, challenges issued with penalised tags
stored ``rating_delta = -10**9 - score`` (a sentinel encoding) instead of the
real rating difference, and older tagged challenges stored ``delta - 200``.
This module restores honest data:

* every row gets an explicit ``score`` column value (what was actually paid),
* ``rating_delta`` is rewritten to ``problem_rating - clamped_rounded_base``
  using the bot's own local caches (cache.db ``problem`` / ``rating_change``
  tables, ``cf_user_cache`` snapshots), falling back to the Codeforces API
  only for handles whose sentinel rows lack local history coverage.

Scores are never recomputed from repaired deltas — they are decoded from the
stored values first, so nobody's points move. Rows that cannot be honestly
reconstructed are left untouched and reported to a warning log; howgud
filters implausible deltas defensively.
"""
import datetime
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# Reserved range the Jul 2026 sentinel encoding lived in. Real deltas are
# bounded by the rating scale (roughly -2600..300), nothing comes near this.
SENTINEL_THRESHOLD = -(10**8)
_SENTINEL_BASE = -(10**9)
# Flat shift tagged challenges carried between Aug 2021 and Jul 2026.
LEGACY_TAG_PENALTY = 200
DEFAULT_RATING = 800
# CF anonymous API allows roughly one request per second.
_API_CALL_SLEEP_SECONDS = 1.0
MAX_API_CALLS = 25
WARNING_LOG_FILENAME = 'migration_warnings.log'


def decode_sentinel_score(rating_delta):
    """Score hidden in a sentinel delta, or ``None`` for real deltas."""
    score = _SENTINEL_BASE - rating_delta
    if 1 <= score <= 23:
        return score
    return None


def is_sentinel_delta(rating_delta):
    return rating_delta is not None and rating_delta <= SENTINEL_THRESHOLD


def clamp_base(rating):
    """Issue-time delta base: nearest 100, clamped to [1100, 3000]."""
    return max(1100, min(3000, round(rating, -2)))


def rating_at(history, issue_time):
    """Rating in effect at ``issue_time`` given ascending ``(time, rating)``
    pairs; ``None`` when history does not reach back that far."""
    result = None
    for update_time, new_rating in history:
        if update_time <= issue_time:
            result = new_rating
        else:
            break
    return result


def open_cache_db_readonly(cache_path):
    """Read-only connection to cache.db, or ``None`` when unavailable."""
    if not cache_path:
        return None
    try:
        conn = sqlite3.connect(f'file:{cache_path}?mode=ro', uri=True)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {'problem', 'rating_change'} & tables:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def collect_local_sources(cache_conn):
    """Gather ``(problem_ratings, histories, current_ratings)`` from cache.db.

    ``problem_ratings`` maps problem name -> rating, ``histories`` maps
    handle -> ascending ``(rating_update_time, new_rating)`` pairs, and
    ``current_ratings`` maps handle -> latest known rating. All empty when
    the connection is ``None``.
    """
    problem_ratings, histories, current_ratings = {}, {}, {}
    if cache_conn is None:
        return problem_ratings, histories, current_ratings
    try:
        for name, rating in cache_conn.execute(
                'SELECT name, rating FROM problem WHERE rating IS NOT NULL'):
            problem_ratings[name] = rating
        for handle, update_time, new_rating in cache_conn.execute(
                'SELECT handle, rating_update_time, new_rating '
                'FROM rating_change ORDER BY handle, rating_update_time'):
            histories.setdefault(handle, []).append((update_time, new_rating))
            current_ratings[handle] = new_rating
    except sqlite3.Error as error:
        logger.warning('1.56.0: reading cache.db sources failed: %s', error)
    return problem_ratings, histories, current_ratings


def collect_user_handles(db):
    """Most recent active CF handle per Discord user id, from user.db."""
    handles = {}
    try:
        for user_id, handle in db.execute(
                'SELECT user_id, handle FROM user_handle WHERE active = 1 '
                'ORDER BY updated_at'):
            handles[str(user_id)] = handle
    except sqlite3.Error as error:
        logger.warning('1.56.0: reading user_handle failed: %s', error)
    return handles


def fetch_rating_history_api(handle, timeout=10):
    """Full rating history for ``handle`` from the Codeforces API, or
    ``None``. Ascending ``(ratingUpdateTimeSeconds, newRating)`` pairs."""
    url = ('https://codeforces.com/api/user.rating?handle='
           + urllib.parse.quote(handle))
    request = urllib.request.Request(
        url, headers={'User-Agent': 'tle-gf-migration/1.56.0'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as error:  # network, HTTP, JSON — all non-fatal
        logger.warning('1.56.0: user.rating(%r) failed: %s', handle, error)
        return None
    if payload.get('status') != 'OK':
        return None
    return [(change['ratingUpdateTimeSeconds'], change['newRating'])
            for change in payload.get('result', [])]


def make_warning_logger(path):
    """Append timestamped lines to ``path`` (and the app log). Never raises."""
    def warn(message):
        line = (f'{datetime.datetime.now().isoformat(timespec="seconds")} '
                f'{message}')
        logger.warning('1.56.0: %s', message)
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        except OSError:
            pass
    return warn


def backfill_scores(db, warn):
    """Pass A: give every challenge row its explicit ``score``.

    Sentinel-encoded rows decode to the exact score that was paid out; all
    other rows take the plain ladder value of their stored delta. Purely
    offline and idempotent.
    """
    from tle.cogs._codeforces_helpers import _calculateGitgudScoreForDelta
    rows = db.execute(
        'SELECT id, rating_delta FROM challenge').fetchall()
    fixed = 0
    for row_id, rating_delta in rows:
        if is_sentinel_delta(rating_delta):
            score = decode_sentinel_score(rating_delta)
            if score is None:
                score = _calculateGitgudScoreForDelta(rating_delta)
                warn(f'challenge {row_id}: sentinel delta {rating_delta} '
                     f'does not decode; scored {score} from ladder')
        else:
            score = _calculateGitgudScoreForDelta(rating_delta)
        db.execute('UPDATE challenge SET score = ? WHERE id = ?',
                   (score, row_id))
        fixed += 1
    db.commit()
    return fixed


def reconstruct_deltas(db, warn, *, problems, histories, currents,
                       user_handles, api_fetch=None,
                       max_api_calls=MAX_API_CALLS):
    """Pass B: rewrite cf-platform ``rating_delta`` values to honest ones.

    Per row, using the issue-time base replicated from the live code
    (``max(1100, min(3000, round(rating, -2)))``):

    * sentinel rows always get the reconstructed delta (approximation noted
      when only the current snapshot was available);
    * untagged rows must reproduce their stored value exactly — mismatches
      mean our reconstruction drifted (handle change, history gap), so the
      original stays and a warning is written;
    * legacy tagged rows are recognised by the exact ``+200`` offset and
      normalised;
    * anything else unexpected keeps its value and warns.
    """
    rows = db.execute(
        "SELECT id, user_id, issue_time, problem_name, rating_delta "
        "FROM challenge WHERE platform = 'cf'").fetchall()
    api_budget = max_api_calls
    fetched_histories = {}
    stats = {'sentinel': 0, 'normalized': 0, 'skipped': 0}

    for row_id, user_id, issue_time, problem_name, old_delta in rows:
        problem_rating = problems.get(problem_name)
        if problem_rating is None:
            warn(f'challenge {row_id}: problem {problem_name!r} has no '
                 f'cached rating; delta left unchanged')
            stats['skipped'] += 1
            continue
        handle = user_handles.get(str(user_id))
        if handle is None:
            warn(f'challenge {row_id}: user {user_id} has no active CF '
                 f'handle; delta left unchanged')
            stats['skipped'] += 1
            continue

        history = histories.get(handle)
        rating = rating_at(history, issue_time) if history else None
        approximated = False
        if rating is None and is_sentinel_delta(old_delta):
            if api_fetch is not None and api_budget > 0:
                if handle not in fetched_histories:
                    if api_budget < max_api_calls:
                        time.sleep(_API_CALL_SLEEP_SECONDS)
                    api_budget -= 1
                    fetched_histories[handle] = api_fetch(handle)
                fetched = fetched_histories[handle]
                if fetched:
                    rating = rating_at(fetched, issue_time)
                    if rating is not None:
                        histories.setdefault(handle, []).extend(
                            (t, r) for t, r in fetched
                            if (t, r) not in histories.get(handle, []))
        if rating is None:
            rating = currents.get(handle)
            approximated = True
        if rating is None:
            warn(f'challenge {row_id}: no rating known for handle '
                 f'{handle!r}; delta left unchanged')
            stats['skipped'] += 1
            continue

        new_delta = problem_rating - clamp_base(rating)
        if is_sentinel_delta(old_delta):
            db.execute('UPDATE challenge SET rating_delta = ? WHERE id = ?',
                       (new_delta, row_id))
            stats['sentinel'] += 1
            if approximated:
                warn(f'challenge {row_id}: sentinel delta approximated as '
                     f'{new_delta} from the current rating of {handle!r}')
        elif new_delta == old_delta:
            pass
        elif new_delta == old_delta + LEGACY_TAG_PENALTY:
            db.execute('UPDATE challenge SET rating_delta = ? WHERE id = ?',
                       (new_delta, row_id))
            stats['normalized'] += 1
        else:
            warn(f'challenge {row_id}: reconstructed delta {new_delta} '
                 f'differs unexpectedly from stored {old_delta}; kept the '
                 f'stored value')
            stats['skipped'] += 1
    db.commit()
    return stats
