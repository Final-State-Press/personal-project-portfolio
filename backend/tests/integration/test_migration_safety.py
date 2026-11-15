"""Migration safety: atomicity, crash recovery and backup integrity.

These tests pin down the failure modes found in the durability review: a
half-applied migration that could not be retried, a backup overwritten with the
damaged database, and a backup that was an empty copy of a WAL database.
"""

import hashlib
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from portfolio_manager.domain.errors import MigrationError, NewerDatabaseError
from portfolio_manager.infrastructure.db.connection import DatabaseConnection
from portfolio_manager.infrastructure.db.migrations import run_migrations, runner
from portfolio_manager.infrastructure.db.migrations.versions import build_migrations

SRC = str(Path(__file__).resolve().parents[2] / "src")
_V1, _V2 = build_migrations()[0][2], build_migrations()[1][2]


def _legacy_v2_db(path: Path, *, extra: str = "") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_V1)
    conn.executescript(_V2)
    conn.execute("INSERT INTO schema_migration (version, description) VALUES ('v1','x'),('v2','y')")
    conn.execute("INSERT INTO project (name, slug) VALUES ('P', 'p')")
    conn.execute(
        "INSERT INTO session (project_id, scheduled_date, week_key, duration_minutes, status,"
        " focus, notes) VALUES (1, '2026-04-07', '2026.15', 90, 'completed', 'f', 'n')"
    )
    conn.execute("INSERT INTO milestone (project_id, description, is_complete) VALUES (1, 'm', 1)")
    if extra:
        conn.executescript(extra)
    conn.commit()
    conn.close()


def _fingerprint(path: Path) -> dict:
    """Schema, versions and row data: everything that must be unchanged by a rollback."""
    conn = sqlite3.connect(path)
    schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migration ORDER BY id")]
    data = {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()  # noqa: S608
        for t in ("project", "session", "milestone")
    }
    conn.close()
    return {"schema": schema, "versions": versions, "data": data}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _versions(db: DatabaseConnection) -> list[str]:
    return [r["version"] for r in db.fetchall("SELECT version FROM schema_migration ORDER BY id")]


# --------------------------------------------------------------------------- splitting


def test_split_statements_keeps_trigger_bodies_whole() -> None:
    sql = """
    -- a comment
    CREATE TABLE t (id INTEGER);
    CREATE TRIGGER t_ins AFTER INSERT ON t
    BEGIN
        UPDATE t SET id = id + 1 WHERE id = NEW.id;
        UPDATE t SET id = id WHERE id = NEW.id;
    END;
    -- trailing comment only
    """
    stmts = runner.split_statements(sql)
    assert len(stmts) == 2
    assert stmts[1].startswith("CREATE TRIGGER") and stmts[1].rstrip().endswith("END;")


def test_split_statements_reproduces_every_real_migration(tmp_path: Path) -> None:
    for _, _, sql in build_migrations():
        assert runner.split_statements(sql)  # none parse to nothing


# --------------------------------------------------------------------------- atomicity


def _with_migrations(monkeypatch: pytest.MonkeyPatch, extra: list[tuple[str, str, str]]) -> None:
    base = build_migrations()
    monkeypatch.setattr(runner, "build_migrations", lambda: base + extra)


def test_failed_migration_rolls_back_completely_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    run_migrations(db)
    with db.transaction():
        db.execute("INSERT INTO project (name, slug) VALUES ('keep', 'keep')")
    before = _fingerprint(path)

    bad = (
        "v5",
        "adds a column then fails",
        "ALTER TABLE project ADD COLUMN extra TEXT;\n"
        "CREATE TABLE half_done (x INTEGER);\n"
        "INSERT INTO table_that_does_not_exist VALUES (1);\n",
    )
    _with_migrations(monkeypatch, [bad])
    with pytest.raises(MigrationError, match="rolled back"):
        run_migrations(db)

    # Nothing from the failed migration survives: not the ALTER, not the new table.
    assert _fingerprint(path) == before
    assert db.fetchone("PRAGMA foreign_keys")[0] == 1, "foreign keys must be restored"
    assert not db.conn.in_transaction

    # Fix the migration and retry: it applies cleanly on top of the unchanged database.
    good = ("v5", "fixed", "ALTER TABLE project ADD COLUMN extra TEXT;")
    _with_migrations(monkeypatch, [good])
    run_migrations(db)
    assert _versions(db)[-1] == "v5"
    assert db.fetchone("SELECT name FROM project WHERE slug = 'keep'") is not None
    db.close()


