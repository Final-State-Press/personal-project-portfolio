"""Restoring a backup: requested, applied before the database opens, crash-safe."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container
from portfolio_manager.domain.errors import DatabaseError, NotFoundError
from portfolio_manager.infrastructure.db import backup, restore
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations import run_migrations

H = {"X-API-Key": "t"}


def _names(path: Path) -> list[str]:
    c = sqlite3.connect(path)
    try:
        return [r[0] for r in c.execute("SELECT name FROM project ORDER BY name")]
    finally:
        c.close()


def _app(tmp_path: Path):  # noqa: ANN202
    c = build_container(
        config_path=tmp_path / "c.toml", db_path=tmp_path / "p.db", token="t", production=True
    )
    return c, TestClient(create_app(c))


def test_request_then_restart_restores_and_keeps_the_old_data_aside(tmp_path: Path) -> None:
    c, client = _app(tmp_path)
    client.post("/api/v1/projects", headers=H, json={"name": "Before"})
    name = client.post("/api/v1/backups", headers=H).json()["name"]
    client.post("/api/v1/projects", headers=H, json={"name": "After"})

    r = client.post("/api/v1/backups/restore", headers=H, json={"name": name})
    assert r.status_code == 200 and r.json()["restart_required"] is True
    assert client.get("/api/v1/backups", headers=H).json()["pending_restore"] == name
    assert _names(tmp_path / "p.db") == ["After", "Before"], "nothing changes until restart"
    c.close()

    c2, _ = _app(tmp_path)  # the next launch
    try:
        assert _names(tmp_path / "p.db") == ["Before"]
        assert restore.pending_restore(tmp_path / "p.db") is None
        aside = list((tmp_path / "backups").glob("before-restore-*.db"))
        assert len(aside) == 1 and _names(aside[0]) == ["After", "Before"]
        assert c2.db.fetchone("PRAGMA integrity_check")[0] == "ok"
    finally:
        c2.close()


def test_unknown_or_path_like_names_are_rejected(tmp_path: Path) -> None:
    c, client = _app(tmp_path)
    for bad in ("nope.db", "../../etc/passwd", "/etc/passwd"):
        r = client.post("/api/v1/backups/restore", headers=H, json={"name": bad})
        assert r.status_code == 404, bad
    assert restore.pending_restore(tmp_path / "p.db") is None
    c.close()


def test_a_damaged_backup_is_refused_and_nothing_changes(tmp_path: Path) -> None:
    c, client = _app(tmp_path)
    client.post("/api/v1/projects", headers=H, json={"name": "Keep"})
    name = client.post("/api/v1/backups", headers=H).json()["name"]
    c.close()
    path = tmp_path / "backups" / name
    raw = bytearray(path.read_bytes())
    raw[:100] = b"\xff" * 100
    path.write_bytes(bytes(raw))

    with pytest.raises(DatabaseError):
        restore.request_restore(tmp_path / "p.db", name)
    with pytest.raises(DatabaseError):
        restore.restore_backup(tmp_path / "p.db", name)
    assert _names(tmp_path / "p.db") == ["Keep"]
    assert not list(tmp_path.glob("*.restoring"))


def test_restore_replaces_a_damaged_database_even_with_a_stale_wal(tmp_path: Path) -> None:
    db = DatabaseConnection(tmp_path / "p.db")
    run_migrations(db)
    db.execute("INSERT INTO project (name, slug) VALUES ('Good', 'g')")
    db.conn.commit()
    info = backup.create_backup(db)
    db.execute("INSERT INTO project (name, slug) VALUES ('Later', 'l')")
    db.conn.commit()
    # Simulate a crash: leave the WAL behind and damage the main file.
    wal = Path(f"{tmp_path / 'p.db'}-wal")
    stale = wal.read_bytes()
    db.close()
    raw = bytearray((tmp_path / "p.db").read_bytes())
    raw[:200] = b"\xff" * 200
    (tmp_path / "p.db").write_bytes(bytes(raw))
    wal.write_bytes(stale)

    restore.restore_backup(tmp_path / "p.db", info.name)

    assert _names(tmp_path / "p.db") == ["Good"]
    assert not wal.exists(), "a stale WAL must not be applied to the restored file"
    d2 = DatabaseConnection(tmp_path / "p.db")
    assert d2.fetchone("PRAGMA integrity_check")[0] == "ok"
    d2.close()


def test_failed_swap_leaves_the_current_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, client = _app(tmp_path)
    client.post("/api/v1/projects", headers=H, json={"name": "Current"})
    name = client.post("/api/v1/backups", headers=H).json()["name"]
    c.close()

    def boom(*_: object) -> None:
        raise OSError("simulated crash at the swap")

    monkeypatch.setattr(restore.os, "replace", boom)
    with pytest.raises(DatabaseError, match="not changed"):
        restore.restore_backup(tmp_path / "p.db", name)
    monkeypatch.undo()
    assert _names(tmp_path / "p.db") == ["Current"]
    assert not list(tmp_path.glob("*.restoring"))


def test_a_pending_restore_that_cannot_be_applied_never_blocks_startup(tmp_path: Path) -> None:
    c, _ = _app(tmp_path)
    c.close()
    (tmp_path / "restore-pending.json").write_text('{"name": "gone.db"}')
    c2, _ = _app(tmp_path)  # must not raise
    c2.close()
    assert not (tmp_path / "restore-pending.json").exists(), "a bad request must not repeat"


def test_restore_latest_cli_path_and_no_backups(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        restore.restore_latest(tmp_path / "p.db")
    c, client = _app(tmp_path)
    client.post("/api/v1/projects", headers=H, json={"name": "Saved"})
    backup.create_backup(c.db)
    client.post("/api/v1/projects", headers=H, json={"name": "Lost"})
    c.close()
    assert restore.restore_latest(tmp_path / "p.db").startswith("portfolio-")
    assert _names(tmp_path / "p.db") == ["Saved"]


def test_export_writes_a_verified_copy_into_the_chosen_folder(tmp_path: Path) -> None:
    c, client = _app(tmp_path)
    client.post("/api/v1/projects", headers=H, json={"name": "Mine"})
    dest = tmp_path / "usb"
    dest.mkdir()

    r = client.post("/api/v1/backups/export", headers=H, json={"destination": str(dest)})

    assert r.status_code == 200
    exported = list(dest.glob("portfolio-export-*.db"))
    assert len(exported) == 1 and _names(exported[0]) == ["Mine"]
    assert r.json()["path"] == str(exported[0])
    assert not list(dest.glob("*.partial"))
    for bad in ("relative/dir", str(tmp_path / "missing")):
        r = client.post("/api/v1/backups/export", headers=H, json={"destination": bad})
        assert (
            r.status_code >= 400
            and "existing folder" in r.json()["message"]
            or r.status_code == 500
        )
    c.close()
