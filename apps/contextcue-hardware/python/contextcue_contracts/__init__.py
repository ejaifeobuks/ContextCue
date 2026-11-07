"""Application-facing ContextCue contracts (software baseline section 5).

They live inside this App Lab app because an app must be self-contained.
When a second service needs them, publish this package and list it in each
app's requirements.txt rather than copying it.
"""

from .common import SCHEMA_VERSION, Quality
from .freshness import (
    BRIDGE_UNAVAILABLE_MS,
    DISTANCE_STALE_MS,
    LIGHT_STALE_MS,
    THERMO_STALE_MS,
)
from .hardware import GatewayState, HardwareCapabilities, HealthState, SensorSample
from .outputs import (
    HEARTBEAT_INTERVAL_MS,
    OUTPUT_LEASE_MS,
    PIXEL_PATTERNS,
    STANDARD_COMMAND_TTL_MS,
    CommandResult,
    DeviceCommand,
    OutputState,
)

__all__ = [
    "BRIDGE_UNAVAILABLE_MS",
    "HEARTBEAT_INTERVAL_MS",
    "OUTPUT_LEASE_MS",
    "PIXEL_PATTERNS",
    "STANDARD_COMMAND_TTL_MS",
    "CommandResult",
    "DeviceCommand",
    "OutputState",
    "DISTANCE_STALE_MS",
    "LIGHT_STALE_MS",
    "SCHEMA_VERSION",
    "THERMO_STALE_MS",
    "GatewayState",
    "HardwareCapabilities",
    "HealthState",
    "Quality",
    "SensorSample",
]
