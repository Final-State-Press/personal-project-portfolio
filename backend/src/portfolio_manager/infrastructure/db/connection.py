"""SQLite connection wrapper.

Unlike the original singleton, the sidecar creates one
:class:`DatabaseConnection` per application instance (owned by
:mod:`portfolio_manager.bootstrap`) and injects it into repositories. This keeps
tests isolated and avoids hidden global state, while preserving the same
per-connection settings (foreign keys ON, WAL) and transaction semantics.
"""

import contextlib
import logging
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from portfolio_manager.domain.errors import DatabaseCorruptError, DatabaseLockedError

logger = logging.getLogger(__name__)

#: SQLite messages that mean the file itself is damaged (not full, locked or unwritable).
CORRUPTION_MARKERS = ("malformed", "not a database", "corrupt")


def is_corruption(exc: BaseException) -> bool:
    """Return ``True`` if *exc* says the database file is damaged."""
    return isinstance(exc, sqlite3.DatabaseError) and any(
        m in str(exc).lower() for m in CORRUPTION_MARKERS
    )


class DatabaseConnection:
    """Thin wrapper around a :mod:`sqlite3` connection.

    :param db_path: Filesystem path to the SQLite database file, or
        ``":memory:"`` for an in-memory database (used in tests).
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = db_path
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        logger.info("Opened database at %s", db_path)

    @property
    def path(self) -> Path | str:
        """The resolved database path (or ``":memory:"``).

        :rtype: pathlib.Path | str
        """
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        """The underlying :class:`sqlite3.Connection`.

        :rtype: sqlite3.Connection
        """
        return self._conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError as exc:
            conn.close()
            if is_corruption(exc):  # e.g. a damaged header: "file is not a database"
                raise DatabaseCorruptError(
                    f"The database file {self._path} is damaged ({exc}). Nothing was "
                    "changed. If you have a backup, restore it; backups made before "
                    "upgrades are the .bak files next to the database."
                ) from exc
            raise
        # Durability settings, stated explicitly rather than left to compile-time
        # defaults. ``synchronous=FULL`` syncs the WAL on every commit. On macOS a
        # plain ``fsync`` does not flush the drive's write cache, so a power cut can
        # still lose or corrupt "committed" data; ``fullfsync`` issues F_FULLFSYNC.
        # It measured about 4 ms per commit, which is irrelevant for this app.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA fullfsync = ON")
        conn.execute("PRAGMA checkpoint_fullfsync = ON")
        return conn

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        with contextlib.suppress(Exception):  # closing must never raise
            self._conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Wrap statements in a single transaction.

        Commits on success; rolls back and re-raises on any exception.

        :raises DatabaseLockedError: If SQLite reports a locked database.
        """
        try:
            self._conn.execute("BEGIN")
            yield self._conn
            self._conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self._rollback_quietly()
            if "locked" in str(exc).lower():
                raise DatabaseLockedError("The database is locked by another process.") from exc
            raise
        except Exception:
            self._rollback_quietly()
            raise

    def _rollback_quietly(self) -> None:
        """Roll back if a transaction is still open, never masking the real error.

        SQLite rolls a transaction back by itself on some failures (for example a
        full disk), after which an explicit ``ROLLBACK`` raises "no transaction is
        active" and would replace the original error with that confusing one.
        """
        if self._conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Execute a single SQL statement.

        :param sql: SQL statement.
        :param params: Bind parameters.
        :rtype: sqlite3.Cursor
        :raises DatabaseLockedError: If the database is locked.
        """
        try:
            return self._conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                raise DatabaseLockedError(str(exc)) from exc
            raise

    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script (commits implicitly).

        :param sql: SQL script text.
        """
        self._conn.executescript(sql)

    def fetchall(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        """Execute *sql* and return all rows.

        :rtype: list[sqlite3.Row]
        """
        return self.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        """Execute *sql* and return the first row, or ``None``.

        :rtype: sqlite3.Row | None
        """
        return self.execute(sql, params).fetchone()
