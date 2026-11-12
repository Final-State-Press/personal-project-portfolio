"""The connection must state its durability settings explicitly."""

from pathlib import Path

from portfolio_manager.infrastructure.db.connection import DatabaseConnection


def test_connection_uses_full_durability_settings(tmp_path: Path) -> None:
    db = DatabaseConnection(tmp_path / "portfolio.db")
    pragma = lambda name: db.fetchone(f"PRAGMA {name}")[0]  # noqa: E731
    assert pragma("journal_mode") == "wal"
    assert pragma("synchronous") == 2  # FULL
    assert pragma("fullfsync") == 1
    assert pragma("checkpoint_fullfsync") == 1
    assert pragma("foreign_keys") == 1
    assert pragma("busy_timeout") >= 5000
    db.close()


def test_transaction_does_not_mask_the_original_error_after_an_automatic_rollback(
    tmp_path: Path,
) -> None:
    """SQLite rolls back by itself on a full disk; our ROLLBACK must not replace that error."""
    import sqlite3

    import pytest

    db = DatabaseConnection(tmp_path / "portfolio.db")
    with (
        pytest.raises(sqlite3.OperationalError, match="database or disk is full"),
        db.transaction(),
    ):
        db.conn.execute("ROLLBACK")  # what SQLite does internally on SQLITE_FULL
        raise sqlite3.OperationalError("database or disk is full")
    assert not db.conn.in_transaction
    db.close()
