"""Sidecar entry point.

Launched by the Rust supervisor. Binds only to ``127.0.0.1`` on the supplied
port, reads the auth token from the environment or stdin (never argv), runs
migrations, and serves the FastAPI app via Uvicorn.

Usage::

    PORTFOLIO_SIDECAR_TOKEN=<token> python -m portfolio_manager.cli.sidecar \\
        --port 8765 [--production]
"""

import argparse
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator

import uvicorn

from portfolio_manager.api.app import create_app
from portfolio_manager.bootstrap import build_container
from portfolio_manager.domain.errors import (
    DatabaseCorruptError,
    MigrationError,
    NewerDatabaseError,
)
from portfolio_manager.infrastructure.logging import configure_logging
from portfolio_manager.infrastructure.security.token import read_startup_token

#: Loopback host — the sidecar must never bind a routable interface.
HOST = "127.0.0.1"

#: Exit status when ``--production`` is used without an auth token.
EXIT_NO_TOKEN = 78

#: Exit status when the database cannot be opened or written (disk full, folder
#: not writable). Matches ``EX_IOERR``; the Tauri shell turns it into a message.
EXIT_STORAGE = 74

#: Exit status when the database is damaged or could not be upgraded
#: (``EX_DATAERR``). A failed upgrade leaves the data unchanged.
EXIT_DATA = 65

#: Exit status when the database was created by a newer version of the app.
EXIT_NEWER_DATABASE = 67

#: Exit status when the database file is damaged.
EXIT_CORRUPT = 68

#: Exit status for any other startup failure (``EX_SOFTWARE``).
EXIT_STARTUP = 70

#: Seconds after a parent-death shutdown request before the process is forced out.
FORCE_EXIT_AFTER = 5.0

_ENOSPC = 28
_STORAGE_MARKERS = (
    "disk is full",
    "no space left",
    "unable to open database",
    "disk i/o error",
    "readonly database",
)

logger = logging.getLogger(__name__)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    cur: BaseException | None = exc
    for _ in range(8):
        if cur is None:
            return
        yield cur
        cur = cur.__cause__ or cur.__context__


def classify_startup_failure(exc: BaseException) -> tuple[int, str]:
    """Map a startup exception to an exit status and a plain-language message.

    :param exc: The exception raised while building the app.
    :returns: ``(exit_status, message)``.
    """
    if isinstance(exc, DatabaseCorruptError):
        return EXIT_CORRUPT, str(exc)
    if isinstance(exc, NewerDatabaseError):
        return EXIT_NEWER_DATABASE, str(exc)
    text = " | ".join(str(e).lower() for e in _chain(exc))
    if any(m in text for m in _STORAGE_MARKERS) or any(
        isinstance(e, OSError) and e.errno == _ENOSPC for e in _chain(exc)
    ):
        return EXIT_STORAGE, (
            "The database could not be opened or written. The disk may be full, or the "
            f"database folder is not writable: {exc}"
        )
    if isinstance(exc, MigrationError | sqlite3.DatabaseError):
        return EXIT_DATA, f"The database could not be read or upgraded: {exc}"
    return EXIT_STARTUP, f"Startup failed: {exc}"


def _restore_latest() -> int:
    """Restore the newest backup over the configured database. Returns an exit status."""
    from portfolio_manager.infrastructure.config.toml_repository import TomlSettingsRepository
    from portfolio_manager.infrastructure.db.restore import restore_latest

    try:
        db_path = TomlSettingsRepository().load().database.resolved_path
        name = restore_latest(db_path)
    except Exception as exc:
        logger.error("Restore failed: %s", exc)
        return EXIT_STARTUP
    logger.info("Restored %s.", name)
    return 0


def watch_parent(server: uvicorn.Server, interval: float = 1.0) -> threading.Thread | None:
    """Shut the server down when the process that launched it goes away.

    The Tauri shell kills the sidecar on a normal quit, but not if the shell is
    killed or crashes. The sidecar is then re-parented (to ``launchd``) and, without
    this, would keep the database open indefinitely.

    :param server: The running Uvicorn server.
    :param interval: Polling interval in seconds.
    :returns: The watchdog thread, or ``None`` if there is no parent to watch.
    """
    parent = os.getppid()
    if parent <= 1:
        return None

    def _watch() -> None:
        while not server.should_exit:
            if os.getppid() != parent:
                logger.warning("Launching process is gone; shutting the sidecar down.")
                server.should_exit = True
                time.sleep(FORCE_EXIT_AFTER)
                logger.error("Graceful shutdown timed out; forcing exit.")
                os._exit(0)
            time.sleep(interval)

    thread = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    thread.start()
    return thread


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Portfolio Manager FastAPI sidecar")
    parser.add_argument("--port", type=int, help="Loopback port to bind")
    parser.add_argument(
        "--restore-latest",
        action="store_true",
        help="Replace the database with the newest backup, then exit (for a damaged database).",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Enable production hardening (disable docs).",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging level (default INFO).")
    return parser.parse_args(argv)


def build_app(production: bool, token: str):  # noqa: ANN201
    """Build the FastAPI app with a wired container.

    :param production: Enable production hardening.
    :param token: Expected sidecar auth token.
    :returns: The FastAPI application.
    """
    container = build_container(token=token, production=production)
    return create_app(container)


def main(argv: list[str] | None = None) -> None:
    """Parse args, build the app, and run Uvicorn on the loopback port.

    :param argv: Optional argument vector (defaults to ``sys.argv``).
    """
    args = _parse_args(argv)
    configure_logging(args.log_level)
    if args.restore_latest:
        raise SystemExit(_restore_latest())
    if args.port is None:
        raise SystemExit("--port is required")
    token = read_startup_token()
    if not token:
        if args.production:
            # Fail closed: a packaged build must never serve data unauthenticated.
            logger.error("No sidecar token supplied; refusing to start in production mode.")
            raise SystemExit(EXIT_NO_TOKEN)
        logger.warning("No sidecar token supplied; authentication is DISABLED.")

    try:
        app = build_app(production=args.production, token=token)
    except Exception as exc:
        code, message = classify_startup_failure(exc)
        logger.error("%s", message, exc_info=exc)
        raise SystemExit(code) from exc
    logger.info("Starting sidecar on %s:%d", HOST, args.port)
    config = uvicorn.Config(app, host=HOST, port=args.port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    watch_parent(server)
    server.run()


if __name__ == "__main__":
    main()
