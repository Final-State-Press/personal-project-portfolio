"""Integration tests for the migration runner and backup behavior."""

from pathlib import Path

from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations import run_migrations


def _applied(db: DatabaseConnection) -> set[str]:
    return {r["version"] for r in db.fetchall("SELECT version FROM schema_migration")}


def test_fresh_database_applies_all_migrations(tmp_path: Path) -> None:
    db = DatabaseConnection(tmp_path / "portfolio.db")
    run_migrations(db)
    assert {"v1", "v2", "v3", "v4"} <= _applied(db)
    db.close()


def test_fresh_database_takes_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    run_migrations(db)
    db.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_rerun_with_nothing_pending_takes_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    run_migrations(db)
    db.close()
    db2 = DatabaseConnection(path)
    run_migrations(db2)
    db2.close()
    assert list(tmp_path.glob("*.bak")) == []


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = DatabaseConnection(tmp_path / "portfolio.db")
    run_migrations(db)
    before = _applied(db)
    run_migrations(db)
    assert _applied(db) == before
    db.close()


def test_v3_schema_has_new_columns(tmp_path: Path) -> None:
    db = DatabaseConnection(tmp_path / "portfolio.db")
    run_migrations(db)
    cols = {r["name"] for r in db.fetchall("PRAGMA table_info(session)")}
    assert "milestone_id" in cols
    project_cols = {r["name"] for r in db.fetchall("PRAGMA table_info(project)")}
    assert "end_date" in project_cols
    ms_cols = {r["name"] for r in db.fetchall("PRAGMA table_info(milestone)")}
    assert {"target_date", "notes", "status"} <= ms_cols
    db.close()
