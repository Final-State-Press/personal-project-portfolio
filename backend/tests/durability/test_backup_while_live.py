"""Copying a database that is being written: what Time Machine and file-sync tools do.

* **Atomic copy** (Time Machine on APFS copies from a point-in-time snapshot): modelled
  by pausing the writer with SIGSTOP, copying every file, then resuming. Every such
  copy must open cleanly with all acknowledged data.
* **Naive copy** (a sync client uploading files one by one while the app runs): modelled
  by copying the files in sequence with no pause. Reported, not asserted, because the
  result depends on timing; it is the reason the app must not live in a synced folder.
"""

import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.durability.conftest import SRC
from tests.durability.test_crash_consistency import _WRITER

pytestmark = pytest.mark.durability

COPIES = int(os.environ.get("DURABILITY_LIVE_COPIES", "40"))
FILES = ("portfolio.db", "portfolio.db-wal", "portfolio.db-shm")


def _start_writer(path: Path, home: Path) -> tuple[subprocess.Popen, list[int]]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _WRITER.format(src=SRC, path=str(path)), "0"],
        stdout=subprocess.PIPE,
        env={**os.environ, "HOME": str(home)},
        text=True,
    )
    return proc, []


def _drain_acks(proc: subprocess.Popen, until: float) -> int:
    acked = 0
    while time.time() < until:
        line = proc.stdout.readline()
        if line.strip().isdigit():
            acked = int(line)
    return acked


def _check(copy_dir: Path) -> tuple[bool, int]:
    try:
        conn = sqlite3.connect(copy_dir / "portfolio.db")
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        top = conn.execute("SELECT coalesce(max(n), 0) FROM ledger").fetchone()[0]
        conn.close()
        return ok, top
    except sqlite3.Error:
        return False, -1


def test_a_point_in_time_copy_of_a_live_database_is_always_restorable(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    proc, _ = _start_writer(path, tmp_path / "home")
    rnd = random.Random(7)
    try:
        _drain_acks(proc, time.time() + 1.0)  # let it create the schema and get busy
        bad = 0
        for i in range(COPIES):
            acked = _drain_acks(proc, time.time() + rnd.uniform(0.05, 0.4))
            os.kill(proc.pid, signal.SIGSTOP)  # freeze: an atomic view of all files
            dest = tmp_path / f"snap{i}"
            dest.mkdir()
            for name in FILES:
                src = tmp_path / name
                if src.exists():
                    shutil.copy2(src, dest / name)
            os.kill(proc.pid, signal.SIGCONT)
            ok, top = _check(dest)
            if not ok or top < acked:
                bad += 1
        print(
            f"\n[atomic live copy] {COPIES} copies, {bad} unrestorable or missing acknowledged data"
        )
        assert bad == 0
    finally:
        proc.kill()
        proc.wait()


def test_report_how_often_a_naive_file_by_file_copy_of_a_live_database_is_bad(
    tmp_path: Path,
) -> None:
    path = tmp_path / "portfolio.db"
    proc, _ = _start_writer(path, tmp_path / "home")
    rnd = random.Random(11)
    bad = lost = 0
    try:
        _drain_acks(proc, time.time() + 1.0)
        for i in range(COPIES):
            acked = _drain_acks(proc, time.time() + rnd.uniform(0.05, 0.3))
            dest = tmp_path / f"naive{i}"
            dest.mkdir()
            for name in FILES:  # no pause: the writer keeps committing between these copies
                src = tmp_path / name
                if src.exists():
                    shutil.copy2(src, dest / name)
                    time.sleep(0.02)
            ok, top = _check(dest)
            bad += not ok
            lost += ok and top < acked
        print(
            f"\n[naive live copy] {COPIES} copies: {bad} corrupt/unreadable, "
            f"{lost} readable but missing acknowledged commits"
        )
    finally:
        proc.kill()
        proc.wait()
