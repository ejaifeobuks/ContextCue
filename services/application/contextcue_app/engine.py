"""ContextEngine: evidence store, freshness, schedule facts, trigger rules and
immutable snapshots (plan 9.1; UML class 2).

Evidence revisions change only on material change (quality, a meaningful value
step, a new derived condition), so incidental new sensor samples never keep
cancelling generation. Freshness comes from each source's own timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from contextcue_contracts import GatewayState

from .clock import iso, parse_iso
from .contracts import (COMFORT, CONTEXT_ANSWER, MORNING, PREPARATION, REAL, ContextEvent,
                        ContextSnapshot, Notice, SourceResult)
from .recommendation import GENERATION_TIMEOUT_S, SUPPORTED_QUESTIONS
from .rules import CONDITION_TEXT, ComfortMonitor, ComfortStatus, ProximityMonitor, ProximityStatus
from .schedule import ScheduleFacts, calculate_schedule
from .settings import Settings, in_window, parse_hhmm

STALE_MS = {"temperature": 5000, "humidity": 5000, "ambient_light": 2000, "distance": 2000}
MATERIAL_STEP = {"temperature": 0.5, "humidity": 3.0, "distance": 100.0}
RAIN_THRESHOLD = 50.0

QUESTION_EVIDENCE = {
    "next_event": ["calendar.schedule"],
    "rain_today": ["weather.now", "weather.later"],
    "room_comfort": ["sensor.temperature", "sensor.humidity"],
    "schedule_today": ["calendar.schedule"],
}


@dataclass
class TriggerSpec:
    category: str
    dedupe_key: str
    priority: int
    evidence: list[str]
    required: list[str]
    reason: str
    facts: dict[str, Any] = field(default_factory=dict)
    latest_expiry: datetime | None = None
    explicit: bool = False
    question: str | None = None
    condition: str | None = None


class ContextEngine:
    def __init__(self, *, now: Callable[[], datetime], monotonic: Callable[[], float],
                 settings: Callable[[], Settings]):
        self._now = now
        self._mono = monotonic
        self._settings = settings
        self.evidence: dict[str, ContextEvent] = {}
        self._material: dict[str, Any] = {}
        self._revisions: dict[str, int] = {}
        self.comfort = ComfortMonitor()
        self.proximity = ProximityMonitor()
        self.comfort_status = ComfortStatus(available=False, reason="no_sample_yet")
        self.proximity_status = ProximityStatus("unknown", None, False, None, "no_sample_yet")
        self.schedule: ScheduleFacts | None = None
        self.sources: dict[str, SourceResult] = {}
        self.sensor_view: dict[str, dict] = {}
        self.health: dict[str, dict] = {}
        self.boot_id: str | None = None
        self.morning_presented: set[str] = set()
        self.near_transition_pending = False
        self._seen_seq: dict[str, tuple] = {}

    # ------------------------------------------------------------ evidence
    def _put(self, evidence_id: str, *, material: Any, **fields_: Any) -> ContextEvent:
        if self._material.get(evidence_id, object()) != material or evidence_id not in self.evidence:
            self._revisions[evidence_id] = self._revisions.get(evidence_id, 0) + 1
            self._material[evidence_id] = material
        event = ContextEvent(evidence_id=evidence_id, revision=self._revisions[evidence_id], **fields_)
        self.evidence[evidence_id] = event
        return event

    def _drop(self, evidence_id: str) -> None:
        if evidence_id in self.evidence:
            del self.evidence[evidence_id]
            self._material.pop(evidence_id, None)
            self._revisions[evidence_id] = self._revisions.get(evidence_id, 0) + 1

    def current_revisions(self) -> dict[str, int]:
        return {k: v.revision for k, v in self.evidence.items()}

    # ------------------------------------------------------------- sensors
    def process_sensor_readings(self, state: GatewayState) -> None:
        settings = self._settings()
        mono = self._mono()
        self.boot_id = state.boot_id
        self.health = {h.component_id: {"state": h.state, "reason": h.reason,
                                        "last_success_at": h.last_success_at,
                                        "execution_location": h.execution_location}
                       for h in state.health}
        values: dict[str, float | None] = {}
        errors: dict[str, str | None] = {}
        for sample in state.samples:
            quality = sample.quality.value if hasattr(sample.quality, "value") else str(sample.quality)
            metric = sample.metric
            valid = quality == "valid" and sample.value is not None
            values[metric] = sample.value if valid else None
            errors[metric] = sample.error
            valid_until = None
            if sample.observed_at and valid:
                valid_until = iso(parse_iso(sample.observed_at) + timedelta(milliseconds=STALE_MS.get(metric, 2000)))
            step = MATERIAL_STEP.get(metric)
            if not valid:
                material = (quality, sample.error)
            elif metric == "ambient_light":
                material = (quality, round(_log_bucket(sample.value)))
            else:
                material = (quality, round(sample.value / step) if step else sample.value)
            self._put(f"sensor.{metric}", material=material, source_id=f"{sample.module_id}",
                      source_type="sensor", metric=metric, value=sample.value if valid else None,
                      unit=sample.unit, observed_at=sample.observed_at, received_at=sample.received_at,
                      valid_until=valid_until, freshness_ms=STALE_MS.get(metric),
                      source_mode=sample.source_mode, quality=quality,
                      provenance=f"gateway:{sample.event_id}",
                      label="Indoor sensor" + (" (simulated)" if sample.source_mode != REAL else ""))
            self.sensor_view[metric] = {
                "value": sample.value if valid else None, "unit": sample.unit, "quality": quality,
                "error": sample.error, "observed_at": sample.observed_at,
                "source_mode": sample.source_mode, "module_id": sample.module_id,
            }
        for metric in ("temperature", "humidity", "ambient_light", "distance"):
            if metric not in values:
                values[metric] = None
                self.sensor_view.setdefault(metric, {"value": None, "unit": None,
                                                     "quality": "unavailable",
                                                     "error": "no_sample", "source_mode": None})

        # Comfort: derived, timestamped condition evidence.
        self.comfort_status = self.comfort.update(
            mono, {"temperature": values.get("temperature"), "humidity": values.get("humidity")},
            settings)
        for name, text in CONDITION_TEXT.items():
            eid = f"derived.comfort.{name}"
            if name in self.comfort_status.active:
                cond = self.comfort.conditions[name]
                metric = "temperature" if name in ("hot", "cold") else "humidity"
                source = self.evidence.get(f"sensor.{metric}")
                self._put(eid, material=cond.revision, source_id="comfort_rule",
                          source_type="derived", metric=f"comfort_{name}", value=text, unit=None,
                          observed_at=source.observed_at if source else None,
                          received_at=iso(self._now()), valid_until=source.valid_until if source else None,
                          freshness_ms=STALE_MS[metric],
                          source_mode=source.source_mode if source else REAL, quality="valid",
                          provenance="rules.ComfortMonitor (60 s persistence, hysteresis)",
                          label="Derived comfort condition")
            else:
                self._drop(eid)

        # Proximity: only a sample with a new sequence counts toward persistence.
        dist = next((s for s in state.samples if s.metric == "distance"), None)
        signature = (dist.boot_id, dist.seq, dist.quality) if dist else None
        if signature != self._seen_seq.get("distance"):
            self._seen_seq["distance"] = signature
            self.proximity_status = self.proximity.update(
                mono, values.get("distance"), errors.get("distance"))
            if self.proximity_status.near_transition:
                self.near_transition_pending = True
        elif values.get("distance") is None:
            self.proximity_status = self.proximity.update(mono, None, errors.get("distance"))

        # Preferences are evidence too, so a comfort cue can cite them.
        for metric, low, high, unit in (
                ("temperature", settings.comfort_temp_min_c, settings.comfort_temp_max_c, "degC"),
                ("humidity", settings.comfort_rh_min, settings.comfort_rh_max, "%RH")):
            self._put(f"pref.{metric}_range", material=(low, high), source_id="settings",
                      source_type="preference", metric=f"preferred_{metric}_range",
                      value=[low, high], unit=unit, observed_at=None, received_at=None,
                      valid_until=None, freshness_ms=None, source_mode=REAL, quality="valid",
                      provenance="user settings", label="Your preference")

    # ---------------------------------------------------- calendar/weather
    def load_calendar_weather(self, calendar, weather) -> None:
        now = self._now()
        settings = self._settings()
        cal = calendar.fetch(now)
        wx = weather.fetch(now)
        self.sources = {"calendar": cal, "weather": wx}
        if cal.quality == "valid":
            self.schedule = calculate_schedule(cal.items, now, settings.zone)
            sched = self.schedule.to_dict(settings.zone)
            material = {k: v for k, v in sched.items() if k != "next_event_in_ms"}
        else:
            self.schedule = None
            sched, material = None, (cal.quality, cal.error)
        self._put("calendar.schedule", material=material, source_id=cal.source_id,
                  source_type="calendar", metric="schedule_today", value=sched, unit=None,
                  observed_at=cal.observed_at, received_at=cal.received_at,
                  valid_until=cal.valid_until, freshness_ms=None, source_mode=cal.source_mode,
                  quality=cal.quality, provenance=f"{cal.label} via {type(calendar).__name__}",
                  label=cal.label)
        # One evidence item per upcoming event, so a preparation cue depends
        # only on its own event, not on the whole day changing.
        live_ids = set()
        if self.schedule is not None:
            for event in ([self.schedule.next_event] if self.schedule.next_event else []):
                eid = f"calendar.event.{event.event_id}"
                live_ids.add(eid)
                value = {"event_id": event.event_id, "title": event.title,
                         "start": iso(event.start), "end": iso(event.end),
                         "start_local": event.start.astimezone(settings.zone).strftime("%H:%M"),
                         "location": event.location}
                self._put(eid, material=value, source_id=cal.source_id, source_type="calendar",
                          metric="event", value=value, unit=None, observed_at=cal.observed_at,
                          received_at=cal.received_at, valid_until=min(cal.valid_until, iso(event.start)),
                          freshness_ms=None, source_mode=cal.source_mode, quality="valid",
                          provenance=cal.label, label=cal.label)
        for eid in [k for k in self.evidence if k.startswith("calendar.event.") and k not in live_ids]:
            self._drop(eid)

        if wx.quality == "valid":
            current = _period_at(wx.items, now) or _next_period(wx.items, now)
        else:
            current = None
        value = _period_value(current, now, settings.zone) if current else None
        self._put("weather.now", material=(wx.quality, value), source_id=wx.source_id,
                  source_type="weather", metric="forecast_next", value=value, unit=None,
                  observed_at=wx.observed_at, received_at=wx.received_at,
                  valid_until=wx.valid_until, freshness_ms=None, source_mode=wx.source_mode,
                  quality=wx.quality if value or wx.quality != "valid" else "unavailable",
                  provenance=f"{wx.label} via {type(weather).__name__}", label=wx.label)
        event_weather = None
        if self.schedule and self.schedule.next_event and wx.quality == "valid":
            event_weather = _period_at(wx.items, self.schedule.next_event.start)
        if event_weather:
            value = _period_value(event_weather, now, settings.zone)
            self._put("weather.event", material=value, source_id=wx.source_id,
                      source_type="weather", metric="forecast_event", value=value, unit=None,
                      observed_at=wx.observed_at, received_at=wx.received_at,
                      valid_until=wx.valid_until, freshness_ms=None, source_mode=wx.source_mode,
                      quality="valid", provenance=wx.label, label=wx.label)
        else:
            self._drop("weather.event")

        # The rest of today: the rainiest period still to come, so a morning
        # briefing or "Will it rain today?" can warn about rain later on.
        later = _later_today(wx.items, current, now, settings.zone) if wx.quality == "valid" else None
        if later:
            value = {**_period_value(later, now, settings.zone),
                     "start_local": parse_iso(later.start).astimezone(settings.zone).strftime("%H:%M"),
                     "end_local": parse_iso(later.end).astimezone(settings.zone).strftime("%H:%M")}
            self._put("weather.later", material=value, source_id=wx.source_id,
                      source_type="weather", metric="forecast_later", value=value, unit=None,
                      observed_at=wx.observed_at, received_at=wx.received_at,
                      valid_until=wx.valid_until, freshness_ms=None, source_mode=wx.source_mode,
                      quality="valid", provenance=wx.label, label=wx.label)
        else:
            self._drop("weather.later")

    # ------------------------------------------------------------ triggers
    def evaluate_triggers(self) -> list[TriggerSpec]:
        settings = self._settings()
        now = self._now()
        local = now.astimezone(settings.zone)
        specs: list[TriggerSpec] = []
        calendar_ok = self.evidence.get("calendar.schedule") is not None and \
            self.evidence["calendar.schedule"].quality == "valid"

        # Morning: approach in the window requests the unread briefing, at most
        # one automatic presentation per local date.
        near = self.near_transition_pending
        self.near_transition_pending = False
        date = local.date().isoformat()
        if (near and calendar_ok and date not in self.morning_presented
                and in_window(local.time(), parse_hhmm(settings.morning_start), parse_hhmm(settings.morning_end))):
            specs.append(self._morning_spec(date, explicit=False, reason="approach_in_morning_window"))

        # Preparation: next valid event within 30 min; never invent travel time.
        if calendar_ok and self.schedule and self.schedule.next_event and \
                self.schedule.urgency in ("soon", "imminent"):
            event = self.schedule.next_event
            ids = ["calendar.schedule", f"calendar.event.{event.event_id}"]
            wx = self.evidence.get("weather.event")
            rain = None
            if wx and wx.quality == "valid" and (wx.value.get("precipitation_probability") or 0) >= RAIN_THRESHOLD:
                ids.append("weather.event")
                rain = wx.value["precipitation_probability"]
            specs.append(TriggerSpec(
                category=PREPARATION, dedupe_key=f"prep:{event.event_id}:{iso(event.start)}",
                priority=2 if self.schedule.urgency == "imminent" else 1,
                evidence=ids, required=[f"calendar.event.{event.event_id}"],
                reason=f"event_within_{'10' if self.schedule.urgency == 'imminent' else '30'}_min",
                facts={"event": self.evidence[f"calendar.event.{event.event_id}"].value,
                       "event_in_ms": self.schedule.next_event_in_ms,
                       "rain_probability": rain},
                latest_expiry=event.start))

        # Comfort: sustained condition after persistence, per-condition cooldown.
        for name in self.comfort_status.newly_eligible:
            metric = "temperature" if name in ("hot", "cold") else "humidity"
            cond = self.comfort.conditions[name]
            specs.append(TriggerSpec(
                category=COMFORT, dedupe_key=f"comfort:{name}:{cond.revision}", priority=0,
                evidence=[f"sensor.{metric}", f"derived.comfort.{name}", f"pref.{metric}_range"],
                required=[f"derived.comfort.{name}"], reason=CONDITION_TEXT[name],
                facts={"condition": name}, condition=name))
        return specs

    def _morning_spec(self, date: str, *, explicit: bool, reason: str, n: int | str = 0) -> TriggerSpec:
        ids = ["calendar.schedule", "weather.now", "weather.later", "sensor.temperature", "sensor.humidity"]
        return TriggerSpec(
            category=MORNING,
            dedupe_key=f"morning:{date}" if not explicit else f"briefing:{date}:{n}",
            priority=2 if explicit else 0, evidence=ids, required=["calendar.schedule"],
            reason=reason, explicit=explicit)

    def briefing_request(self, n: int | str) -> TriggerSpec:
        local = self._now().astimezone(self._settings().zone)
        return self._morning_spec(local.date().isoformat(), explicit=True,
                                  reason="explicit_request", n=n)

    def question_request(self, question: str, n: int | str) -> TriggerSpec:
        if question not in SUPPORTED_QUESTIONS:
            raise ValueError("unsupported_question")
        ids = QUESTION_EVIDENCE[question]
        return TriggerSpec(category=CONTEXT_ANSWER, dedupe_key=f"answer:{question}:{n}", priority=2,
                           evidence=list(ids), required=[], reason="explicit_question",
                           explicit=True, question=question)

    def mark_presented(self, spec: TriggerSpec) -> None:
        if spec.category == MORNING and not spec.explicit:
            self.morning_presented.add(spec.dedupe_key.split(":", 1)[1])
        if spec.condition:
            self.comfort.mark_notified(spec.condition, self._mono())

    # ------------------------------------------------------------ snapshot
    def build_snapshot(self, spec: TriggerSpec, notice_id: str) -> ContextSnapshot:
        settings = self._settings()
        now = self._now()
        local = now.astimezone(settings.zone)
        evidence = tuple(self.evidence[e] for e in spec.evidence if e in self.evidence)
        facts = dict(spec.facts)
        facts.update({"local_time": local.strftime("%H:%M"), "local_date": local.date().isoformat()})
        # Code does the arithmetic (baseline): a countdown the model may quote,
        # rounded up like the UI's "In N min", at the time the snapshot is built.
        event = facts.get("event")
        if isinstance(event, dict) and event.get("start"):
            seconds = (parse_iso(event["start"]) - now).total_seconds()
            facts["minutes_until_event"] = max(0, -(-int(seconds) // 60))
        tips = advice(spec.category, evidence, facts.get("condition"), spec.question)
        if tips:
            facts["advice"] = tips
        return ContextSnapshot(
            request_id=f"req-{uuid4().hex[:10]}", snapshot_id=f"{notice_id}:{uuid4().hex[:6]}",
            revision=sum(e.revision for e in evidence), category=spec.category,
            reason=spec.reason, evidence=evidence, facts=facts,
            preferences={"units": settings.units, "time_zone": settings.time_zone},
            mode="focus" if settings.focus else "normal", audio_policy=settings.audio_policy,
            created_at=iso(now), deadline=iso(now + timedelta(seconds=GENERATION_TIMEOUT_S)),
            question=spec.question)

    def evidence_problem(self, notice: Notice) -> str | None:
        """Why a notice's required evidence no longer supports it, if it doesn't."""
        for eid in notice.required:
            event = self.evidence.get(eid)
            if event is None:
                return f"{eid}: no_longer_present"
            if event.quality != "valid":
                return f"{eid}: {event.quality}"
            if eid in notice.evidence and event.revision != notice.evidence[eid]:
                return f"{eid}: changed"
            # Sensor freshness is the gateway's quality (device clock); only
            # calendar/weather validity is compared with the scenario clock.
            if (event.source_type in ("calendar", "weather") and event.valid_until
                    and parse_iso(event.valid_until) <= self._now()):
                return f"{eid}: validity_ended"
        return None

    def obsolete_reason(self, snapshot: ContextSnapshot, evidence_ids) -> str | None:
        """Reject a result whose referenced evidence changed or went bad."""
        revisions = snapshot.evidence_revisions()
        for eid in evidence_ids:
            event = self.evidence.get(eid)
            if event is None or event.quality != "valid":
                return f"obsolete:{eid}_not_valid"
            # Sensor revisions move only on material change, so a fresh sample
            # with the same meaning does not make the result obsolete.
            if event.revision != revisions.get(eid):
                return f"obsolete:{eid}_changed"
        return None


def _log_bucket(lux: float) -> float:
    import math
    return math.log(lux + 1, 1.25)


# Advice is decided by code from the evidence, like every other fact; the model
# only words it. Thresholds: rain at RAIN_THRESHOLD (the preparation rule),
# cold at 5 degC.
COLD_OUTDOOR_C = 5.0
INDOOR_TIPS = {"hot": "Open a window or use a fan", "cold": "Add a layer or warm the room",
               "humid": "Air out the room", "dry": "Drink some water"}


def advice(category: str, evidence, condition: str | None = None,
           question: str | None = None) -> list[str]:
    """Short, number-free suggestions supported by the snapshot's evidence."""
    if category == COMFORT:
        return [INDOOR_TIPS[condition]] if condition in INDOOR_TIPS else []
    if category == CONTEXT_ANSWER and question != "rain_today":
        return []
    forecasts = [e.value for e in evidence
                 if e.metric.startswith("forecast") and e.quality == "valid" and isinstance(e.value, dict)]
    tips = []
    if any((f.get("precipitation_probability") or 0) >= RAIN_THRESHOLD for f in forecasts):
        tips.append("Bring an umbrella")
    if any(f.get("condition") == "snowy" for f in forecasts):
        tips.append("Dress for snow")
    elif any(f.get("temperature_c") is not None and f["temperature_c"] <= COLD_OUTDOOR_C for f in forecasts):
        tips.append("Wear a warm coat")
    return tips


