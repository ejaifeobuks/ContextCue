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
# prompts, kiosk running): p50 about 21 s, max 31 s on the demo calendar. A
# real calendar briefing (12 November) took about 45 s: about 250 new input
# tokens at 10 tokens/s plus about 60 output tokens at 2.3 tokens/s. 60 s
# leaves headroom; a later reply is discarded and the notice shows the
# labeled factual fallback.
GENERATION_TIMEOUT_S = 60.0
PROMPT_VERSION = "contextcue-prompt-4"

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
Write times exactly as given, like 9:00 AM. Write rain chances as whole percentages.
forecast_next is the weather now; forecast_later is later today, from its "from" time.
If no_more_events_today is true, say there is nothing else on the calendar today.
briefing: in under 25 words, name next_event with its start time, then the weather, including rain later today.
preparation: name the event and its start time; use facts.minutes_until_event if present; mention rain only if rain_chance_pct is 50 or more.
comfort: give the reading and the preferred range.
context_answer: answer request.question in one or two sentences."""


def clock12(hhmm) -> str | None:
    """ "19:00" -> "7:00 PM", as the screen shows it. The 0.8B model copied
    24-hour times into otherwise friendly sentences. Other values pass through."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(hhmm or ""))
    if not match:
        return hhmm
    hour = int(match[1])
    return f"{(hour + 11) % 12 + 1}:{match[2]} {'AM' if hour < 12 else 'PM'}"


def _pct(value):
    """Whole percent: the model wrote "1.0%" when given 1.0."""
    return round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


def sky(period: dict) -> str | None:
    """Condition word for wording: a clear night is "clear", not "sunny"."""
    condition = period.get("condition")
    return "clear" if condition == "sunny" and period.get("night") else condition


def _drop_empty(value: dict) -> dict:
    return {k: v for k, v in value.items() if v not in (None, [], {}, "")}


def _compact(event, brief: bool = False) -> dict:
    """The fields a model needs from one evidence item, in plain local terms.

    The model only sees this view; validation still checks its reply against
    the full evidence, so dropping fields cannot loosen any check. `brief`
    (briefings) keeps only the next event: every input token costs about
    0.1 s on the UNO Q, and a briefing does not mention the rest.
    """
    v, metric = event.value, event.metric
    if metric == "schedule_today" and isinstance(v, dict):
        def item(e):
            return _drop_empty({"title": e.get("title"), "start": clock12(e.get("start_local")),
                                "location": None if brief else e.get("location")}) if e else None
        nxt = v.get("next_event")
        current = v.get("current_events") or []
        remaining = v.get("remaining_events") or []
        later = [] if brief else [item(e) for e in remaining
                                  if not nxt or e.get("event_id") != nxt.get("event_id")][:2]
        # Only what is still ahead: a whole-day count read as "4 events today"
        # in the evening, after every one of them had ended.
        left = len(current) + len(remaining)
        return _drop_empty({"events_left_today": left, "no_more_events_today": left == 0 or None,
                            "now": [e.get("title") for e in current],
                            "next_event": item(nxt), "later": later, "all_day": v.get("all_day")})
    if metric == "event" and isinstance(v, dict):
        return _drop_empty({"title": v.get("title"), "start": clock12(v.get("start_local")),
                            "location": v.get("location")})
    if metric.startswith("forecast") and isinstance(v, dict):
        # Only the later-today period gets times: the 0.8B model read the
        # current period's start ("from": "21:00") as the time of day.
        # No place name: next to an event the model wrote "Team stand-up at
        # 9:00 in Huntsville, Alabama". The screen already shows the place.
        later = metric == "forecast_later"
        return _drop_empty({"from": clock12(v.get("start_local")) if later else None,
                            "until": clock12(v.get("end_local")) if later else None,
                            "condition": sky(v),
                            "rain_chance_pct": _pct(v.get("precipitation_probability")),
                            "temperature_c": v.get("temperature_c")})
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
# Briefings cover the next event and the weather only: the room readings were
# never in the wording, but a room drift of 0.5 °C during the ~30 s generation
# made the reply obsolete and threw it away (board, 16 November). The factual
# fallback still shows them.
_HIDDEN_EVIDENCE = {PREPARATION: {"schedule_today"}, MORNING: {"temperature", "humidity"}}


