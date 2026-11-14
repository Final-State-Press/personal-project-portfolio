"""Startup integrity check.

Runs ``PRAGMA quick_check`` before anything touches the data, so damage is found
at launch with a specific message, not by accident later as a confusing error.
"""

import logging
import sqlite3

from portfolio_manager.domain.errors import DatabaseCorruptError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection, is_corruption

logger = logging.getLogger(__name__)


def check_integrity(db: DatabaseConnection) -> None:
    """Verify the database file is structurally sound.

    :param db: Active connection.
    :raises DatabaseCorruptError: If SQLite reports damage.
    :raises sqlite3.Error: For any other failure (full disk, I/O, lock), unchanged,
        so it is never mistaken for corruption.
    """
    try:
        rows = db.conn.execute("PRAGMA quick_check").fetchall()
    except sqlite3.DatabaseError as exc:
        if is_corruption(exc):
            raise DatabaseCorruptError(_message(db, str(exc))) from exc
        raise
    problems = [r[0] for r in rows if r[0] != "ok"]
    if problems:
        detail = "; ".join(problems[:3])
        raise DatabaseCorruptError(_message(db, detail))
    logger.debug("Database integrity check passed.")


def _message(db: DatabaseConnection, detail: str) -> str:
    return (
        f"The database file {db.path} is damaged ({detail}). "
        "Nothing was changed. If you have a backup, restore it; backups made before "
        "upgrades are the .bak files next to the database."
    )
