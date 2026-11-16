"""Sunrise and sunset, calculated on the board (no network).

The standard sunrise equation (as used by NOAA's solar calculator): mean
solar anomaly, equation of centre, ecliptic longitude, declination and hour
angle, with the sun's centre 0.833 degrees below the horizon (refraction plus
its radius). Accurate to about a minute or two, which is plenty for choosing
the dawn, day, dusk or night sky. Works offline and in Demo Mode.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone, tzinfo

_J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)  # Julian date 2451545.0
_OBLIQUITY = math.radians(23.4397)
_HORIZON = math.radians(-0.833)


def sun_times(day: date, latitude: float, longitude: float,
              zone: tzinfo) -> tuple[datetime, datetime] | None:
    """Local sunrise and sunset on `day` (longitude east-positive), or None
    when the sun does not rise or set that day (polar day or night)."""
    n = (day - date(2000, 1, 1)).days
    j_star = n - longitude / 360.0                       # mean solar noon
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    declination = math.asin(math.sin(lam) * math.sin(_OBLIQUITY))
    phi = math.radians(latitude)
    cos_omega = ((math.sin(_HORIZON) - math.sin(phi) * math.sin(declination))
                 / (math.cos(phi) * math.cos(declination)))
    if not -1.0 <= cos_omega <= 1.0:
        return None
    half_day = math.degrees(math.acos(cos_omega)) / 360.0

    def local(j: float) -> datetime:
        return (_J2000 + timedelta(days=j)).astimezone(zone)

    return local(transit - half_day), local(transit + half_day)


def sun_view(now: datetime, location: dict | None, zone: tzinfo) -> dict | None:
    """{"sunrise": "HH:MM", "sunset": "HH:MM"} for today at the chosen
    location, in the display time zone; None without a location."""
    if not location:
        return None
    try:
        times = sun_times(now.astimezone(zone).date(), float(location["latitude"]),
                          float(location["longitude"]), zone)
    except (KeyError, TypeError, ValueError):
        return None
    if times is None:
        return None
    rise, set_ = times
    return {"sunrise": rise.strftime("%H:%M"), "sunset": set_.strftime("%H:%M")}
