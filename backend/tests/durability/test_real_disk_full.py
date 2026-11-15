"""A real full disk: genuine ENOSPC from the OS, not an injected error."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.durability.conftest import fill_disk
from tests.integration.test_migration_safety import _fingerprint, _legacy_v2_db, _versions

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container
from portfolio_manager.domain.errors import MigrationError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations import run_migrations

pytestmark = pytest.mark.durability


def _big_legacy_db(path: Path, sessions: int = 1500) -> None:
    _legacy_v2_db(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO session (project_id, scheduled_date, week_key, duration_minutes, status,"
        " focus, notes) VALUES (1, '2026-04-07', '2026.15', 90, 'completed', ?, ?)",
        [(f"focus {i} " + "f" * 120, "n" * 120) for i in range(sessions)],
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def _fp(path: Path) -> dict:
    """Fingerprint that tolerates the v3 table swap by comparing data row counts only."""
    return _fingerprint(path)


@pytest.mark.parametrize("leave", ["none", "tiny", "snapshot_only", "double", "triple"])
def test_upgrade_on_a_full_disk_never_damages_the_database(small_volume: Path, leave: str) -> None:
    """At every fill level the upgrade either completes or changes nothing."""
    path = small_volume / "portfolio.db"
    _big_legacy_db(path)
    size = path.stat().st_size
    before = _fp(path)
    free = {
        "none": 0,
        "tiny": 16 * 1024,
        "snapshot_only": size + 100 * 1024,
        "double": size * 2 + 100 * 1024,
        "triple": size * 3 + 100 * 1024,
    }[leave]
    filler = fill_disk(small_volume, leave=free)

    try:
        db = DatabaseConnection(path)
    except sqlite3.OperationalError as exc:
        # On a 100% full disk even opening a WAL database fails: SQLite must create
        # the -wal and -shm files. The file itself must be left exactly as it was.
        assert any(m in str(exc) for m in ("unable to open", "disk I/O error", "disk is full"))
        outcome = "cannot open"
        assert leave in ("none", "tiny")
        filler.unlink()
        db = None
    else:
        try:
            run_migrations(db)
            outcome = "completed"
        except MigrationError as exc:
            outcome = "refused"
            assert "unchanged" in str(exc) or "not changed" in str(exc)
        except sqlite3.OperationalError:
            pytest.fail("a raw sqlite error escaped the migration runner")
        db.close()
        filler.unlink()

    check = sqlite3.connect(path)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
    leftovers = list(small_volume.glob("*.partial"))
    assert not leftovers, f"partial snapshot left behind: {leftovers}"
    if outcome in ("refused", "cannot open"):
        assert _fp(path) == before, "a refused upgrade must leave the database untouched"

    # With space available again, the next launch finishes the job with all data intact.
    db = DatabaseConnection(path)
    run_migrations(db)
    assert {"v3", "v4"} <= set(_versions(db))
    assert db.fetchone("SELECT count(*) AS c FROM session")["c"] == 1501
    assert db.fetchone("PRAGMA integrity_check")[0] == "ok"
    db.close()
    print(f"\n[full-disk upgrade leave={leave} ({free} B free)] first attempt: {outcome}")


def test_writes_on_a_full_disk_fail_cleanly_and_recover(small_volume: Path) -> None:
    c = build_container(
        config_path=small_volume / "config.toml",
        db_path=small_volume / "portfolio.db",
        token="t",
        production=True,
    )
    client = TestClient(create_app(c), raise_server_exceptions=False)
    h = {"X-API-Key": "t"}
    assert client.post("/api/v1/projects", headers=h, json={"name": "Before"}).status_code == 201

    filler = fill_disk(small_volume)
    big = "x" * 900_000  # just under the 1 MiB request limit
    failures = 0
    big_names_created: set[str] = set()
    for i in range(3):
        r = client.post(
            "/api/v1/projects", headers=h, json={"name": f"Big{i}", "plan_content": big}
        )
        if r.status_code >= 400:
            failures += 1
        else:
            big_names_created.add(f"Big{i}")
        print(f"\n[full-disk write {i}] HTTP {r.status_code} {r.json().get('code')}")
    assert failures >= 1, "writes on a full disk must fail, not silently succeed"

    # Reads still work, nothing is stuck, and the file is not corrupt.
    names = [p["name"] for p in client.get("/api/v1/projects", headers=h).json()["projects"]]
    assert "Before" in names
    # A write that failed must not leave a half-written project behind.
    assert {n for n in names if n.startswith("Big")} == big_names_created
    assert not c.db.conn.in_transaction
    assert c.db.fetchone("PRAGMA integrity_check")[0] == "ok"

    filler.unlink()
    r = client.post("/api/v1/projects", headers=h, json={"name": "After"})
    assert r.status_code == 201, "the app must recover once space is available"
    names = [p["name"] for p in client.get("/api/v1/projects", headers=h).json()["projects"]]
    assert {"Before", "After"} <= set(names)
    assert {n for n in names if n.startswith("Big")} == big_names_created
    c.close()


@pytest.mark.parametrize("leave", [0, 16 * 1024])
def test_sidecar_start_on_a_full_disk_exits_with_the_storage_status(
    small_volume: Path, leave: int
) -> None:
    """The app can only show a useful message if the sidecar says why it stopped."""
    import os
    import subprocess
    import sys

    from tests.durability.conftest import SRC

    from portfolio_manager.cli import sidecar

    home = small_volume / "home"
    home.mkdir()
    # Pre-existing database that needs upgrading, so startup must write to the full disk.
    db_dir = home / ".portfolio_manager"
    db_dir.mkdir()
    _big_legacy_db(db_dir / "portfolio.db")
    filler = fill_disk(small_volume, leave=leave)

    env = {**os.environ, "HOME": str(home), "PYTHONPATH": SRC, "PORTFOLIO_SIDECAR_TOKEN": "t"}
    proc = subprocess.run(
        [sys.executable, "-m", "portfolio_manager.cli.sidecar", "--port", "58123", "--production"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    filler.unlink()
    print(f"\n[full-disk start, {leave} B free] exit={proc.returncode}\n{proc.stderr[-400:]}")
    assert proc.returncode == sidecar.EXIT_STORAGE, proc.stderr[-800:]
    assert "disk" in proc.stderr.lower()
