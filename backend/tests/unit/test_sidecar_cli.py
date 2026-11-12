"""Sidecar entry-point startup policy."""

import contextlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from portfolio_manager.cli import sidecar
from portfolio_manager.domain.errors import (
    DatabaseCorruptError,
    MigrationError,
    NewerDatabaseError,
)


def _with_cause(exc: BaseException, cause: BaseException) -> BaseException:
    exc.__cause__ = cause
    return exc


def test_production_without_token_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTFOLIO_SIDECAR_TOKEN", raising=False)
    monkeypatch.setattr(sidecar, "read_startup_token", lambda: "")
    monkeypatch.setattr(
        sidecar.uvicorn.Server, "run", lambda *a, **k: pytest.fail("server must not start")
    )
    monkeypatch.setattr(sidecar, "build_app", lambda **k: pytest.fail("app must not be built"))

    with pytest.raises(SystemExit) as exc:
        sidecar.main(["--port", "8765", "--production"])

    assert exc.value.code == sidecar.EXIT_NO_TOKEN


def test_production_with_token_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    monkeypatch.setattr(sidecar, "read_startup_token", lambda: "t" * 64)
    monkeypatch.setattr(sidecar, "build_app", lambda **k: object())
    monkeypatch.setattr(
        sidecar.uvicorn.Server, "run", lambda self: started.append(self.config.host)
    )

    sidecar.main(["--port", "8765", "--production"])

    assert started and started[0] == "127.0.0.1"


def test_development_without_token_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[dict] = []
    monkeypatch.setattr(sidecar, "read_startup_token", lambda: "")
    monkeypatch.setattr(sidecar, "build_app", lambda **k: object())
    monkeypatch.setattr(
        sidecar.uvicorn.Server, "run", lambda self: started.append(self.config.host)
    )

    sidecar.main(["--port", "8765"])

    assert started


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (sqlite3.OperationalError("database or disk is full"), sidecar.EXIT_STORAGE),
        (sqlite3.OperationalError("unable to open database file"), sidecar.EXIT_STORAGE),
        (OSError(28, "No space left on device"), sidecar.EXIT_STORAGE),
        (
            _with_cause(
                MigrationError("Could not back up"), sqlite3.OperationalError("disk is full")
            ),
            sidecar.EXIT_STORAGE,
        ),
        (MigrationError("Migration 'v3' failed and was rolled back"), sidecar.EXIT_DATA),
        (sqlite3.DatabaseError("database disk image is malformed"), sidecar.EXIT_DATA),
        (NewerDatabaseError("newer"), sidecar.EXIT_NEWER_DATABASE),
        (RuntimeError("boom"), sidecar.EXIT_STARTUP),
    ],
)
def test_startup_failures_get_distinct_exit_codes(exc: BaseException, code: int) -> None:
    assert sidecar.classify_startup_failure(exc)[0] == code


def test_failed_startup_exits_with_the_classified_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sidecar, "read_startup_token", lambda: "t" * 64)

    def boom(**_: object) -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(sidecar, "build_app", boom)
    monkeypatch.setattr(sidecar.uvicorn.Server, "run", lambda *a: pytest.fail("must not serve"))

    with pytest.raises(SystemExit) as exc:
        sidecar.main(["--port", "8765", "--production"])

    assert exc.value.code == sidecar.EXIT_STORAGE


_PARENT = """
import os, subprocess, sys, time
env = dict(os.environ, PORTFOLIO_SIDECAR_TOKEN="tok", HOME=sys.argv[2])
c = subprocess.Popen(
    [sys.executable, "-m", "portfolio_manager.cli.sidecar", "--port", sys.argv[1], "--production"],
    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print(c.pid, flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_sidecar_exits_when_its_parent_is_killed(tmp_path: Path) -> None:
    """A killed or crashed app must not leave the sidecar running with the DB open."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    src = str(Path(sidecar.__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": src}
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, str(port), str(tmp_path)],
        stdout=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert parent.stdout is not None
    child = int(parent.stdout.readline())
    try:
        deadline = time.time() + 20
        while time.time() < deadline:  # wait until it is really serving
            with socket.socket() as c:
                c.settimeout(0.5)
                if c.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.3)
        else:
            pytest.fail("sidecar never started")
        assert _alive(child)

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()

        deadline = time.time() + 15
        while time.time() < deadline and _alive(child):
            time.sleep(0.3)
        assert not _alive(child), "sidecar was orphaned after its parent was killed"
    finally:
        with contextlib.suppress(OSError):
            os.kill(child, signal.SIGKILL)


def test_damaged_database_has_its_own_exit_status() -> None:
    assert sidecar.classify_startup_failure(DatabaseCorruptError("x"))[0] == sidecar.EXIT_CORRUPT


def test_sigterm_stops_the_sidecar_cleanly_and_checkpoints_the_database(tmp_path: Path) -> None:
    """The shell's graceful stop must leave one complete file, not a pending WAL."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    src = str(Path(sidecar.__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": src, "HOME": str(tmp_path), "PORTFOLIO_SIDECAR_TOKEN": "t"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "portfolio_manager.cli.sidecar",
            "--port",
            str(port),
            "--production",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            with socket.socket() as c:
                c.settimeout(0.5)
                if c.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.3)
        else:
            pytest.fail("sidecar never started")
        urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/projects",
                data=b'{"name": "Saved"}',
                headers={"X-API-Key": "t", "Content-Type": "application/json"},
            ),
            timeout=5,
        )
        db = tmp_path / ".portfolio_manager" / "portfolio.db"
        assert Path(f"{db}-wal").exists(), "precondition: a live WAL exists"

        proc.send_signal(signal.SIGTERM)
        # uvicorn re-raises the signal after a graceful shutdown, so -SIGTERM is normal.
        assert proc.wait(timeout=10) in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert not Path(f"{db}-wal").exists() or Path(f"{db}-wal").stat().st_size == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT name FROM project").fetchall() == [("Saved",)]
    conn.close()
