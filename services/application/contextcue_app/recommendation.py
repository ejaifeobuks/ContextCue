"""Recommendation port used by the application (APP-06 to APP-08).

The AI service owns the real worker and model benchmark (services/ai). This
module gives the application what it needs to run end to end without
pretending to generate:

* `RecommendationService`: async bounded queue (one active request, one
  coalesced waiting request), deadline, cancellation and late-result guard.
* `validate_response`: schema, category, evidence-ID and numeric-fact checks.
* `factual_fallback`: deterministic, labeled text built only from valid facts.
  It is required failure behavior and never counts as generation.
* `LlamaCppModel`: client for the `arduino:llm` Brick's OpenAI-compatible
  endpoint. Unbenchmarked (G1 is still open).
* `UnavailableModel`: the default when no model is configured. Every request
  ends as `error: model_not_configured` and the notice is a labeled fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from .clock import iso
from .contracts import (CATEGORIES, COMFORT, CONTEXT_ANSWER, MORNING, PREPARATION,
                        ContextSnapshot, ModelResponse)

MAX_SENTENCES = 3
MAX_CHARS = 400
GENERATION_TIMEOUT_S = 30.0  # development cap; G1 must freeze the release value
PROMPT_VERSION = "contextcue-prompt-1"

SUPPORTED_QUESTIONS = {
    "next_event": "What is my next event?",
    "rain_today": "Will it rain today?",
    "room_comfort": "How is the room?",
    "schedule_today": "How busy is today?",
}


class ModelUnavailable(RuntimeError):
    pass


class ModelBackend(Protocol):
    execution_location: str
    model_version: str | None

    async def complete(self, snapshot: ContextSnapshot) -> ModelResponse: ...


class UnavailableModel:
    execution_location = "none"
    model_version = None

    async def complete(self, snapshot: ContextSnapshot) -> ModelResponse:
        raise ModelUnavailable("model_not_configured")


class LlamaCppModel:
    """OpenAI-compatible chat completion against the on-board llama.cpp runner.

    No tools, no memory: each request sees only its own snapshot.
    """

    execution_location = "uno_q_linux"

    def __init__(self, base_url: str = "http://127.0.0.1:9999/v1", model: str | None = None,
                 max_tokens: int = 160, timeout_s: float = GENERATION_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.model_version = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s

    def _prompt(self, snapshot: ContextSnapshot) -> list[dict]:
        evidence = [
            {"id": e.evidence_id, "metric": e.metric, "value": e.value, "unit": e.unit,
             "quality": e.quality, "label": e.label}
            for e in snapshot.evidence
        ]
        system = (
            "You write short desk cues for ContextCue. Use only the evidence given. "
            "Never invent times, numbers, places or events. At most 3 sentences. "
            'Reply with JSON only: {"category": "...", "text": "...", "evidence_ids": ["..."]}.'
        )
        user = json.dumps({
            "category": snapshot.category, "reason": snapshot.reason,
            "question": snapshot.question, "facts": snapshot.facts, "evidence": evidence,
        }, default=str)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _call(self, snapshot: ContextSnapshot) -> str:
        body = {"messages": self._prompt(snapshot), "max_tokens": self.max_tokens,
                "temperature": 0.3, "response_format": {"type": "json_object"}}
        if self.model_version:
            body["model"] = self.model_version
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            payload = json.loads(response.read())
        return payload["choices"][0]["message"]["content"]

    async def complete(self, snapshot: ContextSnapshot) -> ModelResponse:
        started = iso(datetime.now().astimezone())
        try:
            text = await asyncio.to_thread(self._call, snapshot)
        except OSError as exc:
            raise ModelUnavailable(f"model_endpoint_unreachable: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("malformed_model_output") from exc
        if not isinstance(data, dict):
            raise ValueError("malformed_model_output")
        ids = data.get("evidence_ids")
        return ModelResponse(
            request_id=snapshot.request_id, snapshot_id=snapshot.snapshot_id,
            category=str(data.get("category", "")), text=str(data.get("text", "")),
            evidence_ids=tuple(ids) if isinstance(ids, list) else (),
            execution_location=self.execution_location, model_version=self.model_version,
            runtime_version="llamacpp-runner", prompt_version=PROMPT_VERSION,
            started_at=started, finished_at=iso(datetime.now().astimezone()),
        )


# ---------------------------------------------------------------- validation

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _allowed_numbers(snapshot: ContextSnapshot, referenced: set[str]) -> set[str]:
    allowed: set[str] = set()

    def add(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            for text in (f"{value:.0f}", f"{value:.1f}", str(value)):
                allowed.add(text.rstrip("0").rstrip(".") if "." in text else text)
        elif isinstance(value, str):
            for match in _NUMBER.findall(value):
                allowed.add(match.lstrip("0") or "0")
                allowed.add(match)
        elif isinstance(value, dict):
            for item in value.values():
                add(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    for evidence in snapshot.evidence:
        if evidence.evidence_id in referenced:
            add(evidence.value)
    add(snapshot.facts)
    return allowed


def validate_response(response: ModelResponse, snapshot: ContextSnapshot) -> str | None:
    """Return None when acceptable, otherwise a rejection reason."""
    if response.request_id != snapshot.request_id or response.snapshot_id != snapshot.snapshot_id:
        return "wrong_request"
    if response.category not in CATEGORIES or response.category != snapshot.category:
        return "wrong_category"
    text = response.text.strip()
    if not text:
        return "empty_text"
    if len(text) > MAX_CHARS or len(re.findall(r"[.!?](?:\s|$)", text)) > MAX_SENTENCES:
        return "too_long"
    available = {e.evidence_id for e in snapshot.evidence if e.quality == "valid"}
    referenced = set(response.evidence_ids)
    if not referenced:
        return "no_evidence"
    if not referenced <= available:
        return "unknown_or_invalid_evidence"
    allowed = _allowed_numbers(snapshot, referenced)
    for number in _NUMBER.findall(text):
        if number not in allowed and (number.lstrip("0") or "0") not in allowed:
            return f"unsupported_number:{number}"
    return None


# ------------------------------------------------------------ factual fallback

def _evidence(snapshot: ContextSnapshot, metric: str):
    for e in snapshot.evidence:
        if e.metric == metric and e.quality == "valid":
            return e
    return None


def _duration(ms: int) -> str:
    minutes = round(ms / 60_000)
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def factual_fallback(snapshot: ContextSnapshot) -> ModelResponse | None:
    """Deterministic text from valid facts only; None when nothing valid remains."""
    facts = snapshot.facts
    parts: list[str] = []
    ids: list[str] = []
    schedule = _evidence(snapshot, "schedule_today")
    forecast = _evidence(snapshot, "forecast_next")
    temperature = _evidence(snapshot, "temperature")
    humidity = _evidence(snapshot, "humidity")

    def schedule_line():
        if schedule is None:
            return
        s = schedule.value
        line = f"{s['event_count']} events today, {_duration(s['busy_ms'])} scheduled"
        if s.get("next_event"):
            n = s["next_event"]
            line += f"; next is {n['title']} at {n['start_local']}"
        parts.append(line + ".")
        ids.append(schedule.evidence_id)

    def forecast_line():
        if forecast is None:
            return
        f = forecast.value
        if f.get("precipitation_probability") is not None:
            parts.append(f"Forecast for {f['location']}: {f['condition']}, "
                         f"{f['precipitation_probability']:.0f}% chance of rain.")
        else:
            parts.append(f"Forecast for {f['location']}: {f['condition']}.")
        ids.append(forecast.evidence_id)

    def indoor_line():
        bits = []
        if temperature is not None:
            bits.append(f"{temperature.value:.1f} °C")
            ids.append(temperature.evidence_id)
        if humidity is not None:
            bits.append(f"{humidity.value:.0f}% RH")
            ids.append(humidity.evidence_id)
        if bits:
            parts.append("Indoors " + ", ".join(bits) + ".")

    category = snapshot.category
    if category == MORNING:
        schedule_line()
        forecast_line()
        indoor_line()
    elif category == PREPARATION:
        event = _evidence(snapshot, "event")
        if event is not None:
            e = event.value
            where = f" in {e['location']}" if e.get("location") else ""
            parts.append(f"{e['title']} starts at {e['start_local']}{where}.")
            ids.append(event.evidence_id)
        rain = _evidence(snapshot, "forecast_event")
        if rain is not None and rain.value.get("precipitation_probability") is not None:
            parts.append(f"Rain chance around then: {rain.value['precipitation_probability']:.0f}%.")
            ids.append(rain.evidence_id)
    elif category == COMFORT:
        condition = facts.get("condition")
        metric = "temperature" if condition in ("hot", "cold") else "humidity"
        reading = temperature if metric == "temperature" else humidity
        preference = _evidence(snapshot, f"preferred_{metric}_range")
        if reading is not None and preference is not None:
            low, high = preference.value
            unit = "°C" if metric == "temperature" else "% RH"
            value = f"{reading.value:.1f}" if metric == "temperature" else f"{reading.value:.0f}"
            direction = "above" if condition in ("hot", "humid") else "below"
            parts.append(f"Room {metric} is {value} {unit}, {direction} your preferred "
                         f"{low:g}-{high:g} {unit} for over a minute.")
            ids += [reading.evidence_id, preference.evidence_id]
    elif category == CONTEXT_ANSWER:
        question = snapshot.question
        if question == "next_event":
            if schedule is not None and schedule.value.get("next_event"):
                n = schedule.value["next_event"]
                parts.append(f"Your next event is {n['title']} at {n['start_local']}.")
                ids.append(schedule.evidence_id)
            elif schedule is not None:
                parts.append("No more timed events today.")
                ids.append(schedule.evidence_id)
        elif question == "rain_today":
            forecast_line()
        elif question == "room_comfort":
            indoor_line()
        elif question == "schedule_today":
            schedule_line()
    if not parts:
        return None
    return ModelResponse(snapshot.request_id, snapshot.snapshot_id, category,
                         " ".join(parts[:MAX_SENTENCES]), tuple(dict.fromkeys(ids)),
                         execution_location="uno_q_linux", model_version=None,
                         runtime_version="factual_fallback", prompt_version=None)


# ------------------------------------------------------------------ service

@dataclass
class GenerationResult:
    snapshot: ContextSnapshot
    outcome: str  # generated | rejected | timed_out | cancelled | error
    response: ModelResponse | None
    reason: str | None
    started_at: str | None
    finished_at: str | None
    execution_location: str


ResultCallback = Callable[[GenerationResult], Awaitable[None]]


class RecommendationService:
    """One active request plus one coalesced waiting request (baseline section 6)."""

    def __init__(self, backend: ModelBackend | None = None, *, now: Callable[[], datetime],
                 timeout_s: float = GENERATION_TIMEOUT_S):
        self.backend = backend or UnavailableModel()
        self.timeout_s = timeout_s
        self._now = now
        self._active: tuple[ContextSnapshot, asyncio.Task] | None = None
        self._waiting: ContextSnapshot | None = None
        self._cancelled: set[str] = set()
        self._callback: ResultCallback | None = None
        self.last_outcome: dict | None = None

    def on_result(self, callback: ResultCallback) -> None:
        self._callback = callback

    @property
    def busy(self) -> bool:
        return self._active is not None

    def status(self) -> dict:
        return {
            "backend": type(self.backend).__name__,
            "execution_location": self.backend.execution_location,
            "model_version": self.backend.model_version,
            "active_request": self._active[0].request_id if self._active else None,
            "waiting_request": self._waiting.request_id if self._waiting else None,
            "last_outcome": self.last_outcome,
        }

    def submit(self, snapshot: ContextSnapshot) -> None:
        if self._active is None:
            self._start(snapshot)
        else:
            if self._waiting is not None:
                self._report_nowait(self._waiting, "cancelled", "coalesced")
            self._waiting = snapshot

    def cancel(self, request_id: str) -> None:
        """Late results for a cancelled request are discarded."""
        self._cancelled.add(request_id)
        if self._waiting and self._waiting.request_id == request_id:
            self._waiting = None
        if self._active and self._active[0].request_id == request_id:
            self._active[1].cancel()

    def _start(self, snapshot: ContextSnapshot) -> None:
        task = asyncio.get_running_loop().create_task(self._run(snapshot))
        self._active = (snapshot, task)

    def _report_nowait(self, snapshot: ContextSnapshot, outcome: str, reason: str) -> None:
        result = GenerationResult(snapshot, outcome, None, reason, None, iso(self._now()),
                                  self.backend.execution_location)
        if self._callback:
            asyncio.get_running_loop().create_task(self._callback(result))

    async def _run(self, snapshot: ContextSnapshot) -> None:
        started = iso(self._now())
        response = None
        try:
            if snapshot.request_id in self._cancelled:
                raise asyncio.CancelledError
            response = await asyncio.wait_for(self.backend.complete(snapshot), self.timeout_s)
            reason = validate_response(response, snapshot)
            outcome = "generated" if reason is None else "rejected"
        except asyncio.TimeoutError:
            outcome, reason = "timed_out", f"deadline_{self.timeout_s:g}s"
        except asyncio.CancelledError:
            outcome, reason = "cancelled", "cancelled"
        except ModelUnavailable as exc:
            outcome, reason = "error", str(exc)
        except Exception as exc:  # malformed output, runtime errors
            outcome, reason = "error", str(exc) or type(exc).__name__
        if snapshot.request_id in self._cancelled:
            outcome, reason = "cancelled", "cancelled"
        if outcome == "generated" and response is not None:
            response = replace(response, execution_location=self.backend.execution_location)
        result = GenerationResult(snapshot, outcome, response if outcome == "generated" else None,
                                  reason, started, iso(self._now()), self.backend.execution_location)
        self.last_outcome = {"request_id": snapshot.request_id, "outcome": outcome, "reason": reason,
                             "finished_at": result.finished_at}
        self._active = None
        waiting, self._waiting = self._waiting, None
        if waiting is not None:
            self._start(waiting)
        if self._callback:
            await self._callback(result)

    async def close(self) -> None:
        self._waiting = None
        if self._active:
            self._active[1].cancel()
            try:
                await self._active[1]
            except BaseException:
                pass
