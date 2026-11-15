"""Repeated hard kills of a busy writer: the closest honest stand-in for power loss.

This kills the *process* (SIGKILL), so it exercises SQLite's WAL recovery and
checkpoint handling, not loss of the operating system's write cache. See the
durability note for what this does and does not prove.
"""

import os
import random
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from tests.durability.conftest import SRC

pytestmark = pytest.mark.durability

ROUNDS = int(os.environ.get("DURABILITY_CRASH_ROUNDS", "30"))

_WRITER = textwrap.dedent(
    """
    import sys, random
    sys.path.insert(0, {src!r})
    from portfolio_manager.infrastructure.db.connection import DatabaseConnection
    from portfolio_manager.infrastructure.db.migrations import run_migrations

    db = DatabaseConnection({path!r})
    run_migrations(db)
    db.conn.execute("PRAGMA wal_autocheckpoint = 8")   # checkpoint constantly
    db.conn.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, n INTEGER, pad TEXT)")
    db.conn.commit()
    rnd = random.Random()
    n = int(sys.argv[1])
    while True:
        n += 1
        with db.transaction():
            db.execute("INSERT INTO ledger (n, pad) VALUES (?, ?)", (n, "p" * rnd.randint(10, 3000)))
            if n % 7 == 0:   # churn: updates and deletes force page reuse and checkpoints
                db.execute("UPDATE ledger SET pad = ? WHERE n = ?", ("u" * 2000, n - 3))
            if n % 11 == 0:
                db.execute("DELETE FROM ledger WHERE n = ?", (n - 5,))
        print(n, flush=True)   # acknowledged only after COMMIT returned
    """
)


def test_repeated_sigkill_never_loses_an_acknowledged_commit(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    env = {**os.environ, "HOME": str(tmp_path / "home")}
    acked = 0
    rnd = random.Random(20261110)
    for round_no in range(ROUNDS):
        proc = subprocess.Popen(
            [sys.executable, "-c", _WRITER.format(src=SRC, path=str(path)), str(acked)],
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        deadline = time.time() + rnd.uniform(0.3, 1.6)
        while time.time() < deadline:
            line = proc.stdout.readline()
            if line.strip().isdigit():
                acked = max(acked, int(line))
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()

        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", f"round {round_no}"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        present = conn.execute("SELECT max(n) FROM ledger").fetchone()[0] or 0
        # The last acknowledged commit, and everything before it, must be there.
        assert present >= acked, f"round {round_no}: acked {acked}, found max {present}"
        conn.close()
    print(f"\n[crash loop] {ROUNDS} SIGKILLs, {acked} acknowledged commits, 0 lost, integrity ok")