def test_migration_that_breaks_a_relationship_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    run_migrations(db)
    before = _fingerprint(path)
    orphan = (
        "v5",
        "inserts a session for a project that does not exist",
        "INSERT INTO session (project_id, scheduled_date, week_key) "
        "VALUES (999, '2026-01-01', '2026.1');",
    )
    _with_migrations(monkeypatch, [orphan])
    with pytest.raises(MigrationError, match="broken relationship"):
        run_migrations(db)
    assert _fingerprint(path) == before
    db.close()


def test_database_left_half_migrated_by_an_older_build_is_healed(tmp_path: Path) -> None:
    # Experiment B / A from the review: the column already exists but the version row
    # was never written. This used to fail forever with "duplicate column name".
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path, extra="ALTER TABLE project ADD COLUMN end_date DATE;")
    db = DatabaseConnection(path)
    run_migrations(db)
    assert {"v3", "v4"} <= set(_versions(db))
    assert db.fetchone("SELECT status FROM session WHERE id = 1")["status"] == "done"
    db.close()


# --------------------------------------------------------------------------- hard crash

_CRASH_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {src!r})
    from portfolio_manager.infrastructure.db.connection import DatabaseConnection
    from portfolio_manager.infrastructure.db.migrations import run_migrations

    class Dying:
        # Proxy that kills the whole process (no cleanup, no ROLLBACK) at the Nth
        # statement executed after the migration transaction begins.
        def __init__(self, real, n):
            self._real, self._n, self._count, self._armed = real, n, 0, False
        def execute(self, sql, *a, **k):
            if self._armed:
                if self._count == self._n:
                    os._exit(137)
                self._count += 1
            if sql.startswith("BEGIN IMMEDIATE"):
                self._armed = True
            return self._real.execute(sql, *a, **k)
        def __getattr__(self, name):
            return getattr(self._real, name)

    db = DatabaseConnection({path!r})
    db._conn = Dying(db._conn, {n})
    run_migrations(db)
    print("COMPLETED")
    """
)


def _statements_in_v3_to_v4() -> int:
    n = 0
    for v, _, sql in build_migrations():
        if v in ("v3", "v4"):
            n += len(
                [s for s in runner.split_statements(sql) if not s.upper().startswith("PRAGMA")]
            )
            n += 3  # version INSERT, foreign_key_check, COMMIT
    return n


def test_process_killed_at_every_point_in_a_migration_loses_nothing(tmp_path: Path) -> None:
    """Kill the process (``os._exit``) after each statement of the real v3/v4 upgrade.

    After every crash the database must be at a clean version, hold all its data,
    pass an integrity check, and finish upgrading on the next launch.
    """
    reference = tmp_path / "ref.db"
    _legacy_v2_db(reference)
    ref_data = {
        "projects": [
            tuple(r) for r in sqlite3.connect(reference).execute("SELECT name FROM project")
        ],
    }

    crashed_early = 0
    for n in range(_statements_in_v3_to_v4() + 1):
        path = tmp_path / f"crash{n}.db"
        _legacy_v2_db(path)
        proc = subprocess.run(
            [sys.executable, "-c", _CRASH_CHILD.format(src=SRC, path=str(path), n=n)],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(tmp_path / "home")},
            timeout=60,
        )
        if proc.returncode == 137:
            crashed_early += 1
            conn = sqlite3.connect(path)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", f"n={n}"
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migration")]
            # A migration is either fully applied or not at all: the schema must match the version.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session)")}
            if "v3" in versions:
                assert "milestone_id" in cols and "focus" not in cols, f"n={n}"
            else:
                assert "focus" in cols and "milestone_id" not in cols, f"n={n}"
            assert [tuple(r) for r in conn.execute("SELECT name FROM project")] == ref_data[
                "projects"
            ]
            conn.close()

        # Next launch finishes the job, whatever happened.
        db = DatabaseConnection(path)
        run_migrations(db)
        assert {"v3", "v4"} <= set(_versions(db)), f"n={n}"
        assert db.fetchone("SELECT status FROM session WHERE id = 1")["status"] == "done"
        assert db.fetchone("SELECT count(*) AS c FROM milestone")["c"] == 1
        db.close()

    assert crashed_early >= 10, "the crash points did not actually interrupt the migration"


# --------------------------------------------------------------------------- backups


def test_snapshot_contains_wal_resident_data(tmp_path: Path) -> None:
    """Finding 2: a plain file copy of a WAL database can be empty; a snapshot is not."""
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    db.conn.execute("CREATE TABLE t (x INTEGER)")
    db.conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    db.conn.commit()
    assert os.path.getsize(f"{path}-wal") > 0, "premise: data is still in the WAL"

    snap = runner.create_snapshot(db, "v1", "v2")
    assert snap is not None
    check = sqlite3.connect(snap)
    assert check.execute("SELECT count(*) FROM t").fetchone()[0] == 500
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    check.close()
    db.close()


def test_upgrade_snapshot_holds_the_pre_upgrade_database(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path)
    db = DatabaseConnection(path)
    run_migrations(db)
    (snap,) = tmp_path.glob("portfolio.v2-to-v4.*.bak")
    check = sqlite3.connect(snap)
    assert {r[0] for r in check.execute("SELECT version FROM schema_migration")} == {"v1", "v2"}
    assert check.execute("SELECT count(*) FROM session").fetchone()[0] == 1
    check.close()
    db.close()


def test_a_failed_upgrade_never_overwrites_an_earlier_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: the old runner replaced the single .bak with the damaged database."""
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path)
    db = DatabaseConnection(path)
    run_migrations(db)
    db.close()
    (first,) = tmp_path.glob("portfolio.v2-to-v4.*.bak")
    first_hash = _sha(first)

    bad = ("v5", "fails", "CREATE TABLE a (x);\nINSERT INTO nope VALUES (1);")
    _with_migrations(monkeypatch, [bad])
    for _ in range(3):  # three failing launches in a row
        db = DatabaseConnection(path)
        with pytest.raises(MigrationError):
            run_migrations(db)
        db.close()

    assert first.exists() and _sha(first) == first_hash, "the original snapshot must be untouched"
    assert len(list(tmp_path.glob("portfolio.*.bak"))) == 4  # 1 original + 3 new, all distinct


