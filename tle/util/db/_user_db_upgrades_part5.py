"""User DB upgrade functions — part 5 (1.55.0+).

Split from ``_user_db_upgrades_part4.py`` to keep every module under the
500-line limit. Imported (for side-effect registration) and re-exported by
``user_db_upgrades.py``.
"""
import logging
import os

from tle.util.db._user_db_upgrade_registry import registry
from tle.util.db import _challenge_upgrade_reconstruct as _cur

logger = logging.getLogger(__name__)


@registry.register('1.55.0', 'Challenge platform column')
def upgrade_1_55_0(db):
    """Tag ``challenge`` rows with a ``platform`` column.

    The existing ``problem_name`` column stays as the platform-canonical
    problem key (the problem *name* for Codeforces — the CF cache dedupes by
    name, so it is a safe key — and the problem *id*, e.g. ``abc383_a``, for
    AtCoder). AtCoder contest ids are stored as text inside the integer
    ``contest_id`` and only ever formatted into URLs, never compared
    numerically.

    ``p_index`` keeps its original meaning: the problem's index within its
    contest. Codeforces rows already hold the index letter (e.g. ``'A'``,
    ``'C1'``), which the old code used to build problem URLs; AtCoder rows
    store the letter after the underscore of the problem id
    (``abc383_a`` -> ``'A'``). The new code writes ``p_index`` on every
    insert (the column is NOT NULL in the original schema, so a fresh insert
    that omits it would fail) and uses it to build Codeforces problem URLs
    without consulting the problem cache. ``problem_name`` is NOT renamed —
    it is reused as-is for both platforms for legacy compatibility.

    Fresh databases get the same schema from ``ChallengeDbMixin``'s DDL, so
    the upgrade is a no-op when it already exists.
    """
    logger.info('1.55.0: Adding challenge platform column')
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if 'challenge' not in tables:
        logger.info('1.55.0: challenge table absent; nothing to migrate')
        return
    columns = {
        row[1] for row in db.execute(
            'PRAGMA table_info(challenge)').fetchall()
    }
    if 'platform' not in columns:
        db.execute(
            "ALTER TABLE challenge ADD COLUMN platform TEXT NOT NULL DEFAULT 'cf'")
    db.commit()
    logger.info('1.55.0: Upgrade complete')


@registry.register('1.56.0', 'Challenge score column and honest rating deltas')
def upgrade_1_56_0(db, warning_log_path=None):
    """Give challenges an explicit ``score`` column and repair deltas.

    The Jul 2026 tag-penalty change stored penalised scores as sentinel
    ``rating_delta`` values (``-10**9 - score``), destroying the column's
    meaning and breaking howgud's histogram. This migration:

    1. adds ``score INTEGER`` to ``challenge`` (also present in the fresh-DB
       DDL, which never runs migrations);
    2. backfills ``score`` for every row — decoded exactly for sentinel rows,
       plain ladder otherwise — so points history is frozen at what was paid;
    3. rewrites cf-platform ``rating_delta`` values to honest
       ``problem_rating - clamped_rounded_base`` using local caches first
       (cache.db ``problem``/``rating_change``, ``cf_user_cache``) and the
       Codeforces API only for sentinel handles without local coverage.
       Legacy flat ``-200`` tagged rows are normalised; rows whose
       reconstruction disagrees unexpectedly keep their value.

    Unresolvable rows are reported in ``migration_warnings.log`` (repo root)
    and left untouched; howgud filters implausible deltas defensively. The
    whole upgrade is idempotent.
    """
    logger.info('1.56.0: Adding challenge score column')
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if 'challenge' not in tables:
        logger.info('1.56.0: challenge table absent; nothing to migrate')
        return
    columns = {
        row[1] for row in db.execute(
            'PRAGMA table_info(challenge)').fetchall()
    }
    if 'score' not in columns:
        db.execute('ALTER TABLE challenge ADD COLUMN score INTEGER')

    if warning_log_path is None:
        warning_log_path = os.environ.get(
            'TLE_MIGRATION_WARNING_LOG',
            os.path.join(os.getcwd(), _cur.WARNING_LOG_FILENAME))
    warn = _cur.make_warning_logger(warning_log_path)

    scored = _cur.backfill_scores(db, warn)
    logger.info('1.56.0: scored %d challenge row(s)', scored)

    cache_conn = _cur.open_cache_db_readonly(_cache_db_path())
    try:
        problems, histories, currents = _cur.collect_local_sources(cache_conn)
    finally:
        if cache_conn is not None:
            cache_conn.close()
    stats = _cur.reconstruct_deltas(
        db, warn,
        problems=problems,
        histories=histories,
        currents=currents,
        user_handles=_cur.collect_user_handles(db),
        api_fetch=_cur.fetch_rating_history_api,
    )
    logger.info('1.56.0: delta repair stats: %s', stats)
    db.commit()
    logger.info('1.56.0: Upgrade complete')


def _cache_db_path():
    """Locate cache.db next to user.db; ``None`` when unavailable."""
    try:
        from tle import constants
        return constants.CACHE_DB_FILE_PATH
    except Exception:
        return None
