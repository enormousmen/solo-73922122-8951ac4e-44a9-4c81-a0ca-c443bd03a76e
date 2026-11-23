"""New York scheduling windows and the drillable UTC clock.

The duty window for a local calendar date is:

    [01:30 America/New_York on that date, 03:00 America/New_York on that date)

Both endpoints are resolved through the IANA timezone database, which makes the
fall-back day behave as required:

    * 2026-11-01: 01:30 EDT (05:30Z) .. 03:00 EST (08:00Z)  -> 150 real minutes
    * 2026-03-08: 01:30 EST (06:30Z) .. 03:00 EDT (07:00Z)  ->  30 real minutes

The 01:30 endpoint is explicitly built with ``fold=0`` so the window always
starts at the *first* 01:30 on a fall-back day.  A single window object exists
per local date; the repeated 01:00-02:00 hour does not create a second opening.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

UTC = timezone.utc


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a missing offset as UTC."""

    text = (value or "").strip()
    if not text:
        raise ValueError("缺少 UTC 时刻")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("UTC 时刻格式不正确，请使用 2026-11-01T05:30:00Z") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if not (1970 <= parsed.year <= 2100):
        raise ValueError("UTC 时刻年份需在 1970..2100 之间")
    return parsed


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_utc_seconds(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return parse_utc(value)


def local_date_of(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


@dataclass(frozen=True)
class DutyWindow:
    local_date: date
    start_utc: datetime
    end_utc: datetime
    tz_name: str

    @property
    def duration_seconds(self) -> float:
        return (self.end_utc - self.start_utc).total_seconds()

    def contains(self, moment: datetime) -> bool:
        return self.start_utc <= moment < self.end_utc

    def phase(self, moment: datetime) -> str:
        if moment < self.start_utc:
            return "before"
        if moment >= self.end_utc:
            return "closed"
        return "open"

    def to_dict(self, tz: ZoneInfo) -> dict:
        return {
            "local_date": self.local_date.isoformat(),
            "timezone": self.tz_name,
            "start_utc": iso_utc(self.start_utc),
            "end_utc": iso_utc(self.end_utc),
            "start_local": self.start_utc.astimezone(tz).isoformat(),
            "end_local": self.end_utc.astimezone(tz).isoformat(),
            "duration_seconds": int(self.duration_seconds),
            "start_offset_seconds": int(self.start_utc.astimezone(tz).utcoffset().total_seconds()),
            "end_offset_seconds": int(self.end_utc.astimezone(tz).utcoffset().total_seconds()),
        }


def window_for(day: date, tz: ZoneInfo) -> DutyWindow:
    # fold=0 is the first occurrence of 01:30 on a fall-back day.  01:30 is
    # never inside the spring-forward gap, so it is a valid wall time daily.
    start = datetime(day.year, day.month, day.day, 1, 30, tzinfo=tz, fold=0)
    end = datetime(day.year, day.month, day.day, 3, 0, tzinfo=tz)
    return DutyWindow(day, start.astimezone(UTC), end.astimezone(UTC), str(tz))


class SettingsStore(Protocol):
    def get_setting(self, key: str, default: str | None = None) -> str | None: ...
    def set_setting(self, key: str, value: str) -> None: ...


class Clock:
    """Effective clock: real UTC, or a fixed UTC instant set from the drill UI."""

    MODE_KEY = "clock_mode"
    SIMULATED_KEY = "simulated_utc"

    def __init__(self, store: SettingsStore, tz: ZoneInfo):
        self.store = store
        self.tz = tz

    def real_now(self) -> datetime:
        return datetime.now(UTC)

    def mode(self) -> str:
        mode = self.store.get_setting(self.MODE_KEY, "real")
        return mode if mode in {"real", "simulated"} else "real"

    def simulated_utc(self) -> datetime | None:
        raw = self.store.get_setting(self.SIMULATED_KEY)
        if not raw:
            return None
        try:
            return parse_utc(raw)
        except ValueError:
            return None

    def now(self) -> datetime:
        if self.mode() == "simulated":
            simulated = self.simulated_utc()
            if simulated is not None:
                return simulated
        return self.real_now()

    def is_simulated(self) -> bool:
        return self.mode() == "simulated" and self.simulated_utc() is not None

    def set_simulated(self, moment: datetime) -> None:
        self.store.set_setting(self.SIMULATED_KEY, iso_utc(moment))
        self.store.set_setting(self.MODE_KEY, "simulated")

    def use_real(self) -> None:
        self.store.set_setting(self.MODE_KEY, "real")

    def snapshot(self) -> dict:
        effective = self.now()
        real = self.real_now()
        return {
            "mode": "simulated" if self.is_simulated() else "real",
            "real_utc": iso_utc(real),
            "effective_utc": iso_utc(effective),
            "effective_new_york": effective.astimezone(self.tz).isoformat(),
            "local_date": local_date_of(effective, self.tz).isoformat(),
            "simulated_utc": (
                iso_utc(self.simulated_utc()) if self.simulated_utc() else None
            ),
        }
