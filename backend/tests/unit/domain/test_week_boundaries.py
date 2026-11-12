"""Week keys across year boundaries, time zones and daylight-saving changes.

The app derives week keys from *local* dates (what the user sees on their wall
calendar) and stores timestamps as naive UTC. These tests pin both rules down.
"""

import os
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from portfolio_manager.domain.week import (
    current_week_key,
    to_week_key,
    week_key_to_date_range,
)

SRC = str(Path(__file__).resolve().parents[3] / "src")


@pytest.mark.parametrize(
    ("day", "key"),
    [
        (date(2024, 12, 29), "2024.52"),
        (date(2024, 12, 30), "2025.1"),  # Monday of ISO week 1, still in December
        (date(2025, 12, 28), "2025.52"),
        (date(2025, 12, 29), "2026.1"),
        (date(2026, 1, 1), "2026.1"),
        (date(2026, 12, 28), "2026.53"),  # 2026 has 53 ISO weeks
        (date(2027, 1, 1), "2026.53"),  # January days belonging to the previous year's week
        (date(2027, 1, 3), "2026.53"),
        (date(2027, 1, 4), "2027.1"),
        (date(2020, 12, 31), "2020.53"),
        (date(2021, 1, 3), "2020.53"),
        (date(2024, 2, 29), "2024.9"),  # leap day
    ],
)
def test_iso_week_boundaries(day: date, key: str) -> None:
    assert to_week_key(day) == key


def test_every_date_falls_inside_its_own_week_range() -> None:
    """Round trip for every day from 2000 to 2040: key -> Monday..Sunday contains the day."""
    d = date(2000, 1, 1)
    end = date(2040, 12, 31)
    while d <= end:
        monday, sunday = week_key_to_date_range(to_week_key(d))
        assert monday.weekday() == 0 and sunday.weekday() == 6
        assert monday <= d <= sunday, d
        assert (sunday - monday).days == 6
        d += timedelta(days=1)


def test_consecutive_weeks_tile_the_calendar_without_gaps_or_overlaps() -> None:
    d = date(2019, 12, 30)  # a Monday
    previous_sunday = None
    while d < date(2032, 1, 1):
        monday, sunday = week_key_to_date_range(to_week_key(d))
        if previous_sunday is not None:
            assert monday == previous_sunday + timedelta(days=1), d
        previous_sunday = sunday
        d += timedelta(days=7)


_CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {src!r})
    instant = float(sys.argv[1])
    time.time = lambda: instant           # freeze "now" for date.today()
    from portfolio_manager.domain.week import current_week_key
    from portfolio_manager.infrastructure.system.clock import SystemClock
    c = SystemClock()
    print(current_week_key(), c.today().isoformat(), c.now().isoformat())
    """
)


def _run_in_zone(tz: str, instant: float) -> tuple[str, str, str]:
    env = {**os.environ, "TZ": tz, "HOME": os.environ.get("HOME", "")}
    out = subprocess.run(
        [sys.executable, "-c", _CHILD.format(src=SRC), str(instant)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=30,
    ).stdout.split()
    return out[0], out[1], out[2]


def _assert_same_utc_moment(*stamps: str) -> None:
    """Stored timestamps are naive UTC: identical in every zone (allowing for run time)."""
    real = datetime.now(UTC).replace(tzinfo=None)
    for stamp in stamps:
        assert abs((datetime.fromisoformat(stamp) - real).total_seconds()) < 30, stamp


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    import calendar

    return float(calendar.timegm((y, m, d, hh, mm, 0)))


def test_week_key_follows_the_local_calendar_but_stored_time_is_always_utc() -> None:
    # 2027-01-03 12:00 UTC is Sunday in UTC but already Monday 2027-01-04 in Kiritimati (+14).
    instant = _utc(2027, 1, 3, 12)
    kiritimati = _run_in_zone("Pacific/Kiritimati", instant)
    utc = _run_in_zone("UTC", instant)
    pago_pago = _run_in_zone("Pacific/Pago_Pago", instant)  # UTC-11: still Sunday
    assert kiritimati[0] == "2027.1" and kiritimati[1] == "2027-01-04"
    assert utc[0] == "2026.53" and utc[1] == "2027-01-03"
    assert pago_pago[0] == "2026.53" and pago_pago[1] == "2027-01-03"
    # The UTC timestamp used for completed_at does not depend on the zone at all
    # (+14h and -11h zones would differ by 25 hours if it did).
    _assert_same_utc_moment(kiritimati[2], utc[2], pago_pago[2])


@pytest.mark.parametrize(
    "instant",
    [
        _utc(2026, 3, 8, 9, 30),  # US spring forward: 02:30 local does not exist
        _utc(2026, 3, 8, 10, 30),
        _utc(2026, 11, 1, 8, 30),  # US fall back: 01:30 local happens twice
        _utc(2026, 11, 1, 9, 30),
    ],
)
def test_daylight_saving_transitions_do_not_disturb_week_or_utc_time(instant: float) -> None:
    la = _run_in_zone("America/Los_Angeles", instant)
    utc = _run_in_zone("UTC", instant)
    _assert_same_utc_moment(la[2], utc[2])  # stored UTC time ignores the local DST change
    # Local week key is stable: both sides of the transition are the same Sunday/Monday week
    # computed from a real local date.
    assert la[0] == to_week_key(date.fromisoformat(la[1]))


def test_current_week_key_accepts_an_injected_date() -> None:
    assert current_week_key(date(2027, 1, 1)) == "2026.53"
