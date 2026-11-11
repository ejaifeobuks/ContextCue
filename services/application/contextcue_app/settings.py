"""User settings with the baseline section 6 defaults and strict validation.

Defaults are configurable v1 choices, not measured capabilities or medical
thresholds. `light_dark_lux` / `light_bright_lux` are placeholders until the
mounted Light calibration (plan section C) supplies real references.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUDIO_POLICIES = ("visual_only", "on_request", "automatic")
UNITS = ("metric", "imperial")
SETTINGS_VERSION = 1


@dataclass(frozen=True)
class Settings:
    time_zone: str = "America/Chicago"
    units: str = "metric"
    comfort_temp_min_c: float = 20.0
    comfort_temp_max_c: float = 26.0
    comfort_rh_min: float = 30.0
    comfort_rh_max: float = 60.0
    morning_start: str = "06:00"
    morning_end: str = "11:00"
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    audio_policy: str = "visual_only"
    buzzer_enabled: bool = False
    away_audio_enabled: bool = False
    focus_p2_speech: bool = False
    speech_volume: int = 60  # percent
    long_idle_s: int = 600
    brightness_override: float | None = None  # 0.2-1.0, None = automatic
    light_dark_lux: float = 10.0
    light_bright_lux: float = 400.0
    calendar_source: str = "fixture"  # fixture | google | microsoft365
    weather_source: str = "fixture"  # fixture | open_meteo
    # Forecast location chosen by the user (never inferred): name, region,
    # country, latitude, longitude, time_zone. None until one is chosen.
    location: dict | None = None
    audio_endpoint: str | None = None
    focus: bool = False
    muted: bool = False
    version: int = SETTINGS_VERSION

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def updated(self, changes: dict[str, Any]) -> "Settings":
        """Return validated settings with `changes` applied; raise ValueError."""
        known = {f.name: f for f in fields(self)}
        unknown = set(changes) - set(known) - {"version"}
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        coerced = {}
        for name, value in changes.items():
            if name == "version":
                continue
            current = getattr(self, name)
            coerced[name] = _coerce(name, value, current)
        result = replace(self, **coerced)
        result.validate()
        return result

    def validate(self) -> None:
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone: {self.time_zone}") from exc
        if self.units not in UNITS:
            raise ValueError("units must be metric or imperial")
        if self.audio_policy not in AUDIO_POLICIES:
            raise ValueError(f"audio_policy must be one of {AUDIO_POLICIES}")
        if not -10 <= self.comfort_temp_min_c < self.comfort_temp_max_c <= 40:
            raise ValueError("Comfort temperature range must be increasing within -10..40 degC")
        if not 0 <= self.comfort_rh_min < self.comfort_rh_max <= 100:
            raise ValueError("Comfort humidity range must be increasing within 0..100 %RH")
        for name in ("morning_start", "morning_end", "quiet_start", "quiet_end"):
            parse_hhmm(getattr(self, name))
        if parse_hhmm(self.morning_start) >= parse_hhmm(self.morning_end):
            raise ValueError("Morning window must start before it ends")
        if not 0 <= self.speech_volume <= 100:
            raise ValueError("speech_volume must be 0-100")
        if not 60 <= self.long_idle_s <= 86_400:
            raise ValueError("long_idle_s must be 60-86400 seconds")
        if self.brightness_override is not None and not 0.2 <= self.brightness_override <= 1.0:
            raise ValueError("brightness_override must be 0.2-1.0 or automatic")
        if not 0 <= self.light_dark_lux < self.light_bright_lux:
            raise ValueError("Light references must satisfy dark < bright")
        if self.calendar_source not in ("fixture", "google", "microsoft365"):
            raise ValueError("Unknown calendar source")
        if self.weather_source not in ("fixture", "open_meteo"):
            raise ValueError("Unknown weather source")
        if self.location is not None:
            _check_location(self.location)


def parse_hhmm(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Expected HH:MM, got {value!r}") from exc


def in_window(local: time, start: time, end: time) -> bool:
    """True when `local` lies in [start, end); handles windows across midnight."""
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def _check_location(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("location must be an object")
    name = value.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= 80:
        raise ValueError("location name must be 1-80 characters")
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not low <= number <= high:
            raise ValueError(f"location {key} must be a number in {low}..{high}")
    try:
        ZoneInfo(value.get("time_zone"))
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("location time_zone is not a known time zone") from exc
    for key in ("region", "country"):
        if value.get(key) is not None and (not isinstance(value[key], str) or len(value[key]) > 80):
            raise ValueError(f"location {key} must be text")
    return {"name": name, "region": value.get("region"), "country": value.get("country"),
            "latitude": float(value["latitude"]), "longitude": float(value["longitude"]),
            "time_zone": value["time_zone"]}


def _coerce(name: str, value: Any, current: Any) -> Any:
    if name == "location":
        return None if value is None else _check_location(value)
    if name in ("brightness_override", "audio_endpoint") and value in (None, "", "auto"):
        return None
    if isinstance(current, bool) or name in ("buzzer_enabled", "away_audio_enabled", "focus_p2_speech", "focus", "muted"):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if isinstance(current, int) and not isinstance(current, bool) and name != "brightness_override":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError(f"{name} must be a whole number")
        return int(value)
    if isinstance(current, float) or name == "brightness_override":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value
