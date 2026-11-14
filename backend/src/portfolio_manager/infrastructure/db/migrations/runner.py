"""Migration runner: atomic migrations and consistent, never-overwritten backups.

Safety properties:

* **Each migration is one transaction.** The schema changes and the
  ``schema_migration`` row commit together or not at all (SQLite DDL is
  transactional). An interruption at any point, a crash, ``SIGKILL``, a full
  disk or a failing statement, leaves the database exactly at the previous
  version, so the next launch simply retries.
* **A consistent snapshot is taken before the first pending migration**, using
  SQLite's online backup API. A plain file copy is not safe for a WAL database:
  committed data can still be in the ``-wal`` file, so the copy can be empty or
  stale. The snapshot is verified with ``PRAGMA integrity_check`` before any
  migration runs.
* **Backups are never overwritten.** Each snapshot has a unique name that
  records the versions and time. Only the newest :data:`KEEP_BACKUPS` are kept.
* **If a snapshot cannot be taken, the migration does not run.** Better to
  refuse to start than to migrate data that cannot be recovered.
* **Foreign keys are off during a migration** (required for table rebuilds such
  as v3) and checked with ``PRAGMA foreign_key_check`` before commit.
* **Newer data is refused.** A database with a migration version this build does
  not know is left untouched and the app refuses to start.
* **Healing.** An ``ALTER TABLE ... ADD COLUMN`` for a column that already
  exists is skipped, which repairs databases left half-migrated by older builds.
"""

import contextlib
import logging
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from portfolio_manager.domain.errors import MigrationError, NewerDatabaseError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations.versions import build_migrations

logger = logging.getLogger(__name__)

#: Number of pre-migration snapshots to keep.
KEEP_BACKUPS = 5

