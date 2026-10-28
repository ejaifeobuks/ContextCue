"""HardwareGateway: bounded RPC, strict decoding, observation age and recovery."""

import asyncio
import json
import math
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .contracts import (
    BRIDGE_UNAVAILABLE_MS,
    SCHEMA_VERSION,
    THERMO_STALE_MS,
    GatewayState,
    HardwareCapabilities,
    HealthState,
    ProtocolError,
    Quality,
    SensorSample,
)
from .transport import ArduinoBridgeTransport, BridgeTransport


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


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

    Only the first Thermo milestone is exposed. Output and control-event APIs
    will be added with their firmware implementations, rather than pretend success.
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
        self._samples: tuple[SensorSample, ...] = ()
        self._observed_monotonic: float | None = None
        self._last_seq: int | None = None
        self._last_uptime: int | None = None
        self._sample_fingerprint: tuple | None = None
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
        if info["ok"] and driver == "none":
            raise ProtocolError("Available Thermo has no driver")
        capabilities = HardwareCapabilities(
            schema_version=SCHEMA_VERSION,
            boot_id=boot,
            firmware_version=_text(info, "fw"),
            configured_modules=("thermo",),
            available_modules=("thermo",) if info["ok"] else (),
            sensor_driver=driver,
            units={"temperature": "degC", "humidity": "%RH"},
            versions={key: _text(info, key) for key in ("core", "bridge", "modulino", "hs", "sht")},
            supported_operations=("get_capabilities", "read_state", "get_device_health"),
        )
        if self._capabilities is None or self._capabilities.boot_id != boot:
            self._samples = ()
            self._last_seq = None
            self._last_uptime = None
            self._observed_monotonic = None
            self._sample_fingerprint = None
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
                start = self._clock.monotonic()
                data = await self._rpc("cc_read")
                end = self._clock.monotonic()
                received = self._clock.utcnow()
                if data.get("boot") != self._capabilities.boot_id:
                    # A firmware restart loses the RAM boot token. Handshake again;
                    # never attach old samples to the new MCU instance.
                    self._needs_discovery = True
                    await self._discover()
                    start = self._clock.monotonic()
                    data = await self._rpc("cc_read")
                    end = self._clock.monotonic()
                    received = self._clock.utcnow()
                self._ingest(data, start, end, received)
                self._last_success = _timestamp(received)
                self._last_success_monotonic = end
                self._health = HealthState("bridge", "ready", None, self._last_success)
            except Exception as exc:
                self._mark_failed(exc)
            return self._state()

    def _ingest(self, data: dict, start: float, end: float, received: datetime) -> None:
        boot = _text(data, "boot")
        if boot != self._capabilities.boot_id:
            raise ProtocolError("MCU reset during handshake")
        seq = _integer(data, "seq")
        uptime = _integer(data, "up")
        observed = _integer(data, "at")
        if observed > uptime:
            raise ProtocolError("Observation is ahead of MCU uptime")
        if self._last_uptime is not None and uptime < self._last_uptime:
            raise ProtocolError("MCU uptime regressed within one boot")
        if self._last_seq is not None and seq < self._last_seq:
            raise ProtocolError("Sensor sequence regressed within one boot")
        try:
            quality = Quality(data["q"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolError("Invalid sensor quality") from exc
        if quality == Quality.STALE:
            raise ProtocolError("MCU reports validity; gateway determines staleness")
        error = data.get("err")
        if error is not None and (not isinstance(error, str) or not error or len(error) > 64):
            raise ProtocolError("Invalid sensor error")
        values = (data.get("t"), data.get("h"))
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
        fingerprint = (observed, quality, values, error, driver)
        if seq == self._last_seq and fingerprint != self._sample_fingerprint:
            raise ProtocolError("Sensor content changed without a new sequence")
        age = (uptime - observed) / 1000
        # Map at RPC midpoint. Repeated polls never refresh an old observation.
        observed_mono = (start + end) / 2 - age
        if seq != self._last_seq:
            self._observed_monotonic = observed_mono
            observed_utc = received - timedelta(seconds=end - observed_mono)
            self._samples = tuple(
                SensorSample(
                    schema_version=SCHEMA_VERSION,
                    event_id=f"{boot}:{seq}:{metric}",
                    boot_id=boot,
                    seq=seq,
                    module_id="thermo",
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
                for metric, unit, value in zip(
                    ("temperature", "humidity"), ("degC", "%RH"), values
                )
            )
        else:
            # Age may increase faster than Linux time after a delayed call.
            self._observed_monotonic = min(self._observed_monotonic, observed_mono)
        self._last_seq = seq
        self._last_uptime = uptime
        self._sample_fingerprint = fingerprint
        available = quality == Quality.VALID
        self._capabilities = replace(
            self._capabilities, available_modules=("thermo",) if available else (),
            sensor_driver=driver,
        )

    def _state(self) -> GatewayState:
        samples = self._samples
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
        if self._observed_monotonic is not None:
            age_ms = (self._clock.monotonic() - self._observed_monotonic) * 1000
            if age_ms >= THERMO_STALE_MS:
                samples = tuple(
                    replace(s, quality=Quality.STALE, error="sample_expired")
                    if s.quality == Quality.VALID else s for s in samples
                )
        healthy = bool(samples) and all(s.quality == Quality.VALID for s in samples)
        sensor = HealthState(
            "thermo", "ready" if healthy else "unavailable",
            None if healthy else (samples[0].error if samples else "No sample received"),
            samples[0].received_at if healthy else None, "uno_q_mcu",
        )
        return GatewayState(
            SCHEMA_VERSION,
            self._capabilities.boot_id if self._capabilities else None,
            samples, (bridge, sensor),
        )

    def get_device_health(self) -> tuple[HealthState, ...]:
        return self._state().health

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                await asyncio.to_thread(self._transport.close)
                self._closed = True
                self._samples = ()
                self._health = HealthState("bridge", "disabled", "Gateway closed", self._last_success)

    async def __aenter__(self):
        self._ensure_open()
        return self

    async def __aexit__(self, *_):
        await self.close()