def test_only_the_newest_snapshots_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    db.conn.execute("CREATE TABLE t (x)")
    db.conn.commit()
    made = [
        runner.create_snapshot(db, "v1", f"v{i}") for i in range(2, 2 + runner.KEEP_BACKUPS + 3)
    ]
    left = sorted(p.name for p in tmp_path.glob("portfolio.*.bak"))
    assert len(left) == runner.KEEP_BACKUPS
    assert made[-1].name in left and made[0].name not in left
    db.close()


def test_partial_snapshot_from_a_crash_is_cleaned_up(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    db.conn.execute("CREATE TABLE t (x)")
    db.conn.commit()
    stale = tmp_path / "portfolio.v1-to-v2.20200101T000000000000Z.bak.partial"
    stale.write_bytes(b"half written")
    runner.create_snapshot(db, "v1", "v2")
    assert not stale.exists()
    db.close()


def test_migration_is_refused_when_the_backup_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path)
    before = _fingerprint(path)
    db = DatabaseConnection(path)

    def full_disk(*_a: object, **_k: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(runner.sqlite3, "connect", full_disk)
    with pytest.raises(MigrationError, match="not changed"):
        run_migrations(db)
    monkeypatch.undo()
    db.close()
    assert _fingerprint(path) == before, "no migration may run without a backup"


# --------------------------------------------------------------------------- misreads


def test_unreadable_migration_state_is_not_mistaken_for_a_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path)
    before = _fingerprint(path)
    db = DatabaseConnection(path)
    real = db.fetchall

    def flaky(sql: str, params: tuple | list = ()) -> list:
        if "schema_migration" in sql:
            raise sqlite3.OperationalError("database is locked")
        return real(sql, params)

    monkeypatch.setattr(db, "fetchall", flaky)
    with pytest.raises(MigrationError, match="migration state"):
        run_migrations(db)
    monkeypatch.undo()
    db.close()
    assert _fingerprint(path) == before
    assert list(tmp_path.glob("*.bak")) == [], "must not start migrating or backing up"


def test_database_from_a_newer_version_is_refused_and_untouched(tmp_path: Path) -> None:
    """An older build must not read or write a schema it does not understand."""
    path = tmp_path / "portfolio.db"
    db = DatabaseConnection(path)
    run_migrations(db)
    db.execute("INSERT INTO schema_migration (version, description) VALUES ('v99', 'future')")
    db.conn.commit()
    db.close()
    before = _fingerprint(path)

    db = DatabaseConnection(path)
    with pytest.raises(NewerDatabaseError, match="newer version"):
        run_migrations(db)
    db.close()

    assert _fingerprint(path) == before
    assert not list(tmp_path.glob("*.bak*")), "a refused database must not be snapshotted"


def test_newer_database_with_pending_migrations_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    _legacy_v2_db(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_migration (version, description) VALUES ('v99', 'future')")
    conn.commit()
    conn.close()
    before = _fingerprint(path)

    db = DatabaseConnection(path)
    with pytest.raises(NewerDatabaseError):
        run_migrations(db)
    db.close()

    assert _fingerprint(path) == before
    assert "v3" not in _versions(DatabaseConnection(path))