_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>\w+)\s+ADD\s+COLUMN\s+(?P<column>\w+)\b",
    re.IGNORECASE,
)
_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.IGNORECASE)


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into complete statements.

    Uses :func:`sqlite3.complete_statement`, so semicolons inside ``CREATE
    TRIGGER ... BEGIN ... END`` bodies do not split a statement.

    :param sql: SQL script text.
    :returns: The statements, in order, with comment-only fragments dropped.
    :rtype: list[str]
    """
    statements: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if _has_code(buf):
                statements.append(buf.strip())
            buf = ""
    if _has_code(buf):
        statements.append(buf.strip())
    return statements


def _has_code(fragment: str) -> bool:
    """Return ``True`` if *fragment* has anything other than comments/whitespace."""
    for line in fragment.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


def _has_user_tables(db: DatabaseConnection) -> bool:
    row = db.fetchone(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    )
    return row is not None


def _applied_versions(db: DatabaseConnection) -> set[str]:
    """Return the set of migration versions already applied.

    A missing ``schema_migration`` table means a brand-new database. Any other
    failure (corruption, I/O error, lock) is raised and never mistaken for "no
    migrations applied".

    :rtype: set[str]
    :raises MigrationError: If the migration state cannot be read.
    """
    try:
        rows = db.fetchall("SELECT version FROM schema_migration")
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return set()
        raise MigrationError(f"Could not read the database migration state: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"The database could not be read: {exc}") from exc
    return {row["version"] for row in rows}


def _backup_name(path: Path, from_version: str, to_version: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{path.stem}.{from_version}-to-{to_version}.{stamp}.bak"


def _prune_backups(path: Path) -> None:
    """Keep only the newest :data:`KEEP_BACKUPS` snapshots for *path*."""
    # Names end in ``.<UTC timestamp>.bak``; sort on the timestamp, not the versions.
    snapshots = sorted(
        path.parent.glob(f"{path.stem}.*-to-*.bak"), key=lambda p: p.name.rsplit(".", 2)[-2]
    )
    for old in snapshots[:-KEEP_BACKUPS]:
        with contextlib.suppress(OSError):
            old.unlink()
    # A crash while writing a snapshot can leave a partial file behind.
    for partial in path.parent.glob(f"{path.stem}.*.bak.partial"):
        with contextlib.suppress(OSError):
            partial.unlink()


def create_snapshot(db: DatabaseConnection, from_version: str, to_version: str) -> Path | None:
    """Write a verified, consistent snapshot of the database.

    :param db: Active connection.
    :param from_version: Highest applied version (``"none"`` if empty).
    :param to_version: Version being migrated to.
    :returns: Path of the snapshot, or ``None`` for an in-memory database.
    :raises MigrationError: If the snapshot cannot be written or fails verification.
    """
    if str(db.path) == ":memory:":
        return None
    path = Path(db.path)
    dest = path.with_name(_backup_name(path, from_version, to_version))
    tmp = dest.with_suffix(".bak.partial")
    try:
        target = sqlite3.connect(str(tmp))
        try:
            db.conn.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
        if result != "ok":
            raise MigrationError(f"The pre-migration snapshot failed verification: {result}")
        tmp.replace(dest)
    except MigrationError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    except (OSError, sqlite3.Error) as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise MigrationError(
            f"Could not back up the database before upgrading it, so it was not changed: {exc}"
        ) from exc
    logger.info("Database snapshot written to %s", dest)
    _prune_backups(path)
    return dest


def _column_exists(db: DatabaseConnection, table: str, column: str) -> bool:
    cols = db.fetchall(f"PRAGMA table_info({table})")  # noqa: S608 - identifier from migration SQL
    return any(c["name"].lower() == column.lower() for c in cols)


def _apply_migration(db: DatabaseConnection, version: str, description: str, sql: str) -> None:
    """Apply one migration atomically, with its version row in the same transaction."""
    conn = db.conn
    if conn.in_transaction:
        conn.rollback()
    # Required by the SQLite table-rebuild procedure; a no-op inside a transaction,
    # so it must be set first. Restored in ``finally``.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in split_statements(sql):
                if _PRAGMA.match(stmt):
                    continue  # connection settings are owned by the runner
                add = _ADD_COLUMN.match(stmt)
                if add and _column_exists(db, add["table"], add["column"]):
                    logger.warning(
                        "Migration %s: column %s.%s already exists; skipping.",
                        version,
                        add["table"],
                        add["column"],
                    )
                    continue
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migration (version, description) VALUES (?, ?)",
                (version, description),
            )
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(
                    f"Migration {version!r} would leave {len(violations)} broken "
                    "relationship(s); it was rolled back."
                )
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def run_migrations(db: DatabaseConnection) -> None:
    """Apply any pending migrations to the database.

    :param db: Active database connection.
    :raises NewerDatabaseError: If the database has a migration this build does not know.
    :raises MigrationError: If the state cannot be read, the snapshot cannot be
        written, or a migration fails. In every case the database is left at its
        last fully applied version.
    """
    migrations = build_migrations()
    applied = _applied_versions(db)
    unknown = sorted(applied - {v for v, _, _ in migrations})
    if unknown:
        # Never open, migrate or write to data this build does not understand.
        raise NewerDatabaseError(
            "This data was created by a newer version of Portfolio Manager "
            f"(schema {', '.join(unknown)}). Install the latest version; "
            "your data has not been changed."
        )
    pending = [(v, d, s) for v, d, s in migrations if v not in applied]

    if not pending:
        logger.debug("No pending migrations.")
        return

    target = pending[-1][0]
    logger.info("%d migration(s) to apply: %s", len(pending), [v for v, _, _ in pending])

    snapshot: Path | None = None
    if _has_user_tables(db):  # nothing worth protecting in a brand-new database
        known = [v for v, _, _ in migrations if v in applied]
        snapshot = create_snapshot(db, known[-1] if known else "none", target)

    for version, description, sql in pending:
        logger.info("Applying migration %s: %s", version, description)
        try:
            _apply_migration(db, version, description, sql)
        except Exception as exc:
            raise MigrationError(_failure_message(version, exc, snapshot)) from exc
        logger.info("Migration %s applied.", version)


def _failure_message(version: str, exc: BaseException, snapshot: Path | None) -> str:
    where = f" A backup from before the upgrade is at {snapshot}." if snapshot else ""
    return (
        f"Migration {version!r} failed and was rolled back; your data is unchanged: {exc}.{where}"
    )
