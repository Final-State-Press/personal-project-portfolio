"""Config file safety: escaping, atomic writes, and recovery from a damaged file."""

import os
import tomllib
from pathlib import Path

import pytest

from portfolio_manager.infrastructure.config.models import DatabaseConfig
from portfolio_manager.infrastructure.config.toml_repository import (
    TomlSettingsRepository,
    toml_string,
)

NASTY = [
    '/tmp/x.db"\n[app]\nlog_level = "DEBUG',
    "back\\slash\\n",
    "quote\"s and 'single'",
    "tab\there\x00nul\x7fdel",
    "unicode é 日本 🙂",
    "",
]


@pytest.mark.parametrize("value", NASTY)
def test_toml_string_round_trips_and_cannot_inject(value: str) -> None:
    parsed = tomllib.loads(f"k = {toml_string(value)}\n")
    assert parsed == {"k": value}


@pytest.mark.parametrize("value", NASTY[:4])
def test_saved_path_never_changes_other_settings(tmp_path: Path, value: str) -> None:
    repo = TomlSettingsRepository(tmp_path / "config.toml")
    settings = repo.load()
    repo.save(settings.model_copy(update={"database": DatabaseConfig(path=value)}))

    reloaded = repo.load()
    assert str(reloaded.database.path) == value
    assert reloaded.app.log_level == "INFO"
    assert repo.recovered_from is None


def test_save_is_atomic_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    repo = TomlSettingsRepository(path)
    settings = repo.load()
    good = path.read_text()

    def boom(*_: object) -> None:
        raise OSError("simulated crash before the swap")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated"):
        repo.save(settings.model_copy(update={"database": DatabaseConfig(path="/elsewhere.db")}))
    monkeypatch.undo()

    assert path.read_text() == good, "an interrupted save must leave the old file intact"
    assert not list(tmp_path.glob(".config.toml.*")), "no temp file may be left behind"


@pytest.mark.parametrize(
    "damage",
    [
        "[app]\nlog_le",  # truncated mid-write
        "\x00\x00\x00\x00",  # zero-filled after a crash
        "[app]\ntheme = 5\n",  # valid TOML, wrong type
        "[session]\nweekly_budget_hours = 9999\n",  # out of range
        '[database]\npath = "x"\n[database]\n',  # duplicate table
    ],
)
def test_damaged_config_is_set_aside_and_defaults_used(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(damage)
    repo = TomlSettingsRepository(path)

    settings = repo.load()  # must not raise

    assert settings.app.theme == "light"
    assert repo.recovered_from is not None
    assert repo.recovered_from.read_text() == damage, "the user's file must be kept"
    assert ".corrupt-" in repo.recovered_from.name
    assert TomlSettingsRepository(path).load().app.theme == "light"
    assert TomlSettingsRepository(path).recovered_from is None  # healed: no repeat


def test_sidecar_container_starts_with_a_damaged_config(tmp_path: Path) -> None:
    from portfolio_manager.bootstrap import build_container

    cfg = tmp_path / "config.toml"
    cfg.write_text("[app\nbroken")
    c = build_container(config_path=cfg, db_path=tmp_path / "p.db", token="t", production=True)
    try:
        assert c.db.fetchone("PRAGMA integrity_check")[0] == "ok"
    finally:
        c.close()
    assert list(tmp_path.glob("config.toml.corrupt-*"))