def _model_evidence(snapshot: ContextSnapshot) -> list:
    hidden = _HIDDEN_EVIDENCE.get(snapshot.category, set())
    return [e for e in snapshot.evidence if e.quality == "valid" and e.metric not in hidden]


def model_view(snapshot: ContextSnapshot) -> dict:
    """The compact snapshot a model reads: request, code-calculated facts and
    evidence with the same IDs the reply must cite."""
    # "briefing", not "morning": the user can ask for one at any hour, and the
    # model greeted "Good morning" in the evening.
    request = {"category": "briefing" if snapshot.category == MORNING else snapshot.category}
    if snapshot.question:
        request["question"] = SUPPORTED_QUESTIONS.get(snapshot.question, snapshot.question)
    facts = {k: v for k, v in snapshot.facts.items() if k not in _HIDDEN_FACTS}
    hidden = _HIDDEN_EVIDENCE.get(snapshot.category, set())
    brief = snapshot.category == MORNING
    evidence = [{"id": e.evidence_id, "type": e.metric, **_compact(e, brief)}
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
        choice = payload["choices"][0]
        # A reply cut off at max_tokens is a runaway, not a notice: on the board
        # one such reply drifted into stray words ("11:0 abajo ... iszlich")
        # that the number and character checks cannot see.
        if choice.get("finish_reason") == "length":
            raise ValueError("truncated_model_output")
        return choice["message"]["content"]

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
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# Plain English notice text: Latin letters (with accents), digits, common
# punctuation, degree sign and dashes. Anything else is a stray model token.
_PLAIN_TEXT = re.compile(r"[A-Za-z\u00C0-\u024F0-9\s.,;:!?'\"()%°/&+\-\u2013\u2014\u2019]*")
# A stray token glued after punctuation (",orea"), or a phrase repeated in a
# runaway reply: both passed the character and number checks on the board.
_GLUED = re.compile(r"[,;!?][A-Za-z]")
_REPEAT_WORDS = 5
_COMFORTABLE = re.compile(r"\bcomfortable\b", re.I)
_NEGATED = re.compile(r"(not|n't|less|un)\s*(very\s+)?$", re.I)


def _allowed(snapshot: ContextSnapshot, referenced: set[str]) -> tuple[set[str], set[tuple[int, str]]]:
    """Numbers and clock times the reply may contain: only what the model was
    shown (model_view), so a time's minutes ("10:00" -> "0") or an ISO date
    hidden from the model cannot excuse an invented number."""
    numbers: set[str] = set()
    times: set[tuple[int, str]] = set()

    def add(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            for text in (f"{value:.0f}", f"{value:.1f}", str(value)):
                numbers.add(text.rstrip("0").rstrip(".") if "." in text else text)
        elif isinstance(value, str):
            for hour, minute in _TIME.findall(value):
                h = int(hour)
                # "19:00" may be written "7:00 PM" and "9:00 AM" as "9 AM".
                for form in (h, (h + 11) % 12 + 1):
                    times.add((form, minute))
                    numbers.add(str(form))
            for match in _NUMBER.findall(_TIME.sub(" ", value)):
                numbers.add(match.lstrip("0") or "0")
                numbers.add(match)
        elif isinstance(value, dict):
            for item in value.values():
                add(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    view = model_view(snapshot)
    for item in view.get("evidence", []):
        if item["id"] in referenced:
            add({k: v for k, v in item.items() if k not in ("id", "type")})
    add(view.get("facts", {}))
    return numbers, times


# Meaning checks (security review CC-05). The number checks above accept any
# number the model was shown, wherever it appears in the reply: "70% chance of
# rain now and 10% later" passed with 10% now and 70% later. These tie a
# number to what it measures: its unit, its forecast period and AM/PM.
_PERCENT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?=\s*(?:%|percent\b))", re.I)
_DEGREES = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)(?=\s*(?:°|deg))", re.I)
# "7 PM", "7:30pm", "7:30 a.m." in the reply; "7:30 PM" or "19:30" in the view.
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.I)
_VIEW_CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})(?:\s*([AP])M\b)?")
# A clause names one period; "10% now and 70% later" is two clauses.
_CLAUSE = re.compile(r"[,;]|\s+(?:and|but|while|then|with|though|although)\s+|\s[-–—]\s", re.I)
_NOW = re.compile(r"\b(?:right now|now|currently|at the moment|at present|for now)\b", re.I)
_LATER = re.compile(r"\b(?:later|this afternoon|this evening|tonight|overnight)\b|"
                    r"\b(?:from|until|after)\s+\d", re.I)
