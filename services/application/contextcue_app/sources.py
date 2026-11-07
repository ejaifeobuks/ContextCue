"""Calendar and weather adapters behind one contract (plan 9.2).

Fixture adapters are the only sources for the demonstration: deterministic,
fictional and labeled `simulated`. Live adapters (Google, Microsoft 365,
Open-Meteo) report `disabled` until they are implemented and connected; they
never fall back to fixture data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

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


class _FixtureBase:
    source_mode = SIMULATED

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.source_id = data["source_id"]
        self.failure: str | None = None  # injected by the simulator only

    def _result(self, now: datetime, items: tuple, label: str) -> SourceResult:
        received = iso(now)
        if self.failure:
            return SourceResult(self.source_id, self.kind, SIMULATED, "unavailable",
                                self.failure, None, received, None, label, ())
        observed = self.data["observed_at"]
        valid_until = self.data["valid_until"]
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


def make_calendar_adapter(setting: str, scenario: Scenario):
    if setting == "fixture":
        return FixtureCalendarAdapter(scenario.calendar)
    names = {"google": "Google Calendar", "microsoft365": "Microsoft 365 Calendar"}
    return UnconnectedLiveAdapter(f"live-{setting}", "calendar", names.get(setting, setting))


def make_weather_adapter(setting: str, scenario: Scenario):
    if setting == "fixture":
        return FixtureWeatherAdapter(scenario.weather)
    return UnconnectedLiveAdapter("live-open-meteo", "weather", "Open-Meteo forecast")
