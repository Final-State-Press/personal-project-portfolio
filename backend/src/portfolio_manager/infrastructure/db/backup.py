"""User-facing database backups.

A backup is a complete, verified copy of the database made with SQLite's online
backup API, so it is consistent even while the app is writing (a plain file copy
of a WAL database can be stale or unreadable). Backups live in a ``backups``
folder next to the database, are never overwritten, and the newest
:data:`KEEP_BACKUPS` are kept.

To restore, quit the app and replace ``portfolio.db`` with a backup file (see
``docs/backup-and-restore.md``).
"""

import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from portfolio_manager.domain.errors import DatabaseError, ValidationError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection

logger = logging.getLogger(__name__)

#: Number of backups to keep.
KEEP_BACKUPS = 7

#: A new automatic backup is made when the newest one is older than this.
AUTO_BACKUP_INTERVAL = timedelta(hours=24)

_PREFIX = "portfolio-"
_STAMP = "%Y%m%dT%H%M%S%fZ"


@dataclass(frozen=True)
class BackupInfo:
    """One backup file.

    :param name: File name.
    :param path: Full path.
    :param created_at: When it was made (UTC).
    :param size_bytes: File size.
    """

    name: str
    path: Path
    created_at: datetime
    size_bytes: int


def backups_dir(db_path: Path | str) -> Path:
    """Return the folder that holds backups for the database at *db_path*."""
    return Path(db_path).parent / "backups"


def _parse(path: Path) -> BackupInfo | None:
    try:
        stamp = path.name[len(_PREFIX) : -len(".db")]
        created = datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
        return BackupInfo(path.name, path, created, path.stat().st_size)
    except (ValueError, OSError):
        return None


def list_backups(db_path: Path | str) -> list[BackupInfo]:
    """List backups, newest first.

    :param db_path: Path of the live database.
    :rtype: list[BackupInfo]
    """
    folder = backups_dir(db_path)
    found = [b for p in folder.glob(f"{_PREFIX}*.db") if (b := _parse(p)) is not None]
    return sorted(found, key=lambda b: b.created_at, reverse=True)


def _write_verified(db: DatabaseConnection, dest: Path) -> None:
    """Write a verified copy of the live database to *dest* (atomically; no partial file)."""
    tmp = dest.with_suffix(".db.partial")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(tmp))
        try:
            db.conn.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
        if result != "ok":
            raise DatabaseError(f"The backup failed verification: {result}")
        tmp.replace(dest)
    except (OSError, sqlite3.Error, DatabaseError) as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        if isinstance(exc, DatabaseError):
            raise
        raise DatabaseError(f"Could not write the backup: {exc}") from exc


def create_backup(db: DatabaseConnection, *, keep: int = KEEP_BACKUPS) -> BackupInfo:
    """Write a verified backup of the live database.

    :param db: Active connection.
    :param keep: How many backups to keep afterwards.
    :returns: The new backup.
    :raises DatabaseError: If the backup could not be written or failed verification.
        No partial file is left behind.
    """
    if str(db.path) == ":memory:":
        raise DatabaseError("An in-memory database cannot be backed up.")
    dest = backups_dir(db.path) / f"{_PREFIX}{datetime.now(UTC).strftime(_STAMP)}.db"
    _write_verified(db, dest)
    _prune(db.path, keep)
    info = _parse(dest)
    assert info is not None  # noqa: S101 - name was just built by this module
    logger.info("Backup written to %s", dest)
    return info


def export_backup(db: DatabaseConnection, destination: Path | str) -> Path:
    """Write a verified copy of the database into a folder the user chose.

    :param db: Active connection.
    :param destination: An existing, absolute folder.
    :returns: The file written.
    :raises ValidationError: If the folder is not usable.
    :raises DatabaseError: If the copy fails.
    """
    folder = Path(destination)
    if not folder.is_absolute() or not folder.is_dir():
        raise ValidationError("Choose an existing folder to export to.")
    dest = folder / f"portfolio-export-{datetime.now(UTC).strftime(_STAMP)}.db"
    _write_verified(db, dest)
    logger.info("Database exported to %s", dest)
    return dest


def _prune(db_path: Path | str, keep: int) -> None:
    for old in list_backups(db_path)[keep:]:
        with contextlib.suppress(OSError):
            old.path.unlink()
    for partial in backups_dir(db_path).glob(f"{_PREFIX}*.db.partial"):
        with contextlib.suppress(OSError):
            partial.unlink()


def backup_if_due(
    db: DatabaseConnection, *, max_age: timedelta = AUTO_BACKUP_INTERVAL
) -> BackupInfo | None:
    """Make an automatic backup if there is data and the newest backup is old.

    Never raises: an automatic backup failing must not stop the app. Failures are
    logged.

    :param db: Active connection.
    :param max_age: Minimum age of the newest backup before another is made.
    :returns: The new backup, or ``None`` if none was due or it failed.
    """
    if str(db.path) == ":memory:":
        return None
    try:
        has_data = db.fetchone("SELECT 1 FROM project LIMIT 1") is not None
        if not has_data:
            return None
        existing = list_backups(db.path)
        if existing and datetime.now(UTC) - existing[0].created_at < max_age:
            return None
        return create_backup(db)
    except Exception:
        logger.exception("Automatic backup failed")
        return None