def _later_today(periods, current, now: datetime, zone):
    """The rainiest forecast period after the current one that starts today."""
    today = now.astimezone(zone).date()
    after = parse_iso(current.end) if current else now
    later = [p for p in periods if parse_iso(p.start) >= after
             and parse_iso(p.start).astimezone(zone).date() == today]
    if not later:
        return None
    return max(later, key=lambda p: (p.precipitation_probability or 0, -parse_iso(p.start).timestamp()))


def _period_at(periods, when: datetime):
    for p in periods:
        if parse_iso(p.start) <= when < parse_iso(p.end):
            return p
    return None


def _next_period(periods, when: datetime):
    upcoming = sorted((p for p in periods if parse_iso(p.start) > when), key=lambda p: p.start)
    return upcoming[0] if upcoming else None


def _is_night(hour: int) -> bool:
    """Same rule as the UI (ui-logic isNightHour): 18:00 to 05:59 is night."""
    return hour >= 18 or hour < 6


def _period_value(p, now: datetime, zone) -> dict:
    """A forecast period as evidence. `night` is set when the period (from now,
    if it has already begun) is at night, so wording can say "clear", not
    "sunny", for a clear night."""
    start = max(parse_iso(p.start), now).astimezone(zone)
    return {"period_id": p.period_id, "start": p.start, "end": p.end, "condition": p.condition,
            "precipitation_probability": p.precipitation_probability,
            "temperature_c": p.temperature_c, "location": p.location,
            "night": _is_night(start.hour)}
