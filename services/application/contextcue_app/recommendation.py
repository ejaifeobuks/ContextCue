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
# G1 release deadline. Measured on the UNO Q (Qwen3.5-0.8B Q4_0, CPU, app
# prompts): p50 about 22 s, max 29 s with nothing else loading the board.
# 45 s leaves headroom for sensing and the UI; a later reply is discarded and
# the notice shows the labeled factual fallback.
GENERATION_TIMEOUT_S = 45.0
PROMPT_VERSION = "contextcue-prompt-2"

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


# Fixed instructions, sent first and unchanged on every request so the
# llama.cpp server can reuse their processed prefix: on the UNO Q the model
# reads input at about 12 tokens/s, so every reused token saves time.
SYSTEM_PROMPT = """You write short desk notices for ContextCue.
Use ONLY the facts in the snapshot JSON. Never invent times, numbers, names, places or events.
Do not do arithmetic: quote numbers exactly as they appear in the snapshot.
Reply with exactly one JSON object and nothing else: {"text": "<at most 2 short sentences>"}
Write like a helpful friend, not a report. Never state the current time. If facts.advice is present, include that advice in your own words.
forecast_next is the weather now; forecast_later is later today, from its "from" time.
If no_more_events_today is true, say there is nothing else on the calendar today.
morning: name next_event with its start time, then the weather, including rain later today.
preparation: name the event and its start time; use facts.minutes_until_event if present; mention rain only if rain_chance_pct is 50 or more.
comfort: give the reading and the preferred range.
context_answer: answer request.question in one or two sentences."""


def sky(period: dict) -> str | None:
    """Condition word for wording: a clear night is "clear", not "sunny"."""
    condition = period.get("condition")
    return "clear" if condition == "sunny" and period.get("night") else condition


def _drop_empty(value: dict) -> dict:
    return {k: v for k, v in value.items() if v not in (None, [], {}, "")}


def _compact(event) -> dict:
    """The fields a model needs from one evidence item, in plain local terms.

    The model only sees this view; validation still checks its reply against
    the full evidence, so dropping fields cannot loosen any check.
    """
    v, metric = event.value, event.metric
    if metric == "schedule_today" and isinstance(v, dict):
        def brief(e):
            return _drop_empty({"title": e.get("title"), "start": e.get("start_local"),
                                "location": e.get("location")}) if e else None
        nxt = v.get("next_event")
        current = v.get("current_events") or []
        remaining = v.get("remaining_events") or []
        later = [brief(e) for e in remaining
                 if not nxt or e.get("event_id") != nxt.get("event_id")][:2]
        # Only what is still ahead: a whole-day count read as "4 events today"
        # in the evening, after every one of them had ended.
        left = len(current) + len(remaining)
        return _drop_empty({"events_left_today": left, "no_more_events_today": left == 0 or None,
                            "now": [e.get("title") for e in current],
                            "next_event": brief(nxt), "later": later, "all_day": v.get("all_day")})
    if metric == "event" and isinstance(v, dict):
        return _drop_empty({"title": v.get("title"), "start": v.get("start_local"),
                            "location": v.get("location")})
    if metric.startswith("forecast") and isinstance(v, dict):
        # Only the later-today period gets times: the 0.8B model read the
        # current period's start ("from": "21:00") as the time of day.
        later = metric == "forecast_later"
        return _drop_empty({"from": v.get("start_local") if later else None,
                            "until": v.get("end_local") if later else None,
                            "condition": sky(v),
                            "rain_chance_pct": v.get("precipitation_probability"),
                            "temperature_c": v.get("temperature_c"), "location": v.get("location")})
    if metric.startswith("preferred_") and isinstance(v, (list, tuple)) and len(v) == 2:
        return {"low": v[0], "high": v[1], "unit": event.unit}
    if metric.startswith("comfort_"):
        return {"condition": v}
    if isinstance(v, float):
        return _drop_empty({"value": round(v, 1), "unit": event.unit})
    return _drop_empty({"value": v, "unit": event.unit})


