"""Smoke test of the *frozen* sidecar (PyInstaller build), not the Python sources.

Build it first: ``python scripts/build_sidecar.py``. Skipped if it is missing.
Runs against a temporary ``HOME`` so it never touches ``~/.portfolio_manager``.
"""

import hashlib
import hmac
import json
import os
import signal
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from tests.integration.test_migration_safety import _legacy_v2_db

pytestmark = pytest.mark.durability

BINARY = Path(
    os.environ.get(
        "SIDECAR_BINARY",
        Path(__file__).resolve().parents[3]
        / "src-tauri/binaries/portfolio-sidecar/portfolio-sidecar",
    )
)
TOKEN = "packaged-test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(port: int, path: str, *, key: str | None = None, body: dict | None = None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if body else "GET")
    for k, v in {**({"X-API-Key": key} if key else {}), **(headers or {})}.items():
        req.add_header(k, v)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def _start(home: Path, port: int, *, token: str | None = TOKEN, production: bool = True):
    env = {k: v for k, v in os.environ.items() if k != "PORTFOLIO_SIDECAR_TOKEN"}
    env["HOME"] = str(home)
    if token:
        env["PORTFOLIO_SIDECAR_TOKEN"] = token
    args = [str(BINARY), "--port", str(port)] + (["--production"] if production else [])
    return subprocess.Popen(
        args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )


def _wait_ready(port: int, timeout: float = 30) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if _call(port, "/health")[0] == 200:
                return time.time() - t0
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError("packaged sidecar never became healthy")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    if not BINARY.is_file():
        pytest.skip(f"frozen sidecar not built: {BINARY}")
    (tmp_path / "home" / ".portfolio_manager").mkdir(parents=True)
    return tmp_path / "home"


def test_frozen_sidecar_upgrades_serves_proves_identity_and_survives_sigkill(home: Path) -> None:
    db_path = home / ".portfolio_manager" / "portfolio.db"
    _legacy_v2_db(db_path)  # a pre-Tauri database that needs the v3/v4 upgrade
    port = _free_port()
    proc = _start(home, port)
    try:
        boot = _wait_ready(port)
        print(f"\n[packaged] healthy {boot:.1f}s after launch (includes v2->v4 upgrade)")

        # Identity proof works in the frozen build (hmac/hashlib bundled correctly).
        nonce = "00112233445566778899aabbccddeeff"
        status, body = _call(port, "/health", headers={"X-Challenge": nonce})
        expected = hmac.new(TOKEN.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        assert status == 200 and body.get("proof") == expected

        # Auth is enforced, docs are off in production, upgraded legacy data is served.
        assert _call(port, "/api/v1/projects")[0] == 401
        assert _call(port, "/docs")[0] in (401, 404)  # never the interactive docs page
        status, body = _call(port, "/api/v1/projects", key=TOKEN)
        assert status == 200 and [p["name"] for p in body["projects"]] == ["P"]
        status, created = _call(port, "/api/v1/projects", key=TOKEN, body={"name": "Written live"})
        assert status == 201

        # A consistent pre-upgrade snapshot was written.
        (snap,) = (home / ".portfolio_manager").glob("portfolio.v2-to-v4.*.bak")
        check = sqlite3.connect(snap)
        assert check.execute("SELECT count(*) FROM project").fetchone()[0] == 1
        check.close()

        proc.send_signal(signal.SIGKILL)  # what the shell's shutdown does
        proc.wait()

        port2 = _free_port()
        proc = _start(home, port2)
        print(f"[packaged] restart after SIGKILL healthy in {_wait_ready(port2):.1f}s")
        status, body = _call(port2, "/api/v1/projects", key=TOKEN)
        assert sorted(p["name"] for p in body["projects"]) == ["P", "Written live"]
        assert not list((home / ".portfolio_manager").glob("*.partial"))
    finally:
        proc.kill()
        proc.wait()


def test_frozen_sidecar_refuses_production_start_without_a_token(home: Path) -> None:
    proc = _start(home, _free_port(), token=None)
    try:
        assert proc.wait(timeout=30) == 78
    finally:
        proc.kill()
