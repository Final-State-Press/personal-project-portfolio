"""Storage failures reach the user as specific, honest errors."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container


class _Failing:
    def __init__(self, real: object, exc: Exception) -> None:
        self._real, self._exc = real, exc

    def execute(self, sql: str, *a: object, **k: object) -> object:
        if sql.lstrip().upper().startswith("INSERT"):
            raise self._exc
        return self._real.execute(sql, *a, **k)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


_FULL = sqlite3.OperationalError("database or disk is full")
_IO = sqlite3.OperationalError("disk I/O error")
_BAD = sqlite3.DatabaseError("database disk image is malformed")
_RO = sqlite3.OperationalError("attempt to write a readonly database")
_OTHER = sqlite3.OperationalError("something else")


@pytest.mark.parametrize(
    ("exc", "status", "code", "retryable", "words"),
    [
        (_FULL, 507, "DISK_FULL", True, "disk is full"),
        (_IO, 500, "DATABASE_ERROR", True, "not have been saved"),
        (_BAD, 500, "DATABASE_CORRUPT", False, "damaged"),
        (_RO, 500, "DATABASE_READ_ONLY", False, "permissions"),
        (_OTHER, 500, "DATABASE_ERROR", True, "database error"),
    ],
)
def test_storage_error_is_translated(tmp_path, exc, status, code, retryable, words) -> None:  # noqa: ANN001
    c = build_container(
        config_path=tmp_path / "c.toml", db_path=tmp_path / "p.db", token="t", production=True
    )
    c.db._conn = _Failing(c.db.conn, exc)  # noqa: SLF001
    client = TestClient(create_app(c), raise_server_exceptions=False)

    r = client.post("/api/v1/projects", headers={"X-API-Key": "t"}, json={"name": "N"})

    body = r.json()
    assert (r.status_code, body["code"], body["retryable"]) == (status, code, retryable)
    assert words in body["message"]
    assert "Traceback" not in body["message"]
