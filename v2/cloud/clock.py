"""IST-aware clock used everywhere "current FY" or "now" matters (S1 spec §6.1, D2, A13).

FY = 1 April to 31 March, decided by the **IST** calendar date (Q19/A13's "current FY" is IST, not UTC — a
heartbeat at 2026-03-31 20:00 UTC is already 1 April in IST).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, dt: datetime):
        self._dt = dt

    def now(self) -> datetime:
        return self._dt

    def advance(self, **delta) -> None:
        self._dt += timedelta(**delta)


def ist_date(dt: datetime) -> date:
    return dt.astimezone(IST).date()


def fy_start_of(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def fy_end_of(d: date) -> date:
    start = fy_start_of(d)
    return date(start.year + 1, 3, 31)


def current_fy_start(clock: Clock) -> date:
    return fy_start_of(ist_date(clock.now()))
