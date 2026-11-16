"""Deterministic comfort, proximity and brightness rules (plan 9.3-9.5,
baseline section 6). Times are monotonic seconds from the application clock.

Sensors report measurements only. Nothing here infers identity, occupancy,
sleep, attention or Focus.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .settings import Settings

COMFORT_PERSISTENCE_S = 60.0
COMFORT_COOLDOWN_S = 15 * 60.0
NEAR_MM = 500.0
CLEAR_MM = 650.0
PROXIMITY_PERSISTENCE_S = 0.5
# Distance errors that mean "working, nothing in range", not a fault.
NO_TARGET_ERRORS = frozenset({"distance_timeout"})
SCREEN_MIN, SCREEN_MAX, SCREEN_FALLBACK = 0.20, 1.00, 0.60
PIXELS_MIN, PIXELS_MAX, PIXELS_FALLBACK = 0.05, 0.40, 0.15

CONDITION_TEXT = {
    "hot": "sustained_above_preferred_temperature",
    "cold": "sustained_below_preferred_temperature",
    "humid": "sustained_above_preferred_humidity",
    "dry": "sustained_below_preferred_humidity",
}


@dataclass
class _Condition:
    name: str
    pending_since: float | None = None
    active: bool = False
    active_since: float | None = None
    notified: bool = False
    last_notice_at: float | None = None
    revision: int = 0


@dataclass
class ComfortStatus:
    """Derived, timestamped conditions for evidence and policy."""

    available: bool
    reason: str | None
    active: dict[str, int] = field(default_factory=dict)  # condition -> revision
    newly_eligible: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)


class ComfortMonitor:
    def __init__(self):
        self.conditions = {name: _Condition(name) for name in CONDITION_TEXT}
        self._revision_counter = 0

    def _thresholds(self, s: Settings) -> dict[str, tuple]:
        # (metric, trigger test, clear test)
        return {
            "hot": ("temperature", lambda v: v > s.comfort_temp_max_c, lambda v: v < s.comfort_temp_max_c - 0.5),
            "cold": ("temperature", lambda v: v < s.comfort_temp_min_c, lambda v: v > s.comfort_temp_min_c + 0.5),
            "humid": ("humidity", lambda v: v > s.comfort_rh_max, lambda v: v < s.comfort_rh_max - 5),
            "dry": ("humidity", lambda v: v < s.comfort_rh_min, lambda v: v > s.comfort_rh_min + 5),
        }

    def update(self, now: float, values: dict[str, float | None], settings: Settings) -> ComfortStatus:
        """`values` maps temperature/humidity to a VALID reading or None."""
        status = ComfortStatus(available=True, reason=None)
        for name, (metric, trigger, clear) in self._thresholds(settings).items():
            cond = self.conditions[name]
            value = values.get(metric)
            if value is None:
                # Bad input disables dependent behavior (APP-11) and ends the
                # condition: an active cue must not outlive its evidence.
                if cond.active:
                    status.cleared.append(name)
                    self._bump(cond)
                cond.active = False
                cond.active_since = None
                cond.pending_since = None
                cond.notified = False
                status.available = False
                status.reason = f"{metric}_unavailable"
                continue
            if cond.active:
                if clear(value):
                    cond.active = False
                    cond.active_since = None
                    cond.pending_since = None
                    cond.notified = False
                    self._bump(cond)
                    status.cleared.append(name)
                else:
                    status.active[name] = cond.revision
                continue
            if trigger(value):
                if cond.pending_since is None:
                    cond.pending_since = now
                if now - cond.pending_since >= COMFORT_PERSISTENCE_S:
                    cond.active = True
                    cond.active_since = now
                    cond.notified = False
                    self._bump(cond)
                    status.active[name] = cond.revision
            else:
                cond.pending_since = None
        for name, cond in self.conditions.items():
            if cond.active and not cond.notified:
                cooled = cond.last_notice_at is None or now - cond.last_notice_at >= COMFORT_COOLDOWN_S
                if cooled:
                    status.newly_eligible.append(name)
        return status

    def mark_notified(self, name: str, now: float) -> None:
        cond = self.conditions[name]
        cond.notified = True
        cond.last_notice_at = now

    def _bump(self, cond: _Condition) -> None:
        self._revision_counter += 1
        cond.revision = self._revision_counter


@dataclass
class ProximityStatus:
    state: str  # near | clear | unknown
    confirmed: str | None  # last confirmed near/clear
    near_transition: bool
    clear_since: float | None
    reason: str | None


class ProximityMonitor:
    """Near <=500 mm, clear >650 mm or nothing in range, 500 ms persistence.

    Only a confirmed clear -> near transition wakes the screen, so a stationary
    object (or a flickering out-of-range reading) never repeats the wake.
    Out-of-range or failed readings are unknown, not far away.
    """

    def __init__(self):
        self.confirmed: str | None = None
        self._candidate: str | None = None
        self._candidate_since: float | None = None
        self.clear_since: float | None = None

    def update(self, now: float, distance_mm: float | None, error: str | None = None) -> ProximityStatus:
        if distance_mm is None and error not in NO_TARGET_ERRORS:
            self._candidate = None
            self._candidate_since = None
            return ProximityStatus("unknown", self.confirmed, False, self.clear_since,
                                   error or "distance_unavailable")
        if distance_mm is None:
            # A working sensor with nothing in range (about 1.3 m) has nothing
            # within NEAR_MM either: the empty desk is "clear", so walking up
            # to it wakes the screen. The reading itself stays unknown.
            reading = "clear"
        elif distance_mm <= NEAR_MM:
            reading = "near"
        elif distance_mm > CLEAR_MM:
            reading = "clear"
        else:
            reading = self.confirmed or "unknown"  # hysteresis band keeps the state
        transition = False
        if reading in ("near", "clear") and reading != self.confirmed:
            if self._candidate != reading:
                self._candidate = reading
                self._candidate_since = now
            if now - self._candidate_since >= PROXIMITY_PERSISTENCE_S:
                transition = reading == "near" and self.confirmed == "clear"
                self.confirmed = reading
                self.clear_since = now if reading == "clear" else None
                self._candidate = None
                self._candidate_since = None
        elif reading == self.confirmed:
            self._candidate = None
            self._candidate_since = None
        return ProximityStatus(self.confirmed or "unknown", self.confirmed, transition,
                               self.clear_since, None)


def brightness(lux: float | None, settings: Settings) -> dict:
    """Screen 20-100% and Pixels 5-40% from ambient light; 60%/15% on failure.

    Uses a log scale between the dark and bright references. The references
    are uncalibrated defaults until the mounted calibration exists.
    """
    if lux is None:
        screen, pixels, source = SCREEN_FALLBACK, PIXELS_FALLBACK, "light_unavailable_fallback"
    else:
        dark = math.log10(settings.light_dark_lux + 1)
        bright = math.log10(settings.light_bright_lux + 1)
        ratio = (math.log10(lux + 1) - dark) / (bright - dark)
        ratio = min(1.0, max(0.0, ratio))
        screen = SCREEN_MIN + ratio * (SCREEN_MAX - SCREEN_MIN)
        pixels = PIXELS_MIN + ratio * (PIXELS_MAX - PIXELS_MIN)
        source = "ambient_light"
    if settings.brightness_override is not None:
        ratio = (settings.brightness_override - SCREEN_MIN) / (SCREEN_MAX - SCREEN_MIN)
        screen = settings.brightness_override
        pixels = PIXELS_MIN + ratio * (PIXELS_MAX - PIXELS_MIN)
        source = "manual_override"
    return {"screen": round(screen, 3), "pixels": round(pixels, 3), "source": source,
            "calibrated": False}