# Facts that are only for code (raw milliseconds, duplicated event records).
# local_time is hidden too: no notice needs the current time, and the 0.8B
# model quoted it as an event time ("hot at 22:31") in board tests.
_HIDDEN_FACTS = {"event", "event_in_ms", "local_date", "local_time"}
# Evidence a notice type does not need in its wording. Preparation is about one
# event: with the whole day's schedule beside it the model left the event's
# name out. The evidence stays in the snapshot for Why and validation.
_HIDDEN_EVIDENCE = {PREPARATION: {"schedule_today"}}


def _model_evidence(snapshot: ContextSnapshot) -> list:
    hidden = _HIDDEN_EVIDENCE.get(snapshot.category, set())
    return [e for e in snapshot.evidence if e.quality == "valid" and e.metric not in hidden]


def model_view(snapshot: ContextSnapshot) -> dict:
    """The compact snapshot a model reads: request, code-calculated facts and
    evidence with the same IDs the reply must cite."""
    request = {"category": snapshot.category}
    if snapshot.question:
        request["question"] = SUPPORTED_QUESTIONS.get(snapshot.question, snapshot.question)
    facts = {k: v for k, v in snapshot.facts.items() if k not in _HIDDEN_FACTS}
    hidden = _HIDDEN_EVIDENCE.get(snapshot.category, set())
    evidence = [{"id": e.evidence_id, "type": e.metric, **_compact(e)}
                for e in _model_evidence(snapshot) if e.metric not in hidden]
    return _drop_empty({"request": request, "facts": facts, "evidence": evidence})


def response_schema(snapshot: ContextSnapshot) -> dict:
    """JSON schema for llama.cpp's grammar: only the notice text.

    The category is the request's and the evidence is what the model was
    shown, so code fills both in. Asking the model to echo them cost about 25
    output tokens per reply (8-12 s on the UNO Q CPU) and pushed briefings
    past the deadline. Every number in the text is still checked against
    that evidence."""
    return {
        "type": "object",
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": MAX_CHARS}},
        "required": ["text"],
        "additionalProperties": False,
    }


def model_evidence_ids(snapshot: ContextSnapshot) -> tuple[str, ...]:
    return tuple(e.evidence_id for e in _model_evidence(snapshot))


