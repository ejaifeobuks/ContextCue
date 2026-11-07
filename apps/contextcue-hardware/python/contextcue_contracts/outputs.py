"""SWE-to-HW output contracts (baseline section 5: DeviceCommand, CommandResult).

The MCU enforces the same allowlist and bounds independently; these values
must match `sketch/src/Config.h` and `sketch/src/outputs/OutputController.cpp`.
"""

from dataclasses import dataclass, field
from typing import Any

# Allowlisted (target, action) pairs and the parameters each one takes.
OUTPUT_ACTIONS: dict[tuple[str, str], frozenset[str]] = {
    ("pixels", "set_pattern"): frozenset({"pattern", "brightness"}),
    ("pixels", "off"): frozenset(),
    ("buzzer", "short_tone"): frozenset({"frequency_hz", "duration_ms"}),
    ("buzzer", "stop"): frozenset(),
    ("outputs", "stop"): frozenset(),
}
PIXEL_PATTERNS = ("ready", "notice", "fault", "off")
MIN_TONE_FREQUENCY_HZ = 200
MAX_TONE_FREQUENCY_HZ = 5000
MAX_TONE_DURATION_MS = 250
MAX_COMMAND_TTL_MS = 2000
STANDARD_COMMAND_TTL_MS = 2000
OUTPUT_LEASE_MS = 3000
HEARTBEAT_INTERVAL_MS = 1000  # renew well inside the lease

# Command outcomes. Acceptance is not completion, and driver completion is not
# independent physical confirmation (baseline section 5).
ACCEPTED = "accepted"
COMPLETED = "completed"
REJECTED = "rejected"
FAILED = "failed"
EXPIRED = "expired"


@dataclass(frozen=True)
class DeviceCommand:
    schema_version: str
    session_id: str
    command_id: str
    target: str
    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    ttl_ms: int = STANDARD_COMMAND_TTL_MS  # remaining TTL when handed to the gateway
    notice_id: str | None = None
    notice_revision: int | None = None


@dataclass(frozen=True)
class CommandResult:
    schema_version: str
    command_id: str
    status: str
    reason: str | None
    at: str  # UTC ISO 8601


@dataclass(frozen=True)
class OutputState:
    """Latest MCU output report. `last_result` is the most recent applied,
    failed or expired command; earlier outcomes are not retained."""

    schema_version: str
    boot_id: str
    pixels_available: bool
    buzzer_available: bool
    pattern: str
    brightness: float  # 0-1
    tone_active: bool
    lease_active: bool
    last_result: CommandResult | None
