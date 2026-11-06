"""HW-to-SWE contracts (baseline section 5), independent of Arduino imports."""

from dataclasses import asdict, dataclass
from typing import Any

from .common import Quality


@dataclass(frozen=True)
class SensorSample:
    schema_version: str
    event_id: str
    boot_id: str
    seq: int
    module_id: str
    metric: str
    value: float | None
    unit: str
    observed_uptime_ms: int
    observed_at: str
    received_at: str
    clock_quality: str
    clock_uncertainty_ms: float
    source_mode: str
    quality: Quality
    error: str | None


@dataclass(frozen=True)
class HardwareCapabilities:
    schema_version: str
    boot_id: str
    firmware_version: str
    configured_modules: tuple[str, ...]
    available_modules: tuple[str, ...]
    sensor_driver: str
    units: dict[str, str]
    versions: dict[str, str]
    supported_operations: tuple[str, ...]
    output_patterns: tuple[str, ...] = ()
    native_brightness: bool = False
    audio_adapter_version: str | None = None


@dataclass(frozen=True)
class HealthState:
    component_id: str
    state: str
    reason: str | None
    last_success_at: str | None
    execution_location: str = "uno_q_linux"


@dataclass(frozen=True)
class GatewayState:
    schema_version: str
    boot_id: str | None
    samples: tuple[SensorSample, ...]
    health: tuple[HealthState, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
