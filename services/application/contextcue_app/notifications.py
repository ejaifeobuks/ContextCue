"""NotificationService: notice identity, priority, delivery policy and
cancellation across screen, Pixels, Buzzer and speech (APP-12 to APP-16).

Policy decisions are synchronous and deterministic. Hardware and speech I/O
happen only in `dispatch()`, which the application loop awaits; every
asynchronous callback rechecks the notice lifecycle first, so an acknowledged
or expired notice can never be revived by a late result.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol
from uuid import uuid4

from contextcue_contracts import SCHEMA_VERSION as HW_SCHEMA, DeviceCommand

from .clock import iso, parse_iso
from .contracts import (COMFORT, CONTEXT_ANSWER, MORNING, PREPARATION, Lifecycle, Notice,
                        RouteResult)
from .settings import Settings, in_window, parse_hhmm

LIFETIME = {MORNING: timedelta(minutes=5), COMFORT: timedelta(minutes=5),
            CONTEXT_ANSWER: timedelta(minutes=2), PREPARATION: timedelta(minutes=30)}
TERMINAL_KEEP = timedelta(minutes=10)  # how long ended notices stay listed
CHIME_HZ, CHIME_MS = 1760, 120
PIXEL_RETRY_S = 5.0


class SpeechPort(Protocol):
    def status(self) -> dict: ...

    async def play(self, playback_id: str, text: str, volume: int) -> str: ...

    async def stop(self, playback_id: str) -> None: ...


class OutputPort(Protocol):
    async def execute(self, command: DeviceCommand): ...


class NullSpeech:
    """No TTS engine is selected yet (plan section D). Reports that plainly."""

    def status(self) -> dict:
        return {"engine": None, "endpoint": None, "paired": False, "connected": False,
                "ready": False, "reason": "no_tts_engine_selected"}

    async def play(self, playback_id: str, text: str, volume: int) -> str:
        return "unavailable: no_tts_engine_selected"

    async def stop(self, playback_id: str) -> None:
        return None


@dataclass
class Availability:
    """Inputs for availability routing. Never a claim about the user."""

    distance_state: str = "unknown"  # near | clear | unknown
    clear_since: float | None = None
    last_input: float | None = None
    buzzer_ready: bool = False
    pixels_ready: bool = True


class NotificationService:
    def __init__(self, *, now: Callable[[], datetime], monotonic: Callable[[], float],
                 settings: Callable[[], Settings], repository=None,
                 speech: SpeechPort | None = None, log: Callable[[str, dict], None] | None = None):
        self._now = now
        self._mono = monotonic
        self._settings = settings
        self._repo = repository
        self.speech = speech or NullSpeech()
        self._log = log or (lambda kind, data: None)
        self.notices: dict[str, Notice] = {}
        self.tombstones: dict[str, dict] = dict(repository.tombstones()) if repository else {}
        self.muted = False
        self.focus = False
        self.availability = Availability()
        self.audio_queue: list[tuple[str, int, bool]] = []  # (notice_id, revision, manual)
        self._playing: tuple[str, str, asyncio.Task] | None = None  # notice, playback, task
        self._pending_buzzer: list[str] = []
        self._pending_stops: list[str] = []
        self._ids = itertools.count(1)
        self.session_id = f"app-{uuid4().hex[:12]}"
        self._commands = itertools.count(1)
        self.last_pixels: tuple[str, float] | None = None
        self.pixel_result: RouteResult | None = None
        self._manual_playback: dict[str, bool] = {}
        self._pixel_retry_at: float | None = None

    # ------------------------------------------------------------ identity
    def create_candidate(self, *, category: str, dedupe_key: str, priority: int,
                         evidence: dict[str, int], required: list[str], source_mode: str,
                         lifetime: timedelta | None = None, latest_expiry: datetime | None = None,
                         explicit: bool = False, important: bool = False) -> Notice | None:
        """Allocate the notice ID before generation. Returns None when the key
        is tombstoned (already acknowledged) or a live notice already has it."""
        if dedupe_key in self.tombstones:
            return None
        for notice in self.notices.values():
            if notice.dedupe_key == dedupe_key and not notice.terminal:
                return None
        now = self._now()
        expires = now + (lifetime or LIFETIME[category])
        if latest_expiry is not None:
            expires = min(expires, latest_expiry)
        if expires <= now:
            return None
        notice = Notice(
            notice_id=f"notice-{next(self._ids)}-{uuid4().hex[:6]}", revision=1,
            dedupe_key=dedupe_key, category=category, priority=priority, text="",
            evidence=dict(evidence), required=list(required), created_at=iso(now),
            expires_at=iso(expires), source_mode=source_mode, explicit=explicit,
            important=important,
        )
        if self._presentation_blocked(notice):
            notice.lifecycle = Lifecycle.DEFERRED
            notice.deferred_reason = "focus"
        else:
            notice.lifecycle = Lifecycle.GENERATING
        self.notices[notice.notice_id] = notice
        self._persist(notice)
        self._log("notice_created", {"notice_id": notice.notice_id, "category": category,
                                     "priority": priority, "lifecycle": notice.lifecycle.value})
        return notice

    def _presentation_blocked(self, notice: Notice) -> bool:
        # Focus defers unsolicited morning briefings; on-request stays available.
        return self.focus and notice.category == MORNING and not notice.explicit

    def raise_priority(self, notice_id: str, priority: int) -> None:
        notice = self.notices.get(notice_id)
        if notice and not notice.terminal and priority > notice.priority:
            notice.priority = priority
            notice.revision += 1
            self._persist(notice)

    # -------------------------------------------------------- generation
    def apply_text(self, notice_id: str, *, text: str | None, origin: str,
                   request_outcome: str, validation: str, evidence: dict[str, int] | None,
                   generation: dict[str, Any]) -> bool:
        """Publish generated or fallback text. Returns False when the notice
        has ended (late callback): nothing is revived."""
        notice = self.notices.get(notice_id)
        if notice is None or notice.terminal:
            self._log("late_result_ignored", {"notice_id": notice_id, "outcome": request_outcome})
            return False
        notice.request_outcome = request_outcome
        notice.generation = generation
        if text is None:
            # No valid facts remain: a status, not invented advice.
            self._end(notice, Lifecycle.EXPIRED, "no_valid_facts")
            return False
        notice.text = text
        notice.origin = origin
        notice.validation = validation
        if evidence is not None:
            notice.evidence = dict(evidence)
        notice.revision += 1
        if notice.lifecycle == Lifecycle.DEFERRED:
            self._persist(notice)
            return True
        notice.lifecycle = Lifecycle.ACTIVE
        self._persist(notice)
        self._route_new_active(notice)
        return True

    # ------------------------------------------------------------ controls
    def acknowledge(self, notice_id: str) -> Notice | None:
        notice = self.notices.get(notice_id)
        if notice is None or notice.terminal:
            return None
        notice.lifecycle = Lifecycle.ACKNOWLEDGED
        notice.acknowledged_at = iso(self._now())
        notice.ended_reason = "acknowledged"
        self.tombstones[notice.dedupe_key] = {"notice_id": notice.notice_id,
                                              "revision": notice.revision,
                                              "reason": "acknowledged", "at": notice.acknowledged_at}
        if self._repo:
            self._repo.save_acknowledgement(notice.to_dict(), "acknowledged", self._now())
        self._cancel_audio(notice.notice_id, "acknowledged")
        self._log("notice_acknowledged", {"notice_id": notice.notice_id})
        return notice

    def set_mute(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self._cancel_audio(None, "muted")
            self._pending_buzzer.clear()
        # Unmute never auto-replays.
        self._log("mute", {"muted": muted})

    def set_focus(self, enabled: bool) -> None:
        self.focus = enabled
        if enabled:
            # Losing permission stops automatic speech; visuals stay.
            playing = self._playing
            if playing and not self._manual_playback.get(playing[1], False):
                self._cancel_audio(playing[0], "focus")
        self._log("focus", {"focus": enabled})
        self.recheck_deferred()

    def recheck_deferred(self) -> list[Notice]:
        """Deferred notices must pass eligibility again before delivery."""
        released = []
        for notice in list(self.notices.values()):
            if notice.lifecycle == Lifecycle.DEFERRED and not self._presentation_blocked(notice):
                notice.deferred_reason = None
                if notice.text:
                    notice.lifecycle = Lifecycle.ACTIVE
                    notice.revision += 1
                    self._route_new_active(notice)
                else:
                    notice.lifecycle = Lifecycle.GENERATING
                self._persist(notice)
                released.append(notice)
        return released

    # -------------------------------------------------------------- expiry
    def cancel_expired_outputs(self, evidence_ok: Callable[[Notice], str | None]) -> list[Notice]:
        """Expire notices past their expiry or whose required evidence changed."""
        now = self._now()
        ended = []
        for notice in list(self.notices.values()):
            if notice.terminal:
                if now - parse_iso(notice.created_at) > TERMINAL_KEEP + LIFETIME[PREPARATION]:
                    del self.notices[notice.notice_id]
                continue
            reason = "expired" if now >= parse_iso(notice.expires_at) else evidence_ok(notice)
            if reason:
                self._end(notice, Lifecycle.EXPIRED, reason)
                ended.append(notice)
        self.audio_queue = [q for q in self.audio_queue
                            if q[0] in self.notices and not self.notices[q[0]].terminal]
        return ended

    def _end(self, notice: Notice, lifecycle: Lifecycle, reason: str) -> None:
        notice.lifecycle = lifecycle
        notice.ended_reason = reason
        self._cancel_audio(notice.notice_id, reason)
        self._persist(notice)
        self._log("notice_ended", {"notice_id": notice.notice_id, "reason": reason})

    # ----------------------------------------------------------- selection
    def active(self) -> list[Notice]:
        return [n for n in self.notices.values() if n.lifecycle == Lifecycle.ACTIVE]

    def select_priority_notice(self) -> Notice | None:
        """Highest priority; ties by earliest expiry, oldest creation, then ID."""
        candidates = self.active()
        if not candidates:
            return None
        return sorted(candidates, key=lambda n: (-n.priority, n.expires_at, n.created_at,
                                                 n.notice_id))[0]

    # --------------------------------------------------------- audio policy
    def quiet_hours(self) -> bool:
        s = self._settings()
        local = self._now().astimezone(s.zone).time()
        return in_window(local, parse_hhmm(s.quiet_start), parse_hhmm(s.quiet_end))

    def long_idle(self) -> bool:
        a = self.availability
        if a.distance_state != "clear" or a.clear_since is None:
            return False
        idle_since = max(a.clear_since, a.last_input or a.clear_since)
        return self._mono() - idle_since >= self._settings().long_idle_s

    def audio_permission(self, notice: Notice, *, manual: bool) -> tuple[bool, str | None]:
        s = self._settings()
        if self.muted:
            return False, "muted"
        if notice.lifecycle != Lifecycle.ACTIVE:
            return False, f"notice_{notice.lifecycle.value}"
        if self._now() >= parse_iso(notice.expires_at):
            return False, "expired"
        if manual:
            if s.audio_policy == "visual_only":
                return False, "audio_policy_visual_only"
            return True, None  # manual play may override Focus and quiet hours
        if s.audio_policy != "automatic":
            return False, f"audio_policy_{s.audio_policy}"
        if notice.announced:
            return False, "already_announced"
        if self.quiet_hours():
            return False, "quiet_hours"
        if self.focus and not (notice.priority >= 2 and s.focus_p2_speech
                               and notice.category == PREPARATION):
            return False, "focus"
        if self.long_idle():
            if not s.away_audio_enabled:
                return False, "long_idle_clear_distance"
            if not self.speech.status().get("connected"):
                return False, "away_endpoint_disconnected_queued"
        return True, None

    def buzzer_permission(self, notice: Notice) -> tuple[bool, str | None]:
        s = self._settings()
        if not s.buzzer_enabled:
            return False, "buzzer_not_enabled"
        if not self.availability.buzzer_ready:
            return False, "buzzer_unavailable"
        if self.muted:
            return False, "muted"
        if notice.announced:
            return False, "already_announced"
        if s.audio_policy != "automatic":
            return False, f"audio_policy_{s.audio_policy}"
        if self.quiet_hours():
            return False, "quiet_hours"
        if self.focus:
            return False, "focus"
        if self.long_idle():
            return False, "long_idle_clear_distance"
        return True, None

    def _route_new_active(self, notice: Notice) -> None:
        at = iso(self._now())
        notice.routes["screen"] = RouteResult("screen", "permitted", None, at)
        ok, reason = self.buzzer_permission(notice)
        notice.routes["buzzer"] = RouteResult("buzzer", "permitted" if ok else "blocked", reason, at)
        if ok:
            self._pending_buzzer.append(notice.notice_id)
        ok, reason = self.audio_permission(notice, manual=False)
        notice.routes["speech"] = RouteResult("speech", "permitted" if ok else "blocked", reason, at)
        if ok:
            self.audio_queue.append((notice.notice_id, notice.revision, False))
        if ok or notice.routes["buzzer"].status == "permitted":
            notice.announced = True

    def request_play(self, notice_id: str) -> RouteResult:
        """Explicit Play from the touchscreen."""
        notice = self.notices.get(notice_id)
        at = iso(self._now())
        if notice is None:
            return RouteResult("speech", "rejected", "unknown_notice", at)
        ok, reason = self.audio_permission(notice, manual=True)
        result = RouteResult("speech", "permitted" if ok else "blocked", reason, at)
        notice.routes["speech"] = result
        if ok:
            self.audio_queue.append((notice.notice_id, notice.revision, True))
        return result

    def stop_audio(self) -> None:
        self._cancel_audio(None, "stopped_by_user")

    def _cancel_audio(self, notice_id: str | None, reason: str) -> None:
        self.audio_queue = [q for q in self.audio_queue if notice_id is not None and q[0] != notice_id]
        self._pending_buzzer = [] if notice_id is None else [
            n for n in self._pending_buzzer if n != notice_id]
        playing = self._playing
        if playing and (notice_id is None or playing[0] == notice_id):
            playing[2].cancel()
            self._pending_stops.append(playing[1])
            n = self.notices.get(playing[0])
            if n:
                n.routes["speech"] = RouteResult("speech", "stopped", reason, iso(self._now()))
            self._playing = None
        if reason in ("muted", "acknowledged"):
            self._pending_stops.append("buzzer")

    # ------------------------------------------------------------ dispatch
    def desired_pixels(self, fault: bool, level: float) -> tuple[str, float]:
        if self.select_priority_notice() is not None:
            return "notice", level
        return ("fault" if fault else "ready"), level

    def _command(self, target: str, action: str, parameters: dict | None = None,
                 notice: Notice | None = None) -> DeviceCommand:
        return DeviceCommand(
            schema_version=HW_SCHEMA, session_id=self.session_id,
            command_id=f"cmd-{next(self._commands)}-{uuid4().hex[:6]}", target=target,
            action=action, parameters=parameters or {}, ttl_ms=2000,
            notice_id=notice.notice_id if notice else None,
            notice_revision=notice.revision if notice else None)

    async def dispatch(self, outputs: OutputPort | None, *, fault: bool, pixel_level: float,
                       outputs_ready: bool) -> None:
        # Stops first, so a mute or acknowledgement always wins.
        stops, self._pending_stops = self._pending_stops, []
        for item in stops:
            if item == "buzzer":
                if outputs is not None and outputs_ready:
                    await outputs.execute(self._command("buzzer", "stop"))
            else:
                await self.speech.stop(item)
        if outputs is not None and outputs_ready:
            pattern, level = self.desired_pixels(fault, pixel_level)
            key = (pattern, round(level, 2))
            waiting = self._pixel_retry_at is not None and self._mono() < self._pixel_retry_at
            if not self.availability.pixels_ready:
                self.last_pixels = None  # resend once the module is back
            elif key != self.last_pixels and not waiting:
                result = await outputs.execute(self._command(
                    "pixels", "set_pattern", {"pattern": pattern, "brightness": round(level, 2)},
                    self.select_priority_notice()))
                self.pixel_result = RouteResult("pixels", result.status, result.reason, result.at,
                                                result.command_id)
                if result.status in ("accepted", "completed"):
                    self.last_pixels = key
                    self._pixel_retry_at = None
                else:  # never hammer the MCU with a command it keeps refusing
                    self._pixel_retry_at = self._mono() + PIXEL_RETRY_S
            buzz, self._pending_buzzer = self._pending_buzzer, []
            for notice_id in buzz:
                notice = self.notices.get(notice_id)
                if notice is None:
                    continue
                ok, reason = self.buzzer_permission_recheck(notice)
                if not ok:
                    notice.routes["buzzer"] = RouteResult("buzzer", "blocked", reason, iso(self._now()))
                    continue
                result = await outputs.execute(self._command(
                    "buzzer", "short_tone", {"frequency_hz": CHIME_HZ, "duration_ms": CHIME_MS}, notice))
                notice.routes["buzzer"] = RouteResult("buzzer", result.status, result.reason,
                                                      result.at, result.command_id)
        else:
            self.last_pixels = None  # resend current state after reconnect
            for notice_id in self._pending_buzzer:
                n = self.notices.get(notice_id)
                if n:
                    n.routes["buzzer"] = RouteResult("buzzer", "failed", "outputs_unavailable",
                                                     iso(self._now()))
            self._pending_buzzer = []  # tones never replay after reconnect
        if self._playing is None and self.audio_queue:
            notice_id, revision, manual = self.audio_queue.pop(0)
            notice = self.notices.get(notice_id)
            if notice is not None:
                self._start_playback(notice, manual)

    def buzzer_permission_recheck(self, notice: Notice) -> tuple[bool, str | None]:
        if notice.lifecycle != Lifecycle.ACTIVE:
            return False, f"notice_{notice.lifecycle.value}"
        if self.muted:
            return False, "muted"
        return True, None

    def _start_playback(self, notice: Notice, manual: bool) -> None:
        # Recheck eligibility immediately before playback (APP-16).
        ok, reason = self.audio_permission(notice, manual=True) if manual else (
            self._auto_recheck(notice))
        if not ok:
            notice.routes["speech"] = RouteResult("speech", "blocked", reason, iso(self._now()))
            return
        playback_id = f"play-{uuid4().hex[:10]}"
        self._manual_playback[playback_id] = manual
        task = asyncio.get_running_loop().create_task(self._play(notice.notice_id, playback_id, manual))
        self._playing = (notice.notice_id, playback_id, task)
        notice.routes["speech"] = RouteResult("speech", "started", None, iso(self._now()))

    def _auto_recheck(self, notice: Notice) -> tuple[bool, str | None]:
        announced = notice.announced
        notice.announced = False
        try:
            return self.audio_permission(notice, manual=False)
        finally:
            notice.announced = announced

    async def _play(self, notice_id: str, playback_id: str, manual: bool) -> None:
        notice = self.notices.get(notice_id)
        if notice is None:
            return
        volume = self._settings().speech_volume
        try:
            outcome = await self.speech.play(playback_id, notice.text, volume)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            outcome = f"failed: {exc}"
        notice = self.notices.get(notice_id)
        if self._playing and self._playing[1] == playback_id:
            self._playing = None
        if notice is None or notice.terminal:
            return  # late callback: nothing to update or retry
        notice.audio_attempts += 1
        status = outcome.split(":")[0]
        reason = outcome.split(":", 1)[1].strip() if ":" in outcome else None
        # Driver completion is not proof that anyone heard it.
        notice.routes["speech"] = RouteResult("speech", status, reason, iso(self._now()), playback_id)
        if status == "failed" and notice.audio_attempts < 2:
            ok, _ = self.audio_permission(notice, manual=True) if manual else self._auto_recheck(notice)
            if ok:
                self.audio_queue.append((notice.notice_id, notice.revision, manual))  # one retry max

    # --------------------------------------------------------------- utils
    def _persist(self, notice: Notice) -> None:
        if self._repo:
            self._repo.save_notice(notice.to_dict(), self._now())

    def snapshot(self) -> dict:
        selected = self.select_priority_notice()
        return {
            "muted": self.muted,
            "focus": self.focus,
            "quiet_hours": self.quiet_hours(),
            "long_idle": self.long_idle(),
            "selected_notice_id": selected.notice_id if selected else None,
            "audio_queue": [q[0] for q in self.audio_queue],
            "playing": self._playing[0] if self._playing else None,
            "session_id": self.session_id,
            "pixels": None if self.pixel_result is None else vars(self.pixel_result),
        }
