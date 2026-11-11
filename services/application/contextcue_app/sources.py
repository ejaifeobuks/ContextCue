"""Calendar and weather adapters behind one contract (plan 9.2).

Fixture adapters are the only sources for the demonstration: deterministic,
fictional and labeled `simulated`. Live adapters (Google, Microsoft 365,
Open-Meteo) report `disabled` until they are implemented and connected; they
never fall back to fixture data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .clock import iso, parse_iso
from .contracts import SIMULATED, CalendarEvent, SourceResult, WeatherPeriod

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


class SourceAdapter(Protocol):
    source_id: str
    kind: str

    def fetch(self, now: datetime) -> SourceResult: ...


@dataclass
class Scenario:
    scenario_id: str
    title: str
    description: str
    time_zone: str
    start: datetime
    calendar: dict[str, Any]
    weather: dict[str, Any]
    sensors: dict[str, Any]


def load_scenarios(path: Path | None = None) -> dict[str, Scenario]:
    raw = json.loads((path or FIXTURE_DIR / "demo_scenarios.json").read_text(encoding="utf-8"))
    if raw.get("schema_version") != "1.0":
        raise ValueError("Unsupported fixture schema version")
    shared_calendar = raw["calendar"]
    shared_weather = raw["weather"]
    scenarios = {}
    for item in raw["scenarios"]:
        scenarios[item["scenario_id"]] = Scenario(
            scenario_id=item["scenario_id"],
            title=item["title"],
            description=item["description"],
            time_zone=raw["time_zone"],
            start=parse_iso(item["start"]),
            calendar=item.get("calendar", shared_calendar),
            weather=item.get("weather", shared_weather),
            sensors=item.get("sensors", {}),
        )
    return scenarios


_TIME_KEYS = ("start", "end", "observed_at", "valid_until")


def _shift(value: Any, delta: timedelta, days: int) -> Any:
    """Move UTC ISO timestamps by `delta` and local YYYY-MM-DD dates by `days`."""
    if not isinstance(value, str):
        return value
    if len(value) == 10:
        return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
    return iso(parse_iso(value) + delta)


def shift_fixture(data: Any, delta: timedelta, days: int) -> Any:
    if not delta and not days:
        return data
    if isinstance(data, dict):
        return {k: (_shift(v, delta, days) if k in _TIME_KEYS else shift_fixture(v, delta, days))
                for k, v in data.items()}
    if isinstance(data, list):
        return [shift_fixture(v, delta, days) for v in data]
    return data


Shift = tuple[timedelta, int]  # (offset for timestamps, whole days for all-day dates)


class _FixtureBase:
    """Fictional fixture source. With `shift` (from `demo_shift`) the scenario
    is replayed from the moment it was chosen: the scenario's start time maps
    to "now" on the device clock, so the demo day always has events ahead.
    Without it the data keeps its dated scenario times (scenario clock)."""

    source_mode = SIMULATED

    def __init__(self, data: dict[str, Any], shift: Shift | None = None):
        self.raw = data
        self.shifted = shift is not None
        self.data = shift_fixture(data, *shift) if shift else data
        self.source_id = data["source_id"]
        self.failure: str | None = None  # injected by the simulator only

    def _result(self, now: datetime, items: tuple, label: str) -> SourceResult:
        received = iso(now)
        if self.failure:
            return SourceResult(self.source_id, self.kind, SIMULATED, "unavailable",
                                self.failure, None, received, None, label, ())
        observed = self.data["observed_at"]
        valid_until = self.data["valid_until"]
        if self.shifted and parse_iso(observed) > now:
            observed = iso(now)  # replayed from now: "observed" cannot be in the future
        if parse_iso(observed) > now:
            return SourceResult(self.source_id, self.kind, SIMULATED, "invalid",
                                "observed_in_future", observed, received, valid_until, label, ())
        quality = "valid" if now < parse_iso(valid_until) else "stale"
        return SourceResult(self.source_id, self.kind, SIMULATED, quality,
                            None if quality == "valid" else "source_expired",
                            observed, received, valid_until, label, items)


class FixtureCalendarAdapter(_FixtureBase):
    kind = "calendar"

    def fetch(self, now: datetime) -> SourceResult:
        events = tuple(CalendarEvent(**event) for event in self.data["events"])
        return self._result(now, events, self.data.get("label", "Fictional demo calendar"))


class FixtureWeatherAdapter(_FixtureBase):
    kind = "weather"

    def fetch(self, now: datetime) -> SourceResult:
        location = self.data["location"]
        periods = tuple(WeatherPeriod(location=location, **p) for p in self.data["periods"])
        return self._result(now, periods, self.data.get("label", "Fictional demo forecast"))


class UnconnectedLiveAdapter:
    """Placeholder for Google Calendar, Microsoft 365 and Open-Meteo.

    Reports `disabled` so the UI can show the choice honestly; it never returns
    fixture data in place of a live source.
    """

    source_mode = "real"

    def __init__(self, source_id: str, kind: str, label: str):
        self.source_id = source_id
        self.kind = kind
        self.label = label

    def fetch(self, now: datetime) -> SourceResult:
        return SourceResult(self.source_id, self.kind, "real", "disabled",
                            "live_adapter_not_connected", None, iso(now), None, self.label, ())


def demo_shift(scenario: Scenario, now: datetime) -> Shift:
    """Replay `scenario` from `now`: its start time maps to the current minute.
    All-day dates move by the matching number of local days."""
    zone = ZoneInfo(scenario.time_zone)
    delta = now.replace(second=0, microsecond=0) - scenario.start
    days = ((scenario.start + delta).astimezone(zone).date() - scenario.start.astimezone(zone).date()).days
    return delta, days


def make_calendar_adapter(setting: str, scenario: Scenario, *, shift: Shift | None = None):
    if setting == "fixture":
        return FixtureCalendarAdapter(scenario.calendar, shift)
    names = {"google": "Google Calendar", "microsoft365": "Microsoft 365 Calendar"}
    return UnconnectedLiveAdapter(f"live-{setting}", "calendar", names.get(setting, setting))


def make_weather_adapter(setting: str, scenario: Scenario, *, shift: Shift | None = None,
                         location: dict | None = None):
    if setting == "fixture":
        return FixtureWeatherAdapter(scenario.weather, shift)
    from .weather import OpenMeteoWeatherAdapter
    return OpenMeteoWeatherAdapter(location)
