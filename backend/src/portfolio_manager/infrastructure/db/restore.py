"""Restoring a backup.

A running app holds the database open, so a restore is *requested* (a small marker
file) and applied at the next launch, before the database is opened. That also
makes it work when the database is too damaged to open.

The swap is crash-safe: the backup is verified first, the current database is kept
aside (``backups/before-restore-<time>.db``, newest 3 kept), and the new file is
moved into place with an atomic rename, so ``portfolio.db`` is always either the
old file or the new one, never missing or half-written.
"""

import contextlib
import json
import logging
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from portfolio_manager.domain.errors import DatabaseError, NotFoundError
from portfolio_manager.infrastructure.db.backup import backups_dir, list_backups

logger = logging.getLogger(__name__)

_MARKER = "restore-pending.json"
_KEEP_ASIDE = 3


def _marker(db_path: Path | str) -> Path:
    return Path(db_path).parent / _MARKER


def _verify(path: Path) -> None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise DatabaseError(f"The backup {path.name} cannot be read: {exc}") from exc
    if result != "ok":
        raise DatabaseError(f"The backup {path.name} is damaged ({result}).")


def request_restore(db_path: Path | str, name: str) -> None:
    """Ask for *name* to be restored at the next launch.

    :param db_path: Path of the live database.
    :param name: A backup file name from :func:`list_backups`.
    :raises NotFoundError: If there is no such backup (also rejects any path).
    :raises DatabaseError: If the backup is damaged.
    """
    match = next((b for b in list_backups(db_path) if b.name == name), None)
    if match is None:
        raise NotFoundError("Backup", name)
    _verify(match.path)
    marker = _marker(db_path)
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": match.name}), encoding="utf-8")
    os.replace(tmp, marker)
    logger.info("Restore of %s requested; it will be applied at the next launch.", name)


def cancel_restore(db_path: Path | str) -> None:
    """Withdraw a pending restore request, if any."""
    _marker(db_path).unlink(missing_ok=True)


def pending_restore(db_path: Path | str) -> str | None:
    """Return the backup name waiting to be restored, or ``None``."""
    try:
        name = json.loads(_marker(db_path).read_text(encoding="utf-8"))["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return name if isinstance(name, str) else None


def restore_backup(db_path: Path | str, name: str) -> Path:
    """Replace the live database with backup *name* now. The app must not be using it.

    :returns: Where the previous database was kept.
    :raises NotFoundError: If there is no such backup.
    :raises DatabaseError: If the backup is damaged or the swap fails; the live
        database is left as it was.
    """
    live = Path(db_path)
    match = next((b for b in list_backups(live) if b.name == name), None)
    if match is None:
        raise NotFoundError("Backup", name)
    _verify(match.path)

    stage = live.with_name(live.name + ".restoring")
    aside = (
        backups_dir(live) / f"before-restore-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.db"
    )
    try:
        shutil.copy2(match.path, stage)
        with stage.open("rb") as fh:
            os.fsync(fh.fileno())
        _verify(stage)
        if live.exists():
            # Fold any pending WAL into the file so the copy kept aside is complete.
            with contextlib.suppress(sqlite3.Error):
                conn = sqlite3.connect(str(live))
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
            aside.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, aside)
        # A stale WAL must never be applied to a different database file.
        for suffix in ("-wal", "-shm"):
            Path(f"{live}{suffix}").unlink(missing_ok=True)
        os.replace(stage, live)
    except (OSError, sqlite3.Error) as exc:
        with contextlib.suppress(OSError):
            stage.unlink()
        raise DatabaseError(
            f"The restore failed and your current data was not changed: {exc}"
        ) from exc
    for old in sorted(backups_dir(live).glob("before-restore-*.db"))[:-_KEEP_ASIDE]:
        with contextlib.suppress(OSError):
            old.unlink()
    logger.info("Restored %s; the previous database was kept as %s", name, aside)
    return aside


def apply_pending_restore(db_path: Path | str) -> bool:
    """Apply a requested restore, if any. Never raises: a failed restore is logged.

    :returns: ``True`` if a restore was applied.
    """
    name = pending_restore(db_path)
    if name is None:
        return False
    try:
        restore_backup(db_path, name)
    except Exception:
        logger.exception("Pending restore of %s could not be applied; keeping current data.", name)
        return False
    finally:
        cancel_restore(db_path)
    return True


def restore_latest(db_path: Path | str) -> str:
    """Restore the newest backup now (used when the app cannot start).

    :returns: The backup name restored.
    :raises NotFoundError: If there are no backups.
    """
    backups = list_backups(db_path)
    if not backups:
        raise NotFoundError("Backup", "latest")
    restore_backup(db_path, backups[0].name)
    return backups[0].name
