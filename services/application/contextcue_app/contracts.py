"""SWE-side contracts (baseline section 5): ContextEvent, ContextSnapshot,
ModelResponse and Notice, plus the calendar/weather adapter result.

Hardware contracts (SensorSample, HealthState, DeviceCommand ...) stay in
`contextcue_contracts`. These types are application-owned; move them into that
package once the AI service needs to import them from a second app.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"

REAL = "real"
SIMULATED = "simulated"


class Lifecycle(str, Enum):
    CANDIDATE = "candidate"
    DEFERRED = "deferred"
    GENERATING = "generating"
    VALIDATING = "validating"
    FALLBACK = "fallback"
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    EXPIRED = "expired"


TERMINAL = frozenset({Lifecycle.ACKNOWLEDGED, Lifecycle.EXPIRED})

# Notice categories, matching ModelResponse categories (baseline section 5).
MORNING = "morning"
PREPARATION = "preparation"
COMFORT = "comfort"
CONTEXT_ANSWER = "context_answer"
CATEGORIES = (MORNING, PREPARATION, COMFORT, CONTEXT_ANSWER)

# Model request outcomes and notice origins are separate fields (section 5).
REQUEST_OUTCOMES = ("pending", "generated", "rejected", "timed_out", "cancelled", "error", "not_requested")
ORIGINS = ("generated", "cached", "factual_fallback", "status")


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    title: str
    start: str  # UTC ISO 8601, or a local date (YYYY-MM-DD) for all-day events
    end: str
    all_day: bool = False
    status: str = "confirmed"  # confirmed | tentative | cancelled
    location: str | None = None


@dataclass(frozen=True)
class WeatherPeriod:
    period_id: str
    start: str
    end: str
    condition: str  # sunny | cloudy | rainy | snowy | foggy (Brick categories)
    precipitation_probability: float | None  # percent, 0-100
    temperature_c: float | None
    location: str


@dataclass(frozen=True)
class SourceResult:
    """What every calendar/weather adapter returns, fixture or live."""

    source_id: str
    kind: str  # calendar | weather
    source_mode: str  # real | simulated
    quality: str  # valid | stale | unavailable | disabled | invalid
    error: str | None
    observed_at: str | None
    received_at: str
    valid_until: str | None
    label: str
    items: tuple[Any, ...] = ()


@dataclass(frozen=True)
class ContextEvent:
    """One piece of evidence. Revision changes only on material change."""

    evidence_id: str
    revision: int
    source_id: str
    source_type: str  # sensor | calendar | weather | derived | preference
    metric: str
    value: Any
    unit: str | None
    observed_at: str | None
    received_at: str | None
    valid_until: str | None
    freshness_ms: int | None
    source_mode: str
    quality: str
    provenance: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextSnapshot:
    request_id: str
    snapshot_id: str
    revision: int
    category: str
    reason: str
    evidence: tuple[ContextEvent, ...]
    facts: dict[str, Any]
    preferences: dict[str, Any]
    mode: str
    audio_policy: str
    created_at: str
    deadline: str
    question: str | None = None
    schema_version: str = SCHEMA_VERSION

    def evidence_revisions(self) -> dict[str, int]:
        return {e.evidence_id: e.revision for e in self.evidence}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelResponse:
    request_id: str
    snapshot_id: str
    category: str
    text: str
    evidence_ids: tuple[str, ...]
    execution_location: str | None = None
    model_version: str | None = None
    runtime_version: str | None = None
    prompt_version: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class RouteResult:
    route: str  # screen | pixels | buzzer | speech
    status: str  # permitted | blocked | sent | accepted | completed | rejected | failed | unavailable | stopped
    reason: str | None
    at: str
    command_id: str | None = None


@dataclass
class Notice:
    """Mutable record owned only by NotificationService."""

    notice_id: str
    revision: int
    dedupe_key: str
    category: str
    priority: int  # 2 = explicit request / event within 10 min, 1 = preparation, 0 = morning/comfort
    text: str
    evidence: dict[str, int]  # evidence ID -> revision
    required: list[str]  # evidence that must stay valid at the same revision
    created_at: str
    expires_at: str
    origin: str = "status"
    validation: str = "pending"  # pending | passed | failed: <reason> | not_applicable
    request_outcome: str = "pending"
    lifecycle: Lifecycle = Lifecycle.CANDIDATE
    source_mode: str = REAL
    explicit: bool = False
    important: bool = False
    deferred_reason: str | None = None
    acknowledged_at: str | None = None
    ended_reason: str | None = None
    generation: dict[str, Any] = field(default_factory=dict)
    routes: dict[str, RouteResult] = field(default_factory=dict)
    audio_attempts: int = 0
    announced: bool = False

    @property
    def terminal(self) -> bool:
        return self.lifecycle in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["lifecycle"] = self.lifecycle.value
        return data
