"""A damaged database is detected at launch, named as such, and left untouched."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from portfolio_manager.bootstrap import build_container
from portfolio_manager.domain.errors import DatabaseCorruptError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.integrity import check_integrity
from portfolio_manager.infrastructure.db.migrations import run_migrations


def _good_db(path: Path) -> None:
    db = DatabaseConnection(path)
    run_migrations(db)
    db.conn.executemany(
        "INSERT INTO project (name, slug) VALUES (?, ?)", [(f"n{i}", f"s{i}") for i in range(400)]
    )
    db.conn.commit()
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _damage(path: Path, offset: int, length: int = 200) -> None:
    raw = bytearray(path.read_bytes())
    raw[offset : offset + length] = b"\xff" * length
    path.write_bytes(bytes(raw))


def test_healthy_database_passes(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    _good_db(path)
    db = DatabaseConnection(path)
    check_integrity(db)
    db.close()


@pytest.mark.parametrize("where", ["header", "btree_page"])
def test_damaged_database_is_reported_and_not_modified(tmp_path: Path, where: str) -> None:
    path = tmp_path / "p.db"
    _good_db(path)
    _damage(path, 0 if where == "header" else 4096 * 2, 100 if where == "header" else 200)
    before = _digest(path)

    with pytest.raises(DatabaseCorruptError, match="damaged"):
        build_container(config_path=tmp_path / "c.toml", db_path=path, token="t")

    assert _digest(path) == before, "a damaged database must not be changed"
    assert not list(tmp_path.glob("*.bak*")), "a damaged database must not be snapshotted"


def test_other_sqlite_errors_are_not_called_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "p.db"
    _good_db(path)
    db = DatabaseConnection(path)

    class Failing:
        def execute(self, *a: object) -> None:
            raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(db, "_conn", Failing())
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        check_integrity(db)
