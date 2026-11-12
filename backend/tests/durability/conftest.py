"""Fixtures for opt-in durability tests. Run with: ``pytest -m durability -s``."""

import errno
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.durability

SRC = str(Path(__file__).resolve().parents[2] / "src")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/durability" in str(item.fspath):
            item.add_marker(pytest.mark.durability)


@pytest.fixture
def small_volume(tmp_path: Path) -> Iterator[Path]:
    """A real, tiny (6 MB) macOS volume, so writes hit a genuine ENOSPC."""
    if sys.platform != "darwin" or shutil.which("hdiutil") is None:
        pytest.skip("needs macOS hdiutil")
    image = tmp_path / "small.dmg"
    mount = tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(
        [
            "hdiutil",
            "create",
            "-size",
            "6m",
            "-fs",
            "Journaled HFS+",
            "-volname",
            "PMTEST",
            str(image),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)],
        check=True,
        capture_output=True,
    )
    try:
        yield mount
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(mount)], capture_output=True)


def fill_disk(mount: Path, leave: int = 0) -> Path:
    """Fill the volume until the OS reports ENOSPC, then free *leave* bytes again."""
    filler = mount / "filler.bin"
    fd = os.open(filler, os.O_WRONLY | os.O_CREAT)
    chunk = b"\0" * 4096
    size = 0
    try:
        while True:
            try:
                size += os.write(fd, chunk)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise
                break
        if leave:
            os.ftruncate(fd, max(0, size - leave))
        os.fsync(fd)
    finally:
        os.close(fd)
    return filler
