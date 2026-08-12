"""User DB upgrade functions — part 5 (1.55.0+).

Split from ``_user_db_upgrades_part4.py`` to keep every module under the
500-line limit. Imported (for side-effect registration) and re-exported by
``user_db_upgrades.py``.
"""
import logging

from tle.util.db._user_db_upgrade_registry import registry

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