_PERIOD_OF = {"forecast_next": "now", "forecast_later": "later"}


def _forms(value) -> set[str]:
    """Number keys a value may be written as ("22.46" -> 22, 22.5, 22.46)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return set()
    return {_number_key(f"{value:.0f}"), _number_key(f"{value:.1f}"), _number_key(str(value))}


def _hour24(hour: str, meridiem: str | None) -> int:
    h = int(hour)
    if not meridiem:
        return h
    return h % 12 + (12 if meridiem.lower() == "p" else 0)


def _semantic_problem(text: str, snapshot: ContextSnapshot, referenced: set[str]) -> str | None:
    view = model_view(snapshot)
    items = [e for e in view.get("evidence", []) if e["id"] in referenced]
    percents: set[str] = set()
    temperatures: set[str] = set()
    indoor_temperatures: set[str] = set()
    periods: dict[str, dict[str, set[str]]] = {}
    clocks: set[tuple[int, str]] = set()

    def times_in(value):
        if isinstance(value, str):
            for hour, minute, meridiem in _VIEW_CLOCK.findall(value):
                clocks.add((_hour24(hour, meridiem), minute))
        elif isinstance(value, dict):
            for v in value.values():
                times_in(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                times_in(v)

    for item in items:
        kind = item.get("type", "")
        times_in({k: v for k, v in item.items() if k not in ("id", "type")})
        if kind.startswith("forecast"):
            rain, temp = _forms(item.get("rain_chance_pct")), _forms(item.get("temperature_c"))
            percents |= rain
            temperatures |= temp
            if kind in _PERIOD_OF:
                periods[_PERIOD_OF[kind]] = {"rain": rain, "temp": temp}
        elif kind in ("humidity", "temperature"):
            forms = _forms(item.get("value"))
            (percents if kind == "humidity" else indoor_temperatures).update(forms)
        elif kind in ("preferred_humidity_range", "preferred_temperature_range"):
            forms = _forms(item.get("low")) | _forms(item.get("high"))
            (percents if kind == "preferred_humidity_range" else indoor_temperatures).update(forms)
    temperatures |= indoor_temperatures
    facts = view.get("facts", {})
    percents |= _forms(facts.get("rain_probability"))
    times_in(facts)

    # Units: a percentage must be a chance or a humidity, a degree a temperature.
    for number in _PERCENT.findall(text):
        if _number_key(number) not in percents:
            return f"wrong_quantity:{number}%"
    for number in _DEGREES.findall(text):
        if _number_key(number) not in temperatures:
            return f"wrong_quantity:{number}deg"

    # AM/PM: "7:30 AM" for a 19:30 event passes the hour-and-minute check.
    for hour, minute, meridiem in _CLOCK.findall(text):
        if (_hour24(hour, meridiem), minute or "00") not in clocks:
            return f"unsupported_time:{hour}:{minute or '00'} {meridiem.upper()}M"

    # Periods: a forecast number in a clause that says "now" or "later" must be
    # that period's. Clauses without a period word are left to the checks above.
    for clause in _CLAUSE.split(text):
        markers = [(m.start(), m.end(), "now") for m in _NOW.finditer(clause)]
        markers += [(m.start(), m.end(), "later") for m in _LATER.finditer(clause)]
        if not markers:
            continue
        numbers = [] if re.search(r"humid", clause, re.I) else \
            [(m, "rain") for m in _PERCENT.finditer(clause)]
        numbers += [(m, "temp") for m in _DEGREES.finditer(clause)
                    if _number_key(m.group(1)) not in indoor_temperatures]
        for match, quantity in numbers:
            distance = {}
            for start, end, period in markers:
                gap = start - match.end() if start >= match.end() else match.start() - end
                distance[period] = min(gap, distance.get(period, gap))
            nearest = sorted(distance.items(), key=lambda kv: kv[1])
            if len(nearest) > 1 and nearest[0][1] == nearest[1][1]:
                continue  # equally close to both: leave it
            period = nearest[0][0]
            expected = periods.get(period, {}).get(quantity)
            if expected and _number_key(match.group(1)) not in expected:
                return f"wrong_period:{match.group(1)} {period}"
    return None


def _number_key(text: str) -> str:
    """Compare numbers by value: "20", "20.0" and "020" are the same claim."""
    try:
        return format(float(text), "g")
    except ValueError:
        return text


def _out_of_range(snapshot: ContextSnapshot) -> bool:
    """The code already knows the room is outside the preferred range."""
    if snapshot.facts.get("condition") in ("hot", "cold", "humid", "dry"):
        return True
    for metric in ("temperature", "humidity"):
        reading = _evidence(snapshot, metric)
        preference = _evidence(snapshot, f"preferred_{metric}_range")
        if reading is not None and preference is not None:
            low, high = preference.value
            if not low <= reading.value <= high:
                return True
    return False


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
    if not _PLAIN_TEXT.fullmatch(text) or _GLUED.search(text):
        return "unexpected_characters"
    words = re.findall(r"[a-z0-9']+", text.lower())
    phrases = [tuple(words[i:i + _REPEAT_WORDS]) for i in range(len(words) - _REPEAT_WORDS + 1)]
    if len(phrases) != len(set(phrases)):
        return "repetitive_text"
    available = {e.evidence_id for e in snapshot.evidence if e.quality == "valid"}
    referenced = set(response.evidence_ids)
    if not referenced:
        return "no_evidence"
    if not referenced <= available:
        return "unknown_or_invalid_evidence"
    numbers, times = _allowed(snapshot, referenced)
    for hour, minute in _TIME.findall(text):
        if (int(hour), minute) not in times:
            return f"unsupported_time:{hour}:{minute}"
    allowed = {_number_key(n) for n in numbers}
    for number in _NUMBER.findall(_TIME.sub(" ", text)):
        if _number_key(number) not in allowed:
            return f"unsupported_number:{number}"
    problem = _semantic_problem(text, snapshot, referenced)
    if problem:
        return problem
    if _out_of_range(snapshot):
        for match in _COMFORTABLE.finditer(text):
            if not _NEGATED.search(text[:match.start()]):
                return "contradicts_facts:comfortable"
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
            line += f"; next is {n['title']} at {clock12(n['start_local'])}"
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
                     f"{clock12(later.value['start_local'])}")
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
            parts.append(f"{e['title']} starts at {clock12(e['start_local'])}{where}.")
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
                parts.append(f"Your next event is {n['title']} at {clock12(n['start_local'])}.")
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


def factual_only_reason(snapshot: ContextSnapshot) -> str | None:
    """Requests answered from facts without the model. With nothing left on
    the calendar, every model setup tested invented a next event ("Next event
    is a meeting at 10:00 AM"); the facts say it plainly."""
    if snapshot.category == MORNING:
        schedule = _evidence(snapshot, "schedule_today")
        if schedule is not None and not (schedule.value.get("current_events")
                                         or schedule.value.get("remaining_events")):
            return "factual_only:no_events_left"
    return None


# ------------------------------------------------------------------ service

@dataclass
class GenerationResult:
    snapshot: ContextSnapshot
    outcome: str  # generated | rejected | skipped | timed_out | cancelled | error
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
            skip = factual_only_reason(snapshot)
            if skip:
                outcome, reason = "skipped", skip
            else:
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
