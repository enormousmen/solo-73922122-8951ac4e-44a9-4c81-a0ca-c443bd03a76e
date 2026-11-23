"""Window math: normal days, fall-back day and spring-forward day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backupdesk.clock import parse_utc, window_for

TZ = ZoneInfo("America/New_York")
UTC = timezone.utc


def utc(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def test_normal_day_window_is_90_minutes():
    window = window_for(date(2026, 11, 23), TZ)
    assert window.start_utc == utc(2026, 11, 23, 6, 30)
    assert window.end_utc == utc(2026, 11, 23, 8, 0)
    assert window.duration_seconds == 90 * 60


def test_fall_back_day_runs_from_first_0130_to_0300_real_time():
    window = window_for(date(2026, 11, 1), TZ)
    # First 01:30 is EDT (UTC-4) -> 05:30Z.  03:00 is EST (UTC-5) -> 08:00Z.
    assert window.start_utc == utc(2026, 11, 1, 5, 30)
    assert window.end_utc == utc(2026, 11, 1, 8, 0)
    assert window.duration_seconds == 150 * 60
    assert window.start_utc.astimezone(TZ).utcoffset() == timedelta(hours=-4)
    assert window.end_utc.astimezone(TZ).utcoffset() == timedelta(hours=-5)


def test_fall_back_repeated_hour_is_one_window_only():
    window = window_for(date(2026, 11, 1), TZ)
    # 01:30 EDT and 01:30 EST are both inside the same window; the second
    # occurrence must not extend it past 03:00 EST.
    first = datetime(2026, 11, 1, 1, 30, tzinfo=TZ, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=TZ, fold=1)
    assert window.contains(first) and window.contains(second)
    # Equal wall times, so compare the real instants.
    assert second.astimezone(UTC) > first.astimezone(UTC)
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)
    assert window.end_utc == datetime(2026, 11, 1, 3, 0, tzinfo=TZ).astimezone(UTC)
    assert not window.contains(window.end_utc)
    assert window.phase(window.end_utc) == "closed"


def test_fall_back_has_no_second_opening_after_second_0130():
    window = window_for(date(2026, 11, 1), TZ)
    second_0130_utc = datetime(2026, 11, 1, 1, 30, tzinfo=TZ, fold=1).astimezone(UTC)
    assert window.start_utc < second_0130_utc < window.end_utc
    # A job already completed in the first pass stays completed; the window
    # helper itself never produces a second window for the same date.
    assert window_for(date(2026, 11, 1), TZ) == window


def test_spring_forward_day_window_is_30_minutes():
    window = window_for(date(2026, 3, 8), TZ)
    assert window.start_utc == utc(2026, 3, 8, 6, 30)
    assert window.end_utc == utc(2026, 3, 8, 7, 0)
    assert window.duration_seconds == 30 * 60


def test_window_boundaries_are_half_open():
    window = window_for(date(2026, 11, 23), TZ)
    assert window.phase(window.start_utc - timedelta(microseconds=1)) == "before"
    assert window.phase(window.start_utc) == "open"
    assert window.phase(window.end_utc - timedelta(microseconds=1)) == "open"
    assert window.phase(window.end_utc) == "closed"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-11-01T05:30:00Z", utc(2026, 11, 1, 5, 30)),
        ("2026-11-01T05:30:00+00:00", utc(2026, 11, 1, 5, 30)),
        ("2026-11-01T01:30:00-04:00", utc(2026, 11, 1, 5, 30)),
        ("2026-11-01T06:30:00", utc(2026, 11, 1, 6, 30)),
    ],
)
def test_parse_utc(text, expected):
    assert parse_utc(text) == expected


@pytest.mark.parametrize("bad", ["", "nope", "2026-13-01T00:00:00Z", "1900-01-01T00:00:00Z"])
def test_parse_utc_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_utc(bad)


def test_duty_window_dict_reports_real_duration():
    window = window_for(date(2026, 11, 1), TZ)
    payload = window.to_dict(TZ)
    assert payload["duration_seconds"] == 9000
    assert payload["start_offset_seconds"] == -4 * 3600
    assert payload["end_offset_seconds"] == -5 * 3600
