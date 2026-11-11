"""ContextCueApp: the single application backend (baseline section 1).

Wires ContextEngine, RecommendationService, NotificationService and
DataRepository to one gateway, runs the control loop, and exposes a view model
and actions for the UI. All state changes happen on one asyncio loop; the UI
server calls in with `run_coroutine_threadsafe`.
"""

from __future__ import annotations

import asyncio
import itertools
from uuid import uuid4
import logging
from datetime import timedelta
from typing import Any

from contextcue_contracts import GatewayState, HealthState

from .clock import AppClock, ScenarioClock, iso, parse_iso
from .contracts import CONTEXT_ANSWER, Lifecycle, Notice
from .engine import ContextEngine, TriggerSpec
from .notifications import NotificationService, SpeechPort
from .recommendation import (SUPPORTED_QUESTIONS, GenerationResult, ModelBackend,
                             RecommendationService, factual_fallback)
from .repository import DataRepository
from .settings import Settings
from .sources import Scenario, demo_shift, load_scenarios, make_calendar_adapter, make_weather_adapter

log = logging.getLogger("contextcue.app")

POLL_S = 0.1
SLOW_TICK_S = 1.0
# The MCU output lease lasts 3 s; renewing once a second is enough (as
# HardwareGateway.heartbeat documents). The slow tick runs on every poll while
# an object is near, which would otherwise renew it 10 times a second.
HEARTBEAT_S = 1.0
WAKE_TIMEOUT_S = 120.0
EVIDENCE_SAVE_S = 30.0
RETENTION_S = 3600.0
SENSOR_MODULES = ("thermo", "light", "distance")


class ActionError(ValueError):
    pass


