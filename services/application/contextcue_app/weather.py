"""Live weather from Open-Meteo (https://open-meteo.com), behind the shared
weather adapter contract (plan 9.2, LIVE_INTEGRATIONS_PLAN M3/M4).

* `search_locations`: city search through Open-Meteo geocoding.
* `OpenMeteoWeatherAdapter`: hourly forecast for the chosen location, grouped
  into 3-hour local periods with the five WeatherPeriod conditions.

No API key is needed (free for non-commercial use; attribution required:
"Weather data by Open-Meteo.com"). Network requests never run on the event
loop: `fetch()` returns the last result immediately and refreshes in a
background thread. A failed or missing forecast is reported as stale or
unavailable and is never replaced by fixture data.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .clock import iso
from .contracts import WeatherPeriod, SourceResult

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
SOURCE_ID = "open-meteo"
LABEL = "Open-Meteo forecast (weather data by Open-Meteo.com)"
REFRESH_S = 30 * 60   # well under the 10,000 calls/day free limit (48/day)
RETRY_S = 2 * 60      # after a failed refresh
VALID_FOR = timedelta(hours=3)  # a forecast older than this is stale
PERIOD_HOURS = 3
TIMEOUT_S = 10.0

Fetcher = Callable[[str, float], Any]  # (url, timeout) -> parsed JSON


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "ContextCue/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


# WMO weather interpretation codes (Open-Meteo `weather_code`) mapped to the
# five WeatherPeriod conditions. Thunderstorms count as rain.
_SEVERITY = {"sunny": 0, "cloudy": 1, "foggy": 2, "rainy": 3, "snowy": 4}


def condition_for(code: int | None) -> str | None:
    if code is None:
        return None
    if code in (0, 1):
        return "sunny"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "foggy"
    if 51 <= code <= 67 or 80 <= code <= 82 or 95 <= code <= 99:
        return "rainy"
    if 71 <= code <= 77 or code in (85, 86):
        return "snowy"
    return None


def search_locations(query: str, *, fetcher: Fetcher = _get_json, count: int = 5) -> list[dict]:
    """Places matching `query`: name, region, country, coordinates, time zone."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    url = f"{GEOCODING_URL}?{urllib.parse.urlencode({'name': query, 'count': count, 'format': 'json'})}"
    data = fetcher(url, TIMEOUT_S)
    places = []
    for r in (data or {}).get("results") or []:
        try:
            ZoneInfo(r["timezone"])
            places.append({
                "name": str(r["name"]), "region": r.get("admin1"), "country": r.get("country"),
                "latitude": float(r["latitude"]), "longitude": float(r["longitude"]),
                "time_zone": r["timezone"],
            })
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed or unknown-zone entries
    return places


def location_label(location: dict) -> str:
    parts = [location.get("name"), location.get("region")]
    return ", ".join(p for p in parts if p)


def group_periods(hourly: dict, zone: ZoneInfo, location: str) -> tuple[WeatherPeriod, ...]:
    """Hourly UTC rows -> 3-hour local periods (worst condition, highest rain
    chance, mean temperature). Hours with no usable condition are skipped."""
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    rain = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    blocks: dict[datetime, list[tuple[str, float | None, float | None]]] = {}
    for i, stamp in enumerate(times):
        start = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        local = start.astimezone(zone)
        condition = condition_for(codes[i] if i < len(codes) else None)
        if condition is None:
            continue
        block = local.replace(hour=local.hour - local.hour % PERIOD_HOURS, minute=0, second=0, microsecond=0)
        blocks.setdefault(block, []).append(
            (condition, rain[i] if i < len(rain) else None, temps[i] if i < len(temps) else None))
    periods = []
    for block, rows in sorted(blocks.items()):
        start = block.astimezone(timezone.utc)
        end = (block + timedelta(hours=PERIOD_HOURS)).astimezone(timezone.utc)
        chances = [r for _, r, _ in rows if r is not None]
        temps_c = [t for _, _, t in rows if t is not None]
        periods.append(WeatherPeriod(
            period_id=f"om-{block.strftime('%Y%m%d%H')}",
            start=iso(start), end=iso(end),
            condition=max((c for c, _, _ in rows), key=_SEVERITY.__getitem__),
            precipitation_probability=float(max(chances)) if chances else None,
            temperature_c=round(sum(temps_c) / len(temps_c), 1) if temps_c else None,
            location=location))
    return tuple(periods)


class OpenMeteoWeatherAdapter:
    """Live forecast for one location. `location` is the settings record
    (name, region, latitude, longitude, time_zone) or None."""

    kind = "weather"
    source_id = SOURCE_ID
    source_mode = "real"

    def __init__(self, location: dict | None, *, fetcher: Fetcher = _get_json,
                 monotonic: Callable[[], float] = time.monotonic, background: bool = True):
        self.location = location
        self._fetcher = fetcher
        self._mono = monotonic
        self._background = background
        self._lock = threading.Lock()
        self._periods: tuple[WeatherPeriod, ...] = ()
        self._observed: datetime | None = None
        self._error: str | None = None
        self._next_attempt = float("-inf")
        self._running = False

    def _url(self) -> str:
        loc = self.location
        query = {"latitude": f"{loc['latitude']:.4f}", "longitude": f"{loc['longitude']:.4f}",
                 "hourly": "temperature_2m,precipitation_probability,weather_code",
                 "forecast_days": 2, "timezone": "UTC"}
        return f"{FORECAST_URL}?{urllib.parse.urlencode(query)}"

    def refresh(self, now: datetime) -> None:
        """One blocking fetch; runs in a worker thread (or inline in tests)."""
        try:
            data = self._fetcher(self._url(), TIMEOUT_S)
            periods = group_periods(data["hourly"], ZoneInfo(self.location["time_zone"]),
                                    location_label(self.location))
            if not periods:
                raise ValueError("empty_forecast")
            with self._lock:
                self._periods, self._observed, self._error = periods, now, None
                self._next_attempt = self._mono() + REFRESH_S
        except Exception as exc:  # network, HTTP, malformed JSON: keep the last forecast
            reason = "malformed_forecast" if isinstance(exc, (KeyError, TypeError, ValueError)) \
                else f"network_error: {type(exc).__name__}"
            with self._lock:
                self._error = reason
                self._next_attempt = self._mono() + RETRY_S
        finally:
            with self._lock:
                self._running = False

    def _maybe_refresh(self, now: datetime) -> None:
        with self._lock:
            if self._running or self._mono() < self._next_attempt:
                return
            self._running = True
        if self._background:
            threading.Thread(target=self.refresh, args=(now,), daemon=True,
                             name="open-meteo").start()
        else:
            self.refresh(now)

    def fetch(self, now: datetime) -> SourceResult:
        received = iso(now)
        if not self.location:
            return SourceResult(SOURCE_ID, "weather", "real", "disabled", "no_location_set",
                                None, received, None, LABEL, ())
        self._maybe_refresh(now)
        with self._lock:
            periods, observed, error = self._periods, self._observed, self._error
        if observed is None:
            return SourceResult(SOURCE_ID, "weather", "real", "unavailable", error or "fetching",
                                None, received, None, LABEL, ())
        valid_until = observed + VALID_FOR
        quality = "valid" if now < valid_until else "stale"
        return SourceResult(SOURCE_ID, "weather", "real", quality,
                            error if quality == "valid" else (error or "source_expired"),
                            iso(observed), received, iso(valid_until), LABEL, periods)
