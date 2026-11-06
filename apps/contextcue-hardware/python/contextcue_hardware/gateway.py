"""HardwareGateway: bounded RPC, strict decoding, observation age and recovery."""

import asyncio
import json
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from contextcue_contracts import (
    BRIDGE_UNAVAILABLE_MS,
    DISTANCE_STALE_MS,
    LIGHT_STALE_MS,
    SCHEMA_VERSION,
    THERMO_STALE_MS,
    GatewayState,
    HardwareCapabilities,
    HealthState,
    Quality,
    SensorSample,
)

from .transport import ArduinoBridgeTransport, BridgeTransport


class ProtocolError(ValueError):
    """The peer's response cannot be safely used as hardware evidence."""


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class _ModuleTracker:
    samples: tuple[SensorSample, ...] = ()
    observed_monotonic: float | None = None
    last_seq: int | None = None
    last_uptime: int | None = None
    fingerprint: tuple | None = None
    health: HealthState | None = None


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ProtocolError(f"Invalid text field: {key}")
    return value


def _integer(data: dict, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or not 0 <= value <= (2**63 - 1):
        raise ProtocolError(f"Invalid nonnegative integer: {key}")
    return value


def _decode(raw: object) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 224:
        raise ProtocolError("Expected a compact JSON response of at most 224 bytes")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("Malformed MCU JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError("Expected an MCU object")
    version = _text(data, "v").split(".")
    if len(version) != 2 or not all(p.isdigit() for p in version) or version[0] != "1":
        raise ProtocolError("Incompatible MCU schema version")
    return data


class HardwareGateway:
    """Call from one asyncio event loop; blocking Bridge I/O runs in a worker.

    Sensor modules are polled independently so one unavailable module does not
    hide valid data from the others. Output and control-event APIs will be
    added with their firmware implementations, rather than pretend success.
    """

    def __init__(
        self,
        transport: BridgeTransport | None = None,
        *,
        clock: Clock | None = None,
        rpc_timeout: float = 0.75,
    ):
        if not math.isfinite(rpc_timeout) or not 0 < rpc_timeout <= 1:
            raise ValueError("rpc_timeout must be between 0 and 1 second")
        self._transport = transport if transport is not None else ArduinoBridgeTransport()
        self._clock = clock if clock is not None else Clock()
        self._timeout = rpc_timeout
        self._lock = asyncio.Lock()
        self._capabilities: HardwareCapabilities | None = None
        self._modules = {
            "thermo": _ModuleTracker(),
            "light": _ModuleTracker(),
            "distance": _ModuleTracker(),
        }
        self._last_success: str | None = None
        self._last_success_monotonic: float | None = None
        self._health = HealthState("bridge", "starting", "Not connected", None)
        self._needs_discovery = True
        self._closed = False

    async def _rpc(self, method: str, *args: object) -> dict:
        raw = await asyncio.to_thread(
            self._transport.call, method, *args, timeout=self._timeout
        )
        return _decode(raw)

    async def _discover(self) -> HardwareCapabilities:
        await asyncio.to_thread(self._transport.connect, self._timeout)
        # Offer a fresh token each handshake. A running MCU retains its token;
        # a restarted MCU adopts this new one instead of reusing its old epoch.
        info = await self._rpc("cc_open", uuid4().hex)
        boot = _text(info, "boot")
        driver = _text(info, "driver")
        if driver not in {"none", "modulino"} or type(info.get("ok")) is not bool:
            raise ProtocolError("Invalid Thermo capabilities")
        same_boot = self._capabilities is not None and self._capabilities.boot_id == boot
        available_modules = (
            tuple(
                module for module in ("thermo", "light", "distance")
                if self._modules[module].health is not None
                and self._modules[module].health.state == "ready"
            )
            if same_boot else
            (("thermo",) if info["ok"] and driver != "none" else ())
        )
        capabilities = HardwareCapabilities(
            schema_version=SCHEMA_VERSION,
            boot_id=boot,
            firmware_version=_text(info, "fw"),
            configured_modules=("thermo", "light", "distance"),
            available_modules=available_modules,
            sensor_driver=driver,
            units={"temperature": "degC", "humidity": "%RH"},
            versions={key: _text(info, key) for key in ("core", "bridge", "modulino", "hs", "sht")},
            supported_operations=(
                "get_capabilities", "read_state", "get_device_health",
            ),
        )
        if self._capabilities is None or self._capabilities.boot_id != boot:
            self._modules = {
                "thermo": _ModuleTracker(),
                "light": _ModuleTracker(),
                "distance": _ModuleTracker(),
            }
        self._capabilities = capabilities
        self._needs_discovery = False
        return capabilities

    async def get_capabilities(self) -> HardwareCapabilities:
        async with self._lock:
            self._ensure_open()
            try:
                return await self._discover()
            except Exception as exc:
                self._mark_failed(exc)
                raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HardwareGateway is closed")

    def _mark_failed(self, exc: Exception) -> None:
        self._needs_discovery = True
        self._health = HealthState("bridge", "unavailable", str(exc), self._last_success)
        # _state() hides cached values while unavailable. Retain the original
        # identity/fingerprint internally so recovery cannot bless changed data
        # under an already-seen sequence or refresh its observation timestamp.

    async def read_state(self) -> GatewayState:
        async with self._lock:
            self._ensure_open()
            try:
                if self._needs_discovery:
                    await self._discover()
                reads = (
                    ("thermo", "cc_read"),
                    ("light", "cc_read_light"),
                    ("distance", "cc_read_distance"),
                )
                failures = 0
                for module_id, method in reads:
                    if not await self._read_module(module_id, method):
                        failures += 1
                end = self._clock.monotonic()
                if failures == len(reads):
                    raise ProtocolError("All sensor RPCs failed")
                self._last_success = _timestamp(self._clock.utcnow())
                self._last_success_monotonic = end
                self._health = HealthState("bridge", "ready", None, self._last_success)
            except Exception as exc:
                self._mark_failed(exc)
            return self._state()

    async def _read_module(self, module_id: str, method: str) -> bool:
        start = self._clock.monotonic()
        try:
            data = await self._rpc(method)
            end = self._clock.monotonic()
            received = self._clock.utcnow()
            if data.get("boot") != self._capabilities.boot_id:
                    # A firmware restart loses the RAM boot token. Handshake again;
                    # never attach old samples to the new MCU instance.
                self._needs_discovery = True
                await self._discover()
                start = self._clock.monotonic()
                data = await self._rpc(method)
                end = self._clock.monotonic()
                received = self._clock.utcnow()
            self._ingest_module(module_id, data, start, end, received)
            return True
        except Exception as exc:
            tracker = self._modules[module_id]
            tracker.health = HealthState(
                module_id, "unavailable", str(exc),
                tracker.samples[0].received_at if tracker.samples else None,
                "uno_q_mcu",
            )
            return False

    def _ingest_module(
        self, module_id: str, data: dict, start: float, end: float,
        received: datetime,
    ) -> None:
        tracker = self._modules[module_id]
        boot = _text(data, "boot")
        if boot != self._capabilities.boot_id:
            raise ProtocolError("MCU reset during handshake")
        seq = _integer(data, "seq")
        uptime = _integer(data, "up")
        observed = _integer(data, "at")
        if observed > uptime:
            raise ProtocolError("Observation is ahead of MCU uptime")
        if tracker.last_uptime is not None and uptime < tracker.last_uptime:
            raise ProtocolError(f"{module_id} uptime regressed within one boot")
        if tracker.last_seq is not None and seq < tracker.last_seq:
            raise ProtocolError(f"{module_id} sequence regressed within one boot")
        try:
            quality = Quality(data["q"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolError(f"Invalid {module_id} sensor quality") from exc
        if quality == Quality.STALE:
            raise ProtocolError("MCU reports validity; gateway determines staleness")
        error = data.get("err")
        if error is not None and (not isinstance(error, str) or not error or len(error) > 64):
            raise ProtocolError("Invalid sensor error")
        if module_id == "thermo":
            values = (data.get("t"), data.get("h"))
            units = ("degC", "%RH")
            metrics = ("temperature", "humidity")
            driver = _text(data, "d")
            if driver not in {"none", "modulino"}:
                raise ProtocolError("Unknown Thermo driver")
            if quality == Quality.VALID:
                for value, lo, hi in zip(values, (-40, 0), (125, 100)):
                    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
                        raise ProtocolError("Invalid Thermo measurement")
                if error is not None or seq == 0 or driver == "none":
                    raise ProtocolError("Valid sample has an error or no acquisition")
            elif values != (None, None) or error is None:
                raise ProtocolError("Failed measurements must have null values and a reason")
        elif module_id == "light":
            values, units, metrics = (data.get("lux"),), ("lux",), ("ambient_light",)
            if quality == Quality.VALID:
                if type(values[0]) not in (int, float) or not math.isfinite(values[0]) or not 0 <= values[0] <= 1_000_000:
                    raise ProtocolError("Invalid Light measurement")
                if error is not None or seq == 0:
                    raise ProtocolError("Valid Light sample has an error or no acquisition")
            elif values[0] is not None or error is None:
                raise ProtocolError("Failed Light measurement must be null with a reason")
        else:
            values, units, metrics = (data.get("mm"),), ("mm",), ("distance",)
            if quality == Quality.VALID:
                if type(values[0]) not in (int, float) or not math.isfinite(values[0]) or not 0 <= values[0] <= 4000:
                    raise ProtocolError("Invalid Distance measurement")
                if error is not None or seq == 0:
                    raise ProtocolError("Valid Distance sample has an error or no acquisition")
            elif values[0] is not None or error is None:
                raise ProtocolError("Failed Distance measurement must be null with a reason")
        fingerprint = (observed, quality, values, error)
        if seq == tracker.last_seq and fingerprint != tracker.fingerprint:
            raise ProtocolError(f"{module_id} content changed without a new sequence")
        age = (uptime - observed) / 1000
        observed_mono = (start + end) / 2 - age
        if seq != tracker.last_seq:
            tracker.observed_monotonic = observed_mono
            observed_utc = received - timedelta(seconds=end - observed_mono)
            tracker.samples = tuple(
                SensorSample(
                    schema_version=SCHEMA_VERSION,
                    event_id=f"{boot}:{seq}:{metric}",
                    boot_id=boot,
                    seq=seq,
                    module_id=module_id,
                    metric=metric,
                    value=float(value) if value is not None else None,
                    unit=unit,
                    observed_uptime_ms=observed,
                    observed_at=_timestamp(observed_utc),
                    received_at=_timestamp(received),
                    clock_quality="gateway_estimated",
                    clock_uncertainty_ms=(end - start) * 500,
                    source_mode="real",
                    quality=quality,
                    error=error,
                )
                for metric, unit, value in zip(metrics, units, values)
            )
        else:
            tracker.observed_monotonic = min(tracker.observed_monotonic, observed_mono)
        tracker.last_seq = seq
        tracker.last_uptime = uptime
        tracker.fingerprint = fingerprint
        tracker.health = HealthState(
            module_id, "ready" if quality == Quality.VALID else "unavailable",
            None if quality == Quality.VALID else error,
            tracker.samples[0].received_at if quality == Quality.VALID else None,
            "uno_q_mcu",
        )
        self._capabilities = replace(
            self._capabilities,
            available_modules=tuple(
                module for module in ("thermo", "light", "distance")
                if self._modules[module].health is not None
                and self._modules[module].health.state == "ready"
            ),
            sensor_driver=driver if module_id == "thermo"
            else self._capabilities.sensor_driver,
        )

    def _state(self) -> GatewayState:
        samples = tuple(
            sample for module in self._modules.values() for sample in module.samples
        )
        bridge = self._health
        if (bridge.state == "ready" and self._last_success_monotonic is not None
                and (self._clock.monotonic() - self._last_success_monotonic) * 1000
                >= BRIDGE_UNAVAILABLE_MS):
            bridge = replace(bridge, state="unavailable", reason="bridge_heartbeat_expired")
        if bridge.state == "unavailable":
            samples = tuple(
                replace(s, value=None, quality=Quality.UNAVAILABLE, error="bridge_unavailable")
                for s in samples
            )
        stale_limits = {
            "thermo": THERMO_STALE_MS,
            "light": LIGHT_STALE_MS,
            "distance": DISTANCE_STALE_MS,
        }
        health = [bridge]
        for module_id in ("thermo", "light", "distance"):
            tracker = self._modules[module_id]
            module_samples = tuple(s for s in samples if s.module_id == module_id)
            if bridge.state == "unavailable":
                module_samples = tuple(
                    replace(s, value=None, quality=Quality.UNAVAILABLE, error="bridge_unavailable")
                    for s in module_samples
                )
            elif tracker.observed_monotonic is not None:
                age_ms = (self._clock.monotonic() - tracker.observed_monotonic) * 1000
                if age_ms >= stale_limits[module_id]:
                    module_samples = tuple(
                        replace(s, quality=Quality.STALE, error="sample_expired")
                        if s.quality == Quality.VALID else s for s in module_samples
                    )
            samples = tuple(s for s in samples if s.module_id != module_id) + module_samples
            prior = tracker.health
            valid = (
                bool(module_samples)
                and all(s.quality == Quality.VALID for s in module_samples)
                and prior is not None
                and prior.state == "ready"
            )
            reason = None if valid else (
                prior.reason if prior else
                (module_samples[0].error if module_samples else "No sample received")
            )
            health.append(HealthState(
                module_id, "ready" if valid else "unavailable", reason,
                module_samples[0].received_at if valid else None, "uno_q_mcu",
            ))
        return GatewayState(
            SCHEMA_VERSION,
            self._capabilities.boot_id if self._capabilities else None,
            samples, tuple(health),
        )

    def get_device_health(self) -> tuple[HealthState, ...]:
        return self._state().health

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                await asyncio.to_thread(self._transport.close)
                self._closed = True
                self._modules = {
                    "thermo": _ModuleTracker(),
                    "light": _ModuleTracker(),
                    "distance": _ModuleTracker(),
                }
                self._health = HealthState("bridge", "disabled", "Gateway closed", self._last_success)

    async def __aenter__(self):
        self._ensure_open()
        return self

    async def __aexit__(self, *_):
        await self.close()