class ContextCueApp:
    def __init__(self, *, gateway, clock: AppClock, repository: DataRepository,
                 gateway_mode: str, scenario_id: str = "morning",
                 model: ModelBackend | None = None, speech: SpeechPort | None = None,
                 scenarios: dict[str, Scenario] | None = None):
        self.gateway = gateway
        self.clock = clock
        self.repo = repository
        self.gateway_mode = gateway_mode  # hardware | simulated
        self.scenarios = scenarios or load_scenarios()
        if scenario_id not in self.scenarios:
            raise ValueError(f"Unknown scenario {scenario_id}")
        self.scenario = self.scenarios[scenario_id]
        self.settings, problem = repository.restore_settings()
        self.engine = ContextEngine(now=clock.now, monotonic=clock.monotonic,
                                    settings=lambda: self.settings)
        self.notifications = NotificationService(
            now=clock.now, monotonic=clock.monotonic, settings=lambda: self.settings,
            repository=repository, speech=speech, log=self._diag)
        self.notifications.muted = self.settings.muted
        self.notifications.focus = self.settings.focus
        self.recommender = RecommendationService(model, now=clock.now)
        self.recommender.on_result(self._on_generation)
        self._pending: dict[str, tuple[str, TriggerSpec]] = {}  # request -> notice, spec
        self._specs: dict[str, TriggerSpec] = {}  # notice -> spec
        self._counter = itertools.count(1)

        # On the device clock the chosen scenario is replayed from now (its
        # start time maps to the current minute); a scenario clock keeps the
        # dated demo times. The shift is fixed until another scenario is chosen,
        # so switching a source does not restart the demo.
        self._replay = not isinstance(clock, ScenarioClock)
        self._shift = demo_shift(self.scenario, clock.now()) if self._replay else None
        self._make_sources()
        self._task: asyncio.Task | None = None
        self._last_slow = None
        self._last_save = None
        self._last_retention = None
        self._last_input = clock.monotonic()
        self._last_wake = clock.monotonic()
        self._lease: HealthState | None = None
        self._last_heartbeat: float | None = None
        self._gateway_error: str | None = None
        self._state: GatewayState | None = None
        self.started_at = iso(clock.now())
        self.startup_notes: list[str] = []
        if problem:
            self.startup_notes.append(problem)
        if repository.recovered_from:
            self.startup_notes.append(f"database_recovered: {repository.recovered_from}")
        for record in repository.recent_notices(200):
            key = record.get("dedupe_key", "")
            if key.startswith("morning:"):
                self.engine.morning_presented.add(key.split(":", 1)[1])

    # --------------------------------------------------------------- loop
    def _make_sources(self) -> None:
        s = self.settings
        self._calendar = make_calendar_adapter(s.calendar_source, self.scenario, shift=self._shift,
                                               link=self.repo.load_secret("calendar_link"), zone=s.zone)
        self._weather = make_weather_adapter(s.weather_source, self.scenario, shift=self._shift,
                                             location=s.location)

    def _request_token(self) -> str:
        """Unique per explicit request. A counter alone repeats after a restart
        while acknowledgement tombstones persist, so the first "Brief me"
        after a restart was silently refused (seen on the board)."""
        return f"{next(self._counter)}-{uuid4().hex[:8]}"

    async def start(self) -> None:
        self._diag("startup", {"gateway_mode": self.gateway_mode, "scenario": self.scenario.scenario_id,
                               "notes": self.startup_notes})
        await self._tick()
        self._task = asyncio.get_running_loop().create_task(self._run())
        # Load the model and its fixed instructions in the background, so the
        # first notice does not pay for them; the UI never waits on this.
        warm_up = getattr(self.recommender.backend, "warm_up", None)
        if warm_up is not None:
            self._warm_task = asyncio.get_running_loop().create_task(warm_up())

    async def stop(self) -> None:
        warm = getattr(self, "_warm_task", None)
        if warm is not None and not warm.done():
            warm.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.recommender.close()
        self.notifications.stop_audio()
        self._diag("shutdown", {})

    async def _run(self) -> None:
        while True:
            started = self.clock.monotonic()
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep controls responsive whatever fails
                log.exception("Control loop iteration failed")
            await asyncio.sleep(max(0.0, POLL_S - (self.clock.monotonic() - started)))

    async def _tick(self) -> None:
        mono = self.clock.monotonic()
        try:
            self._state = await self.gateway.read_state()
            self._gateway_error = None
        except Exception as exc:
            self._gateway_error = f"gateway_error: {exc}"
            self._state = GatewayState("1.0", None, (), (
                HealthState("bridge", "unavailable", self._gateway_error, None),))
        previous_boot = self.engine.boot_id
        self.engine.process_sensor_readings(self._state)
        if previous_boot and self._state.boot_id and previous_boot != self._state.boot_id:
            self.notifications.last_pixels = None  # resynchronize current state only
            self._diag("mcu_reboot_detected", {"boot_id": self._state.boot_id})
        near = self.engine.proximity_status.near_transition
        if near:
            self._wake()
        # A wake evaluates triggers at once, so an approach is answered promptly.
        slow = near or self._last_slow is None or mono - self._last_slow >= SLOW_TICK_S
        if slow:
            self._last_slow = mono
            await self._slow_tick()
        self._update_availability()
        level = self.display()["pixel_level"]
        await self.notifications.dispatch(self.gateway, fault=self._sensor_fault(),
                                          pixel_level=level, outputs_ready=self._outputs_ready())

    async def _slow_tick(self) -> None:
        self.engine.load_calendar_weather(self._calendar, self._weather)
        for spec in self.engine.evaluate_triggers():
            self._handle_spec(spec)
        self.notifications.recheck_deferred()
        self._generate_waiting()
        self.notifications.cancel_expired_outputs(self.engine.evidence_problem)
        for notice in self.notifications.notices.values():
            if notice.terminal:
                for request_id, (nid, _) in list(self._pending.items()):
                    if nid == notice.notice_id:
                        self.recommender.cancel(request_id)
                        del self._pending[request_id]
        mono = self.clock.monotonic()
        if self._last_heartbeat is None or mono - self._last_heartbeat >= HEARTBEAT_S:
            self._last_heartbeat = mono
            try:
                self._lease = await self.gateway.heartbeat(self.notifications.session_id)
            except Exception as exc:
                self._lease = HealthState("output_lease", "unavailable", str(exc), None)
        if self._last_save is None or mono - self._last_save >= EVIDENCE_SAVE_S:
            self._last_save = mono
            self.repo.save_evidence([e.to_dict() for e in self.engine.evidence.values()
                                     if e.source_type in ("calendar", "weather", "derived")],
                                    self.clock.now())
        if self._last_retention is None or mono - self._last_retention >= RETENTION_S:
            self._last_retention = mono
            self.repo.remove_expired_data(self.clock.now())

    # ------------------------------------------------------- notice flow
    def _handle_spec(self, spec: TriggerSpec) -> Notice | None:
        existing = next((n for n in self.notifications.notices.values()
                         if n.dedupe_key == spec.dedupe_key and not n.terminal), None)
        if existing is not None:
            self.notifications.raise_priority(existing.notice_id, spec.priority)
            return None
        revisions = self.engine.current_revisions()
        notice = self.notifications.create_candidate(
            category=spec.category, dedupe_key=spec.dedupe_key, priority=spec.priority,
            evidence={e: revisions[e] for e in spec.evidence if e in revisions},
            required=spec.required, source_mode=self._source_mode(spec),
            latest_expiry=spec.latest_expiry, explicit=spec.explicit)
        if notice is None:
            return None
        self.engine.mark_presented(spec)
        self._specs[notice.notice_id] = spec
        if notice.lifecycle == Lifecycle.GENERATING:
            self._generate(notice, spec)
        return notice

    def _generate_waiting(self) -> None:
        """Start generation for notices released from Focus (or otherwise
        waiting) that have no request in flight."""
        in_flight = {nid for nid, _ in self._pending.values()}
        for notice in list(self.notifications.notices.values()):
            spec = self._specs.get(notice.notice_id)
            if (spec and notice.lifecycle == Lifecycle.GENERATING
                    and notice.notice_id not in in_flight):
                self._generate(notice, spec)

    def _source_mode(self, spec: TriggerSpec) -> str:
        modes = {self.engine.evidence[e].source_mode for e in spec.evidence if e in self.engine.evidence}
        return "simulated" if "simulated" in modes else "real"

    def _generate(self, notice: Notice, spec: TriggerSpec) -> None:
        snapshot = self.engine.build_snapshot(spec, notice.notice_id)
        notice.generation = {"request_id": snapshot.request_id, "snapshot_id": snapshot.snapshot_id,
                             "started_at": snapshot.created_at, "deadline": snapshot.deadline}
        self._pending[snapshot.request_id] = (notice.notice_id, spec)
        self.recommender.submit(snapshot)

    async def _on_generation(self, result: GenerationResult) -> None:
        entry = self._pending.pop(result.snapshot.request_id, None)
        if entry is None:
            return
        notice_id, spec = entry
        notice = self.notifications.notices.get(notice_id)
        if notice is None or notice.terminal:
            self._diag("late_result_ignored", {"notice_id": notice_id, "outcome": result.outcome})
            return
        outcome, reason = result.outcome, result.reason
        if outcome == "generated":
            obsolete = self.engine.obsolete_reason(result.snapshot, result.response.evidence_ids)
            if obsolete:
                outcome, reason = "rejected", obsolete
        generation = {
            "request_id": result.snapshot.request_id, "outcome": outcome, "reason": reason,
            "started_at": result.started_at, "finished_at": result.finished_at,
            "execution_location": result.execution_location,
            "model": self.recommender.backend.model_version,
        }
        self._diag("generation", {"notice_id": notice_id, **generation})
        if outcome == "generated":
            response = result.response
            used = [e for e in result.snapshot.evidence if e.evidence_id in response.evidence_ids]
            generation["evidence_detail"] = [e.to_dict() for e in used]
            self.notifications.apply_text(
                notice_id, text=response.text, origin="generated", request_outcome="generated",
                validation="passed", evidence={e.evidence_id: e.revision for e in used},
                generation=generation)
            return
        # Fallback from current evidence, never from the stale snapshot.
        fresh = self.engine.build_snapshot(spec, notice_id)
        fallback = factual_fallback(fresh)
        if fallback is not None:
            used = [e for e in fresh.evidence if e.evidence_id in fallback.evidence_ids]
            generation["evidence_detail"] = [e.to_dict() for e in used]
            self.notifications.apply_text(
                notice_id, text=fallback.text, origin="factual_fallback", request_outcome=outcome,
                validation="not_applicable",
                evidence={e.evidence_id: e.revision for e in used}, generation=generation)
        elif spec.explicit:
            missing = ", ".join(sorted({self.engine.evidence[e].label + " " + self.engine.evidence[e].quality
                                        for e in spec.evidence if e in self.engine.evidence
                                        and self.engine.evidence[e].quality != "valid"})) or "no data"
            generation["evidence_detail"] = [self.engine.evidence[e].to_dict()
                                             for e in spec.evidence if e in self.engine.evidence]
            self.notifications.apply_text(
                notice_id, text=f"Can't answer from current data ({missing}).", origin="status",
                request_outcome=outcome, validation="not_applicable", evidence=None,
                generation=generation)
        else:
            self.notifications.apply_text(notice_id, text=None, origin="status",
                                          request_outcome=outcome, validation="not_applicable",
                                          evidence=None, generation=generation)

    # ------------------------------------------------------------ helpers
    def _diag(self, kind: str, data: dict) -> None:
        try:
            self.repo.write_diagnostic(kind, data, self.clock.now())
        except Exception:
            log.exception("Diagnostic write failed")

    def _wake(self) -> None:
        self._last_wake = self.clock.monotonic()

    def _sensor_fault(self) -> bool:
        health = self.engine.health
        return any(health.get(m, {}).get("state") != "ready" for m in ("bridge",) + SENSOR_MODULES)

    def _outputs_ready(self) -> bool:
        health = self.engine.health
        return (self._lease is not None and self._lease.state == "ready"
                and health.get("bridge", {}).get("state") == "ready")

    def _update_availability(self) -> None:
        a = self.notifications.availability
        a.distance_state = self.engine.proximity_status.state
        a.clear_since = self.engine.proximity_status.clear_since
        a.last_input = self._last_input
        a.buzzer_ready = self.engine.health.get("buzzer", {}).get("state") == "ready"
        a.pixels_ready = self.engine.health.get("pixels", {}).get("state") == "ready"

    def display(self) -> dict:
        from .rules import brightness
        light = self.engine.sensor_view.get("ambient_light", {})
        level = brightness(light.get("value") if light.get("quality") == "valid" else None, self.settings)
        idle = self.clock.monotonic() - max(self._last_input, self._last_wake)
        awake = idle < WAKE_TIMEOUT_S
        return {"awake": awake, "screen_level": level["screen"] if awake else min(level["screen"], 0.2),
                "pixel_level": level["pixels"], "source": level["source"],
                "calibrated": level["calibrated"], "native_brightness": False,
                "method": "visual_overlay"}

    def _save_settings(self, settings: Settings) -> None:
        self.repo.save_settings(settings, self.clock.now())
        self.settings = settings

    # ------------------------------------------------------------ actions
    async def action(self, name: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        self._last_input = self.clock.monotonic()  # any touch is input and wakes
        handler = getattr(self, f"_act_{name}", None)
        if handler is None:
            raise ActionError(f"Unknown action: {name}")
        result = handler(payload)
        if asyncio.iscoroutine(result):
            result = await result
        self._update_availability()
        if name != "touch":  # touches only wake the screen; don't flood the log
            self._diag("action", {"action": name, "result": result.get("status") if result else None})
        return result or {"status": "ok"}

    def _act_touch(self, payload):
        self._wake()
        return {"status": "ok"}

    def _act_acknowledge(self, payload):
        notice = self.notifications.acknowledge(str(payload.get("notice_id", "")))
        if notice is None:
            raise ActionError("Notice is not active (already acknowledged or expired)")
        for request_id, (nid, _) in list(self._pending.items()):
            if nid == notice.notice_id:
                self.recommender.cancel(request_id)
        return {"status": "acknowledged", "notice_id": notice.notice_id}

    def _act_mute(self, payload):
        muted = payload.get("muted")
        if not isinstance(muted, bool):
            raise ActionError("muted must be true or false")
        self.notifications.set_mute(muted)
        self._save_settings(self.settings.updated({"muted": muted}))
        return {"status": "muted" if muted else "unmuted"}

    def _act_focus(self, payload):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ActionError("enabled must be true or false")
        self.notifications.set_focus(enabled)
        self._generate_waiting()
        self._save_settings(self.settings.updated({"focus": enabled}))
        return {"status": "focus_on" if enabled else "focus_off"}

    def _act_briefing(self, payload):
        notice = self._handle_spec(self.engine.briefing_request(self._request_token()))
        if notice is None:
            raise ActionError("A briefing is already being prepared")
        return {"status": "requested", "notice_id": notice.notice_id}

    def _act_ask(self, payload):
        question = payload.get("question")
        if question not in SUPPORTED_QUESTIONS:
            raise ActionError("Unsupported question. Choose one of the listed questions.")
        notice = self._handle_spec(self.engine.question_request(question, self._request_token()))
        return {"status": "requested", "notice_id": notice.notice_id if notice else None}

    def _act_play(self, payload):
        result = self.notifications.request_play(str(payload.get("notice_id", "")))
        return {"status": result.status, "reason": result.reason}

    def _act_stop_audio(self, payload):
        self.notifications.stop_audio()
        return {"status": "stopped"}

    def _act_volume(self, payload):
        self._save_settings(self.settings.updated({"speech_volume": payload.get("volume")}))
        return {"status": "ok", "volume": self.settings.speech_volume,
                "applied": False, "reason": "speech_volume_saved_no_audio_adapter"}

    def _act_settings(self, payload):
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            raise ActionError("changes must be an object")
        # Focus/mute have their own actions; the location only comes from a search result.
        changes = {k: v for k, v in changes.items() if k not in ("focus", "muted", "location")}
        try:
            updated = self.settings.updated(changes)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        sources_changed = (updated.calendar_source != self.settings.calendar_source
                           or updated.weather_source != self.settings.weather_source
                           or updated.time_zone != self.settings.time_zone)  # floating calendar times
        self._save_settings(updated)
        if sources_changed:
            self._make_sources()
        return {"status": "saved"}

    async def _act_calendar_connect(self, payload):
        """Check a calendar subscription link once, then store it as a secret
        and switch the calendar to it. The link is never returned or logged."""
        from .calendar_ics import CalendarLinkError, check_link, normalize_link, provider_for
        try:
            link = normalize_link(payload.get("link"))
            count = await asyncio.to_thread(check_link, link, self.clock.now(), self.settings.zone)
        except CalendarLinkError as exc:
            raise ActionError(f"Calendar link not accepted: {exc}") from None
        except ImportError:
            raise ActionError("Calendar support is not installed (icalendar)") from None
        except Exception as exc:
            raise ActionError(f"Calendar link not accepted: {type(exc).__name__}") from None
        provider = provider_for(link)
        self.repo.save_secret("calendar_link", link)
        self._save_settings(self.settings.updated({"calendar_source": provider}))
        self._make_sources()
        return {"status": "connected", "provider": provider, "events": count}

    def _act_calendar_disconnect(self, payload):
        self.repo.save_secret("calendar_link", None)
        self._save_settings(self.settings.updated({"calendar_source": "fixture"}))
        self._make_sources()
        return {"status": "disconnected"}

    async def _act_location_search(self, payload):
        from .weather import search_locations
        query = payload.get("query")
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 80:
            raise ActionError("Type at least 2 characters to search")
        try:
            places = await asyncio.to_thread(search_locations, query)
        except Exception as exc:  # network, HTTP, malformed reply
            raise ActionError(f"Location search failed ({type(exc).__name__}); check the network") from exc
        return {"status": "ok", "results": places}

    def _act_set_location(self, payload):
        """Save a place from a search result; the time zone follows it."""
        place = payload.get("location")
        changes = {"location": place}
        if isinstance(place, dict) and place.get("time_zone"):
            changes["time_zone"] = place["time_zone"]
        try:
            updated = self.settings.updated(changes)
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        self._save_settings(updated)
        self._make_sources()
        return {"status": "saved", "location": updated.location}

    def _act_clear_data(self, payload):
        self.repo.clear_data(keep_settings=True)
        self.notifications.tombstones.clear()
        return {"status": "cleared"}

    # ------------------------------------------------ simulation actions
    def _require_scenario_clock(self) -> ScenarioClock:
        if not isinstance(self.clock, ScenarioClock):
            raise ActionError("Scenario time controls need the scenario clock")
        return self.clock

    def _require_sim_gateway(self):
        if self.gateway_mode != "simulated":
            raise ActionError("Sensor injection is only available with the simulated gateway")
        return self.gateway

    def _act_sim_scenario(self, payload):
        scenario = self.scenarios.get(payload.get("scenario_id"))
        if scenario is None:
            raise ActionError("Unknown scenario")
        self.scenario = scenario
        if isinstance(self.clock, ScenarioClock):
            self.clock.reset(scenario.start)
        # On the device clock the scenario's data is replayed from now.
        if self._replay:
            self._shift = demo_shift(scenario, self.clock.now())
        self._make_sources()
        for notice in self.notifications.notices.values():
            if not notice.terminal:
                self.notifications._end(notice, Lifecycle.EXPIRED, "scenario_changed")
        self.engine.morning_presented.clear()
        if self.gateway_mode == "simulated":
            gw = self.gateway
            values = scenario.sensors
            for metric, key in (("temperature", "temperature"), ("humidity", "humidity"),
                                ("ambient_light", "lux"), ("distance", "distance_mm")):
                if key in values:
                    gw.set_value(metric, values[key])
        self._last_slow = None
        return {"status": "loaded", "scenario_id": scenario.scenario_id}

    def _act_sim_advance(self, payload):
        seconds = payload.get("seconds")
        if not isinstance(seconds, (int, float)) or not 0 < seconds <= 6 * 3600:
            raise ActionError("seconds must be between 0 and 21600")
        self._require_scenario_clock().advance(float(seconds))
        self._last_slow = None
        return {"status": "advanced", "seconds": seconds}

    def _act_sim_sensor(self, payload):
        gw = self._require_sim_gateway()
        try:
            gw.set_value(payload.get("metric"), float(payload.get("value")))
        except (TypeError, ValueError) as exc:
            raise ActionError(str(exc)) from exc
        return {"status": "set"}

    def _act_sim_fault(self, payload):
        gw = self._require_sim_gateway()
        target = payload.get("target")
        active = bool(payload.get("active"))
        if target in SENSOR_MODULES:
            gw.module_fault[target] = f"{target}_disconnected" if active else None
        elif target == "bridge":
            gw.bridge_fault = "router_unavailable: simulated" if active else None
        elif target == "reboot":
            gw.reboot()
        elif target in ("pixels", "buzzer"):
            setattr(gw, f"{target}_available", not active)
        elif target in ("calendar", "weather"):
            adapter = self._calendar if target == "calendar" else self._weather
            if hasattr(adapter, "failure"):
                adapter.failure = "simulated_adapter_loss" if active else None
            self._last_slow = None
        else:
            raise ActionError("Unknown fault target")
        return {"status": "set", "target": target, "active": active}

    # --------------------------------------------------------- view model
    def _calendar_link_view(self) -> dict:
        from .calendar_ics import link_host
        link = self.repo.load_secret("calendar_link")
        return {"connected": bool(link), "host": link_host(link)}

    def view_model(self) -> dict[str, Any]:
        s = self.settings
        now = self.clock.now()
        local = now.astimezone(s.zone)
        engine = self.engine
        selected = self.notifications.select_priority_notice()
        sched = engine.evidence.get("calendar.schedule")
        weather = engine.evidence.get("weather.now")
        notices = sorted(self.notifications.notices.values(),
                         key=lambda n: (n.terminal, -n.priority, n.created_at))
        return {
            "schema_version": "1.0",
            "gateway_mode": self.gateway_mode,
            "clock": {
                "utc": iso(now), "local_time": local.strftime("%H:%M"),
                "local_seconds": local.strftime("%S"),
                "local_date": local.strftime("%a %d %b %Y"), "time_zone": s.time_zone,
                "mode": self.clock.mode,
                "label": "Scenario time (simulated)" if self.clock.mode == "simulated" else "Device time",
            },
            "display": self.display(),
            "status": {**self.notifications.snapshot(), "audio_policy": s.audio_policy,
                       "network": "not_monitored"},
            "indoor": {
                "temperature": engine.sensor_view.get("temperature"),
                "humidity": engine.sensor_view.get("humidity"),
                "ambient_light": engine.sensor_view.get("ambient_light"),
                "distance": engine.sensor_view.get("distance"),
                "proximity": engine.proximity_status.state,
                "comfort_available": engine.comfort_status.available,
                "comfort_active": sorted(engine.comfort_status.active),
            },
            "schedule": None if sched is None else {
                **(sched.value or {}), "quality": sched.quality, "label": sched.label,
                "source_mode": sched.source_mode, "valid_until": sched.valid_until,
                "error": engine.sources.get("calendar").error if engine.sources.get("calendar") else None,
            },
            "forecast": None if weather is None else {
                **(weather.value or {}), "quality": weather.quality, "label": weather.label,
                "source_mode": weather.source_mode, "valid_until": weather.valid_until,
                "observed_at": weather.observed_at,
                "error": engine.sources.get("weather").error if engine.sources.get("weather") else None,
            },
            "notice": self._notice_view(selected) if selected else None,
            "notices": [self._notice_view(n) for n in notices[:20]],
            "health": self._health_view(),
            "audio": {**self.notifications.speech.status(), "volume": s.speech_volume,
                      "policy": s.audio_policy, "buzzer_enabled": s.buzzer_enabled},
            "settings": s.to_dict(),
            # Only whether a link is stored and its host; never the link itself.
            "calendar_link": self._calendar_link_view(),
            "questions": SUPPORTED_QUESTIONS,
            "simulation": {
                "scenario_id": self.scenario.scenario_id, "scenario_title": self.scenario.title,
                "scenario_description": self.scenario.description,
                "scenarios": [{"scenario_id": k, "title": v.title, "description": v.description}
                              for k, v in self.scenarios.items()],
                "time_controls": isinstance(self.clock, ScenarioClock),
                "sensor_controls": self.gateway_mode == "simulated",
                "values": dict(getattr(self.gateway, "values", {})),
                "faults": {**getattr(self.gateway, "module_fault", {}),
                           "bridge": getattr(self.gateway, "bridge_fault", None),
                           "calendar": getattr(self._calendar, "failure", None),
                           "weather": getattr(self._weather, "failure", None)},
            },
            "diagnostics": self.repo.diagnostics(25),
            "startup_notes": self.startup_notes,
        }

    def _notice_view(self, notice: Notice) -> dict:
        data = notice.to_dict()
        data["routes"] = {k: vars(v) for k, v in notice.routes.items()}
        now = self.clock.now()
        detail = notice.generation.get("evidence_detail") or [
            self.engine.evidence[e].to_dict() for e in notice.evidence if e in self.engine.evidence]
        current = self.engine.evidence
        for item in detail:
            live = current.get(item["evidence_id"])
            item["current_quality"] = live.quality if live else "no_longer_present"
            item["current_revision"] = live.revision if live else None
        data["evidence_detail"] = detail
        data["expires_in_s"] = max(0, int((parse_iso(notice.expires_at) - now).total_seconds()))
        data["can_play"] = self.notifications.audio_permission(notice, manual=True)[0]
        data["play_blocked_reason"] = self.notifications.audio_permission(notice, manual=True)[1]
        return data

    def _health_view(self) -> list[dict]:
        items = []
        for component, info in self.engine.health.items():
            items.append({"component_id": component, **info})
        if self._lease is not None:
            items.append({"component_id": "output_lease", "state": self._lease.state,
                          "reason": self._lease.reason, "last_success_at": self._lease.last_success_at,
                          "execution_location": self._lease.execution_location})
        for kind in ("calendar", "weather"):
            src = self.engine.sources.get(kind)
            if src:
                state = {"valid": "ready", "stale": "degraded"}.get(src.quality, src.quality)
                items.append({"component_id": f"{kind}_source", "state": state,
                              "reason": src.error, "last_success_at": src.observed_at,
                              "execution_location": "uno_q_linux", "label": src.label,
                              "source_mode": src.source_mode})
        rec = self.recommender.status()
        last = rec["last_outcome"] or {}
        if rec["backend"] == "UnavailableModel":
            model_state, reason = "unavailable", "No model configured; factual fallback in use"
        elif rec["active_request"]:
            model_state, reason = "busy", f"Generating with {rec['model_version'] or 'the on-board model'}"
        elif last.get("outcome") in ("error", "timed_out"):
            # The worker stays available; the next request tries again.
            model_state, reason = "degraded", f"Last request {last['outcome'].replace('_', ' ')}: {last.get('reason')}"
        else:
            model_state = "ready"
            reason = f"{rec['model_version'] or 'On-board model'} on the UNO Q" + (
                f"; last reply {last['outcome']}" if last.get("outcome") else "")
        items.append({"component_id": "model", "state": model_state, "reason": reason,
                      "last_success_at": None, "execution_location": rec["execution_location"]})
        speech = self.notifications.speech.status()
        items.append({"component_id": "speech", "state": "ready" if speech.get("ready") else "unavailable",
                      "reason": speech.get("reason"), "last_success_at": None,
                      "execution_location": "uno_q_linux"})
        items.append({"component_id": "storage", "state": "ready",
                      "reason": self.repo.recovered_from, "last_success_at": None,
                      "execution_location": "uno_q_linux"})
        return items