class LlamaCppModel:
    """OpenAI-compatible chat completion against the on-board llama.cpp runner
    (the `arduino:llm` Brick).

    No tools, no memory: each request sees only its own compact snapshot.
    With `structured=True` (default) the reply is constrained by a JSON schema.
    """

    execution_location = "uno_q_linux"

    def __init__(self, base_url: str = "http://127.0.0.1:9999/v1", model: str | None = None,
                 max_tokens: int = 100, timeout_s: float = GENERATION_TIMEOUT_S,
                 structured: bool = True):
        self.base_url = base_url.rstrip("/")
        self.model_version = model
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.structured = structured

    def _prompt(self, snapshot: ContextSnapshot) -> list[dict]:
        user = json.dumps(model_view(snapshot), separators=(",", ":"), default=str)
        return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    def _request(self, path: str, body: dict | None, timeout: float) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def _ensure_model(self) -> None:
        # The Brick serves exactly one model and requires its name per request.
        if self.model_version is None:
            models = self._request("/models", None, 10).get("data") or []
            if not models:
                raise ModelUnavailable("model_not_loaded")
            self.model_version = models[0]["id"]

    def _call(self, snapshot: ContextSnapshot) -> str:
        self._ensure_model()
        # Greedy decoding: at temperature 0.2 the 0.8B model occasionally
        # sampled stray tokens from other scripts mid-sentence (G1 benchmark).
        body = {"model": self.model_version, "messages": self._prompt(snapshot),
                "max_tokens": self.max_tokens, "temperature": 0}
        if self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "contextcue_notice", "schema": response_schema(snapshot)}}
        else:
            body["response_format"] = {"type": "json_object"}
        payload = self._request("/chat/completions", body, self.timeout_s)
        return payload["choices"][0]["message"]["content"]

    def _warm(self) -> None:
        self._ensure_model()
        self._request("/chat/completions", {
            "model": self.model_version, "max_tokens": 1,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": "{}"}]}, 120)

    async def warm_up(self) -> None:
        """Load the model and process the fixed instructions once, so the
        first real request does not pay for them. Failures are left to the
        first real request to report."""
        try:
            await asyncio.to_thread(self._warm)
        except Exception:
            pass

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
        return ModelResponse(
            request_id=snapshot.request_id, snapshot_id=snapshot.snapshot_id,
            category=snapshot.category, text=str(data.get("text", "")),
            evidence_ids=model_evidence_ids(snapshot),
            execution_location=self.execution_location, model_version=self.model_version,
            runtime_version="llamacpp-runner", prompt_version=PROMPT_VERSION,
            started_at=started, finished_at=iso(datetime.now().astimezone()),
        )


# ---------------------------------------------------------------- validation

_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")
# Plain English notice text: Latin letters (with accents), digits, common
# punctuation, degree sign and dashes. Anything else is a stray model token.
_PLAIN_TEXT = re.compile(r"[A-Za-z\u00C0-\u024F0-9\s.,;:!?'\"()%°/&+\-\u2013\u2014\u2019]*")


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


def _number_key(text: str) -> str:
    """Compare numbers by value: "20", "20.0" and "020" are the same claim."""
    try:
        return format(float(text), "g")
    except ValueError:
        return text


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
    if not _PLAIN_TEXT.fullmatch(text):
        return "unexpected_characters"
    available = {e.evidence_id for e in snapshot.evidence if e.quality == "valid"}
    referenced = set(response.evidence_ids)
    if not referenced:
        return "no_evidence"
    if not referenced <= available:
        return "unknown_or_invalid_evidence"
    allowed = {_number_key(n) for n in _allowed_numbers(snapshot, referenced)}
    for number in _NUMBER.findall(text):
        if _number_key(number) not in allowed:
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
    later = _evidence(snapshot, "forecast_later")
    temperature = _evidence(snapshot, "temperature")
    humidity = _evidence(snapshot, "humidity")

    def schedule_line(whole_day: bool = False):
        if schedule is None:
            return
        s = schedule.value
        left = len(s.get("current_events") or []) + len(s.get("remaining_events") or [])
        demo = "Demo calendar: " if schedule.source_mode == "simulated" else ""
        if whole_day:  # "How busy is today?"
            line = f"{demo}{s['event_count']} events today, {_duration(s['busy_ms'])} scheduled"
            line += f", {left} still to come" if left else ", none still to come"
        elif left == 0:
            line = f"{demo}No more events today"
        else:
            line = f"{demo}{left} more {'event' if left == 1 else 'events'} today"
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
            line = (f"Forecast for {f['location']}: {sky(f)}, "
                    f"{f['precipitation_probability']:.0f}% chance of rain")
        else:
            line = f"Forecast for {f['location']}: {sky(f)}"
        ids.append(forecast.evidence_id)
        if later is not None and (later.value.get("precipitation_probability") or 0) > \
                (f.get("precipitation_probability") or 0):
            line += (f"; {later.value['precipitation_probability']:.0f}% from "
                     f"{later.value['start_local']}")
            ids.append(later.evidence_id)
        tips = facts.get("advice") or []
        parts.append(line + (f". {tips[0]}." if tips else "."))

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
            schedule_line(whole_day=True)
    if parts and category in (PREPARATION, COMFORT) and facts.get("advice"):
        parts.append(f"{facts['advice'][0]}.")
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
