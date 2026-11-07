"""Application clocks.

`SystemClock` is the real device clock. `ScenarioClock` is the simulator's
virtual clock (baseline section 6): it starts at a dated scenario time, runs at
real speed, and can be jumped forward. Its time is always labeled as scenario
time and must never be shown as the real wall clock.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class AppClock(Protocol):
    #: "real" for the device clock, "simulated" for a scenario clock.
    mode: str

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    mode = "real"

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class ScenarioClock:
    """Virtual clock: `start` plus elapsed real time plus explicit jumps.

    `monotonic()` advances with `advance()` as well, so persistence, cooldown and
    expiry rules see a jump exactly as they would see real time passing.
    """

    mode = "simulated"

    def __init__(self, start: datetime, *, base: "AppClock | None" = None, frozen: bool = False):
        if start.tzinfo is None:
            raise ValueError("Scenario start must be timezone-aware")
        self._start = start.astimezone(timezone.utc)
        self._base = base or SystemClock()
        self._frozen = frozen
        self._origin = self._base.monotonic()
        self._offset = 0.0

    def _elapsed(self) -> float:
        running = 0.0 if self._frozen else self._base.monotonic() - self._origin
        return running + self._offset

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed())

    def monotonic(self) -> float:
        return self._origin + self._elapsed()

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("A scenario clock only moves forward")
        self._offset += seconds

    def reset(self, start: datetime) -> None:
        self._start = start.astimezone(timezone.utc)
        self._origin = self._base.monotonic()
        self._offset = 0.0


def iso(value: datetime) -> str:
    """UTC ISO 8601 with milliseconds and a Z suffix (baseline section 5)."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp without a zone: {value}")
    return parsed.astimezone(timezone.utc)
