"""User-facing backups: consistent, verified, rolling, and never fatal."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container
from portfolio_manager.infrastructure.db import backup
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations import run_migrations

H = {"X-API-Key": "t"}


def _db(tmp_path: Path, projects: int = 3) -> DatabaseConnection:
    db = DatabaseConnection(tmp_path / "portfolio.db")
    run_migrations(db)
    for i in range(projects):
        db.execute("INSERT INTO project (name, slug) VALUES (?, ?)", (f"p{i}", f"s{i}"))
    db.conn.commit()
    return db


def test_backup_contains_data_still_in_the_wal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert Path(f"{db.path}-wal").stat().st_size > 0, "precondition: data lives in the WAL"

    info = backup.create_backup(db)

    conn = sqlite3.connect(info.path)
    assert conn.execute("SELECT count(*) FROM project").fetchone()[0] == 3
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert info.path.parent == tmp_path / "backups"


def test_only_the_newest_seven_are_kept_and_none_overwritten(tmp_path: Path) -> None:
    db = _db(tmp_path)
    made = [backup.create_backup(db).name for _ in range(10)]
    assert len(set(made)) == 10
    kept = [b.name for b in backup.list_backups(db.path)]
    assert kept == sorted(made, reverse=True)[:7]


def test_automatic_backup_is_daily(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert backup.backup_if_due(db) is not None
    assert backup.backup_if_due(db) is None, "a second one within a day is not due"
    assert backup.backup_if_due(db, max_age=timedelta(0)) is not None
    assert len(backup.list_backups(db.path)) == 2


def test_no_automatic_backup_of_an_empty_database(tmp_path: Path) -> None:
    db = _db(tmp_path, projects=0)
    assert backup.backup_if_due(db) is None
    assert not (tmp_path / "backups").exists()


def test_failure_is_reported_and_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)

    def boom(*_: object, **__: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup.sqlite3, "connect", boom)
    with pytest.raises(Exception, match="Could not write the backup"):
        backup.create_backup(db)
    folder = tmp_path / "backups"
    assert not folder.exists() or not list(folder.glob("*")), "no partial backup may remain"
    assert backup.backup_if_due(db, max_age=timedelta(0)) is None  # automatic: never raises


def test_startup_takes_a_backup_and_the_api_lists_and_creates_them(tmp_path: Path) -> None:
    c = build_container(
        config_path=tmp_path / "c.toml", db_path=tmp_path / "p.db", token="t", production=True
    )
    client = TestClient(create_app(c))
    client.post("/api/v1/projects", headers=H, json={"name": "Mine"})

    r = client.post("/api/v1/backups", headers=H)
    assert r.status_code == 201
    listing = client.get("/api/v1/backups", headers=H).json()
    assert listing["folder"] == str(tmp_path / "backups")
    assert [b["name"] for b in listing["backups"]] == [r.json()["name"]]
    restored = sqlite3.connect(tmp_path / "backups" / r.json()["name"])
    assert restored.execute("SELECT name FROM project").fetchall() == [("Mine",)]
    restored.close()
    assert client.get("/api/v1/backups").status_code == 401, "backups need the token"
    c.close()


def test_a_restored_backup_opens_and_runs(tmp_path: Path) -> None:
    db = _db(tmp_path)
    info = backup.create_backup(db)
    db.execute("DELETE FROM project")
    db.conn.commit()
    db.close()

    live = tmp_path / "portfolio.db"
    for suffix in ("-wal", "-shm"):
        Path(f"{live}{suffix}").unlink(missing_ok=True)
    live.write_bytes(info.path.read_bytes())

    restored = DatabaseConnection(live)
    run_migrations(restored)
    assert restored.fetchone("SELECT count(*) AS c FROM project")["c"] == 3
    assert info.created_at <= datetime.now(UTC)
