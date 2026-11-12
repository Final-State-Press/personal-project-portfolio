"""A large database: upgrade time, snapshot cost, memory and API latency."""

import os
import random
import resource
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_migration_safety import _V1, _V2

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container
from portfolio_manager.infrastructure.db.migrations import runner

pytestmark = pytest.mark.durability

PROJECTS = int(os.environ.get("DURABILITY_PROJECTS", "2000"))
SESSIONS = int(os.environ.get("DURABILITY_SESSIONS", "150000"))
PLAN_KB = int(os.environ.get("DURABILITY_PLAN_KB", "40"))


def _build_legacy(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_V1)
    conn.executescript(_V2)
    conn.execute("INSERT INTO schema_migration (version, description) VALUES ('v1','x'),('v2','y')")
    rnd = random.Random(1)
    plan = ("# Plan\n" + "lorem ipsum dolor sit amet " * 40 + "\n") * max(1, PLAN_KB // 1)
    conn.executemany(
        "INSERT INTO project (name, slug, status, priority, plan_content) VALUES (?,?,?,?,?)",
        [
            (
                f"Project {i}",
                f"project-{i}",
                rnd.choice(["active", "backlog", "archive"]),
                3,
                plan[: PLAN_KB * 1024],
            )
            for i in range(PROJECTS)
        ],
    )
    conn.executemany(
        "INSERT INTO session (project_id, scheduled_date, week_key, duration_minutes, status, focus, notes)"
        " VALUES (?, ?, ?, 90, ?, ?, ?)",
        (
            (
                rnd.randint(1, PROJECTS),
                f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
                f"2026.{1 + i % 52}",
                rnd.choice(["completed", "planned", "cancelled"]),
                f"focus {i}",
                "n" * 80,
            )
            for i in range(SESSIONS)
        ),
    )
    conn.executemany(
        "INSERT INTO milestone (project_id, description, is_complete) VALUES (?, ?, ?)",
        ((rnd.randint(1, PROJECTS), f"milestone {i}", i % 2) for i in range(SESSIONS // 10)),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # bytes on macOS


def test_large_legacy_database_upgrades_and_stays_responsive(tmp_path: Path) -> None:
    path = tmp_path / "portfolio.db"
    t = time.perf_counter()
    _build_legacy(path)
    size_mb = path.stat().st_size / 1e6
    print(
        f"\n[large db] built {PROJECTS} projects, {SESSIONS} sessions: {size_mb:.0f} MB "
        f"in {time.perf_counter() - t:.1f}s"
    )

    rss_before = _rss_mb()
    t = time.perf_counter()
    c = build_container(
        config_path=tmp_path / "config.toml", db_path=path, token="t", production=True
    )
    upgrade_s = time.perf_counter() - t
    rss_after = _rss_mb()
    snaps = list(tmp_path.glob("portfolio.v2-to-v4.*.bak"))
    print(
        f"[large db] upgrade v2->v4 incl. snapshot + v3 table rebuild: {upgrade_s:.1f}s; "
        f"peak RSS {rss_before:.0f} -> {rss_after:.0f} MB; snapshot {snaps[0].stat().st_size / 1e6:.0f} MB"
    )

    t = time.perf_counter()
    assert c.db.fetchone("PRAGMA integrity_check")[0] == "ok"
    print(f"[large db] integrity_check: {time.perf_counter() - t:.1f}s")
    t = time.perf_counter()
    assert c.db.fetchone("PRAGMA quick_check")[0] == "ok"
    print(f"[large db] quick_check: {time.perf_counter() - t:.1f}s")

    client = TestClient(create_app(c))
    h = {"X-API-Key": "t"}
    for label, url in [
        ("list projects (incl. all plan text)", "/api/v1/projects"),
        ("dashboard, current week", "/api/v1/dashboard"),
        ("dashboard, busy week", "/api/v1/dashboard?week_key=2026.20"),
        ("sessions for a week", "/api/v1/sessions?week_key=2026.20"),
    ]:
        t = time.perf_counter()
        r = client.get(url, headers=h)
        ms = (time.perf_counter() - t) * 1000
        print(
            f"[large db] GET {label}: {r.status_code} in {ms:.0f} ms, {len(r.content) / 1e6:.1f} MB"
        )
        assert r.status_code == 200

    t = time.perf_counter()
    r = client.post("/api/v1/projects", headers=h, json={"name": "New"})
    print(
        f"[large db] create project: {r.status_code} in {(time.perf_counter() - t) * 1000:.0f} ms"
    )
    assert r.status_code == 201

    # Generous guards: these exist to catch an order-of-magnitude regression.
    assert upgrade_s < 180
    assert rss_after - rss_before < 6 * size_mb + 300
    assert runner.KEEP_BACKUPS >= 1 and len(snaps) == 1
    c.close()
