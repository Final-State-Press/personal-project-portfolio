"""TOML settings adapter.

Reads and writes ``~/.portfolio_manager/config.toml``, creating it with defaults
on first launch. Preserves the exact TOML layout produced by the original
``config/settings.py`` so existing config files remain valid.

Implements :class:`portfolio_manager.application.ports.settings_repository.SettingsRepository`.
"""

import contextlib
import logging
import os
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from portfolio_manager.domain.errors import ConfigError
from portfolio_manager.infrastructure.config.models import (
    AppConfig,
    DatabaseConfig,
    SessionConfig,
    Settings,
)
from portfolio_manager.infrastructure.system.paths import CONFIG_FILE

logger = logging.getLogger(__name__)

_DEFAULT_TOML = """\
[app]
log_level = "INFO"
theme = "light"

[session]
default_duration_minutes = 90
weekly_budget_hours = 12

[database]
path = "~/.portfolio_manager/portfolio.db"
"""


def toml_string(value: str) -> str:
    """Return *value* as a TOML basic string, escaping everything that needs it.

    Quotes, backslashes and control characters (including newlines) are escaped,
    so a value can never end a string early and inject keys or tables.

    :param value: Text to encode.
    :rtype: str
    """
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* so *path* is always either the old or the new file, never partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class TomlSettingsRepository:
    """Load and persist :class:`Settings` from a TOML file.

    :param config_path: Path to the config file. Defaults to
        ``~/.portfolio_manager/config.toml``.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._path = config_path or CONFIG_FILE
        self._recovered_from: Path | None = None

    @property
    def recovered_from(self) -> Path | None:
        """Where an unreadable config file was moved aside to, if that happened.

        :rtype: pathlib.Path | None
        """
        return self._recovered_from

    @property
    def path(self) -> Path:
        """The resolved config file path.

        :rtype: pathlib.Path
        """
        return self._path

    def load(self) -> Settings:
        """Load settings, writing defaults if the file is absent.

        :returns: Validated settings.
        :rtype: Settings
        :raises ConfigError: If the file is unreadable and cannot be moved aside.
        """
        if not self._path.exists():
            self._write_defaults()

        try:
            return self._read()
        except ConfigError as exc:
            # A damaged config must never stop the app from starting. Keep the
            # bad file for the user, then carry on with defaults.
            aside = self._path.with_name(
                f"{self._path.name}.corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                os.replace(self._path, aside)
            except OSError:
                raise exc from None
            self._recovered_from = aside
            logger.warning(
                "Config file was unreadable (%s). It was moved to %s and defaults are in use.",
                exc,
                aside,
            )
            self._write_defaults()
            return self._read()

    def _read(self) -> Settings:
        try:
            with self._path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Could not parse config file {self._path}: {exc}") from exc
        try:
            return Settings(
                app=AppConfig(**raw.get("app", {})),
                session=SessionConfig(**raw.get("session", {})),
                database=DatabaseConfig(**raw.get("database", {})),
            )
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"Invalid configuration in {self._path}: {exc}") from exc

    def save(self, settings: Settings) -> None:
        """Persist *settings* back to the TOML file.

        :param settings: Settings to persist.
        """
        content = (
            "[app]\n"
            f"log_level = {toml_string(settings.app.log_level)}\n"
            f"theme = {toml_string(settings.app.theme)}\n"
            "\n"
            "[session]\n"
            f"default_duration_minutes = {settings.session.default_duration_minutes}\n"
            f"weekly_budget_hours = {settings.session.weekly_budget_hours}\n"
            "\n"
            "[database]\n"
            f"path = {toml_string(str(settings.database.path))}\n"
        )
        _atomic_write(self._path, content)
        logger.info("Saved settings to %s", self._path)

    def _write_defaults(self) -> None:
        _atomic_write(self._path, _DEFAULT_TOML)
        logger.info("Wrote default config to %s", self._path)
