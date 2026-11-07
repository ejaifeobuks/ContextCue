"""HardwareGateway: bounded RPC, strict decoding, observation age and recovery."""

import asyncio
import json
import math
import re
import time
from collections import deque
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
from contextcue_contracts.outputs import (
    ACCEPTED,
    COMPLETED,
    EXPIRED,
    FAILED,
    MAX_COMMAND_TTL_MS,
    MAX_TONE_DURATION_MS,
    MAX_TONE_FREQUENCY_HZ,
    MIN_TONE_FREQUENCY_HZ,
    OUTPUT_ACTIONS,
    PIXEL_PATTERNS,
    REJECTED,
    CommandResult,
    DeviceCommand,
    OutputState,
)

from .transport import ArduinoBridgeTransport, BridgeTransport


class ProtocolError(ValueError):
    """The peer's response cannot be safely used as hardware evidence."""


# Bridge failure reason prefixes, so logs say which side failed.
MCU_UNRESPONSIVE = "mcu_unresponsive"      # router answers; the MCU sketch does not
ROUTER_UNAVAILABLE = "router_unavailable"  # the Linux-side Arduino Router does not answer
GATEWAY_STOPPED = "gateway_stopped"        # this process was asked to stop (e.g. SIGTERM)

SENSOR_MODULES = ("thermo", "light", "distance")
OUTPUT_MODULES = ("pixels", "buzzer")
OUTPUTS_MIN_FIRMWARE = (0, 2, 0)  # firmware that implements cc_out/cc_lease/cc_read_outputs
_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
_REMEMBERED_COMMAND_IDS = 256


def _firmware_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _is_int(value: object) -> bool:
    return type(value) is int  # bool is not an int here


def _validate_command(command: DeviceCommand) -> tuple:
    """Linux-side check of a DeviceCommand; the MCU repeats it independently.

    Returns the cc_out arguments (action, pattern, brightness %, frequency Hz,
    duration ms) or raises ValueError with a short reason.
    """
    major = str(command.schema_version).split(".")[0]
    if major != "1":
        raise ValueError("incompatible_schema_version")
    for name, value in (("session", command.session_id), ("command_id", command.command_id)):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"invalid_{name}")
    allowed = OUTPUT_ACTIONS.get((command.target, command.action))
    if allowed is None:
        raise ValueError("unknown_action")
    parameters = command.parameters or {}
    if not isinstance(parameters, dict) or set(parameters) != allowed:
        raise ValueError("invalid_parameters")
    if not _is_int(command.ttl_ms) or not 0 < command.ttl_ms <= MAX_COMMAND_TTL_MS:
        raise ValueError("invalid_ttl")
    if command.notice_id is not None and (
            not isinstance(command.notice_id, str) or not 0 < len(command.notice_id) <= 64):
        raise ValueError("invalid_notice_id")
    if command.notice_revision is not None and (
            not _is_int(command.notice_revision) or command.notice_revision < 0):
        raise ValueError("invalid_notice_revision")
    pattern, percent, frequency, duration = "", 0, 0, 0
    if "pattern" in allowed:
        pattern = parameters["pattern"]
        if pattern not in PIXEL_PATTERNS:
            raise ValueError("invalid_pattern")
        brightness = parameters["brightness"]
        if (type(brightness) not in (int, float) or not math.isfinite(brightness)
                or not 0 <= brightness <= 1):
            raise ValueError("invalid_brightness")
        percent = round(brightness * 100)
    if "frequency_hz" in allowed:
        frequency, duration = parameters["frequency_hz"], parameters["duration_ms"]
        if not _is_int(frequency) or not MIN_TONE_FREQUENCY_HZ <= frequency <= MAX_TONE_FREQUENCY_HZ:
            raise ValueError("invalid_frequency")
        if not _is_int(duration) or not 0 < duration <= MAX_TONE_DURATION_MS:
            raise ValueError("invalid_duration")
    return f"{command.target}.{command.action}", pattern, percent, frequency, duration


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
    hide valid data from the others. Outputs (firmware 0.2.0+) are reached only
    through execute() and heartbeat(); the gateway never retries or replays a
    command, so a command is sent at most once.
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
        self._outputs_supported = False
        self._outputs: OutputState | None = None
        self._outputs_error: str | None = None
        self._outputs_received_at: str | None = None
        self._result_seen_at: dict[tuple[str, str], str] = {}
        self._command_session: str | None = None
        self._sent_ids: deque[str] = deque(maxlen=_REMEMBERED_COMMAND_IDS)
        self._lease_session: str | None = None

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
        # Firmware 0.1.3+ reports Light/Distance availability; older firmware
        # omits the flags, so those modules are unknown until their first read.
        reported = {"thermo": info["ok"] and driver != "none"}
        for module in ("light", "distance"):
            flag = info.get(module, False)
            if type(flag) is not bool:
                raise ProtocolError(f"Invalid {module} capability")
            reported[module] = flag
        firmware = _text(info, "fw")
        same_boot = self._capabilities is not None and self._capabilities.boot_id == boot
        if not same_boot:
            self._outputs = None
            self._outputs_error = None
            self._result_seen_at = {}
        self._outputs_supported = _firmware_tuple(firmware) >= OUTPUTS_MIN_FIRMWARE
        available_modules = (
            self._available_modules()
            if same_boot else
            tuple(module for module in SENSOR_MODULES if reported[module])
        )
        operations = ("get_capabilities", "read_state", "get_device_health")
        if self._outputs_supported:
            operations += ("execute", "heartbeat", "read_outputs")
        capabilities = HardwareCapabilities(
            schema_version=SCHEMA_VERSION,
            boot_id=boot,
            firmware_version=firmware,
            configured_modules=SENSOR_MODULES + (OUTPUT_MODULES if self._outputs_supported else ()),
            available_modules=available_modules,
            sensor_driver=driver,
            units={
                "temperature": "degC", "humidity": "%RH",
                "ambient_light": "lux", "distance": "mm",
            },
            versions={key: _text(info, key) for key in ("core", "bridge", "modulino", "hs", "sht")},
            supported_operations=operations,
            output_patterns=PIXEL_PATTERNS if self._outputs_supported else (),
        )
        if self._capabilities is None or self._capabilities.boot_id != boot:
            self._modules = {
                "thermo": _ModuleTracker(),
                "light": _ModuleTracker(),
                "distance": _ModuleTracker(),
            }
        self._capabilities = capabilities
        self._needs_discovery = False
        if self._outputs_supported and not same_boot:
            await self._refresh_outputs()
        return self._capabilities

    def _available_modules(self) -> tuple[str, ...]:
        sensors = tuple(
            module for module in SENSOR_MODULES
            if self._modules[module].health is not None
            and self._modules[module].health.state == "ready"
        )
        outputs = self._outputs
        present = () if outputs is None or self._outputs_error else tuple(
            module for module, ok in (("pixels", outputs.pixels_available),
                                      ("buzzer", outputs.buzzer_available)) if ok
        )
        return sensors + present

    async def get_capabilities(self) -> HardwareCapabilities:
        async with self._lock:
            self._ensure_open()
            try:
                return await self._discover()
            except Exception as exc:
                self._mark_failed(await self._diagnose(exc))
                raise

    async def _diagnose(self, exc: Exception) -> str:
        """Name the failing side: a timeout with a live router means the MCU."""
        if isinstance(exc, TimeoutError):
            try:
                await asyncio.to_thread(self._transport.router_version, self._timeout)
            except Exception:
                return f"{ROUTER_UNAVAILABLE}: {exc}"
            return f"{MCU_UNRESPONSIVE}: {exc}"
        if isinstance(exc, OSError):  # socket refused/closed, router not running
            return f"{ROUTER_UNAVAILABLE}: {exc}"
        return str(exc)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("HardwareGateway is closed")

    def _mark_failed(self, reason: str) -> None:
        self._needs_discovery = True
        self._health = HealthState("bridge", "unavailable", reason, self._last_success)
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
                errors = []
                for module_id, method in reads:
                    error = await self._read_module(module_id, method)
                    if error is not None:
                        errors.append(error)
                end = self._clock.monotonic()
                if len(errors) == len(reads):
                    if all(isinstance(e, TimeoutError) for e in errors):
                        raise TimeoutError("All sensor RPCs timed out")
                    raise ProtocolError("All sensor RPCs failed")
                self._last_success = _timestamp(self._clock.utcnow())
                self._last_success_monotonic = end
                self._health = HealthState("bridge", "ready", None, self._last_success)
                if self._outputs_supported:
                    await self._refresh_outputs()
            except Exception as exc:
                self._mark_failed(await self._diagnose(exc))
            return self._state()

    async def _refresh_outputs(self) -> None:
        """Read cc_read_outputs; failures affect output health only."""
        try:
            data = await self._rpc("cc_read_outputs")
            self._outputs = self._decode_outputs(data)
            self._outputs_error = None
            self._outputs_received_at = _timestamp(self._clock.utcnow())
        except Exception as exc:
            self._outputs_error = str(exc)
        self._capabilities = replace(self._capabilities, available_modules=self._available_modules())

    def _decode_outputs(self, data: dict) -> OutputState:
        boot = _text(data, "boot")
        if boot != self._capabilities.boot_id:
            self._needs_discovery = True
            raise ProtocolError("MCU reset since handshake")
        _integer(data, "up")
        flags = {}
        for key in ("px", "bz", "tone", "lease"):
            if type(data.get(key)) is not bool:
                raise ProtocolError(f"Invalid output flag: {key}")
            flags[key] = data[key]
        pattern = data.get("pat")
        if pattern not in PIXEL_PATTERNS:
            raise ProtocolError("Invalid Pixels pattern")
        brightness = data.get("b")
        if not _is_int(brightness) or not 0 <= brightness <= 100:
            raise ProtocolError("Invalid Pixels brightness")
        last, outcome, why = data.get("last"), data.get("o"), data.get("why")
        result = None
        if last is not None or outcome is not None:
            if not isinstance(last, str) or not _ID.fullmatch(last):
                raise ProtocolError("Invalid last command ID")
            if outcome not in (COMPLETED, FAILED, EXPIRED):
                raise ProtocolError("Invalid command outcome")
            if why is not None and (not isinstance(why, str) or not 0 < len(why) <= 64):
                raise ProtocolError("Invalid command reason")
            # The MCU sends no time; record when this gateway first saw the outcome.
            at = self._result_seen_at.setdefault(
                (last, outcome), _timestamp(self._clock.utcnow()))
            result = CommandResult(SCHEMA_VERSION, last, outcome, why, at)
        elif why is not None:
            raise ProtocolError("Command reason without a command")
        return OutputState(
            SCHEMA_VERSION, boot, flags["px"], flags["bz"], pattern, brightness / 100,
            flags["tone"], flags["lease"], result,
        )

    @staticmethod
    def new_session_id() -> str:
        """A fresh output session ID. Use a new one each time the app starts, so
        the MCU starts quiet and never inherits an earlier session's outputs."""
        return f"s-{uuid4().hex[:24]}"

    async def execute(self, command: DeviceCommand) -> CommandResult:
        """Send one bounded output command. Returns accepted (queued on the MCU),
        rejected, expired, or failed; completion appears later in read_outputs().
        A command ID is sent at most once, even when the outcome is unknown."""
        received = self._clock.monotonic()
        command_id = command.command_id if isinstance(command.command_id, str) else ""

        def result(status: str, reason: str | None) -> CommandResult:
            return CommandResult(SCHEMA_VERSION, command_id, status, reason,
                                 _timestamp(self._clock.utcnow()))

        try:
            action, pattern, percent, frequency, duration = _validate_command(command)
        except ValueError as exc:
            return result(REJECTED, str(exc))
        async with self._lock:
            self._ensure_open()
            if command.session_id != self._command_session:
                self._command_session = command.session_id
                self._sent_ids.clear()
            if command_id in self._sent_ids:
                return result(REJECTED, "duplicate_command")
            if self._needs_discovery or self._capabilities is None:
                return result(FAILED, "bridge_unavailable")
            if not self._outputs_supported:
                return result(REJECTED, "outputs_not_supported_by_firmware")
            # Time spent waiting here (behind a poll) comes out of the TTL.
            remaining = command.ttl_ms - math.ceil((self._clock.monotonic() - received) * 1000)
            self._sent_ids.append(command_id)  # before sending: never replay
            if remaining <= 0:
                return result(EXPIRED, "ttl_elapsed_in_gateway")
            try:
                data = await self._rpc("cc_out", command.session_id, command_id, action,
                                       pattern, percent, frequency, duration, remaining)
            except Exception as exc:
                return result(FAILED, f"{await self._diagnose(exc)} (outcome unknown)")
            status = data.get("r")
            if status == ACCEPTED and data.get("id") == command_id:
                return result(ACCEPTED, None)
            why = data.get("why")
            if status == REJECTED and isinstance(why, str) and 0 < len(why) <= 64:
                return result(REJECTED, why)
            return result(FAILED, "Invalid cc_out response (outcome unknown)")

    async def heartbeat(self, session_id: str) -> HealthState:
        """Renew the output lease (call about once a second). Without renewal the
        MCU silences the Buzzer and turns Pixels off after 3 s."""
        async with self._lock:
            self._ensure_open()

            def lease(state: str, reason: str | None) -> HealthState:
                at = _timestamp(self._clock.utcnow()) if state == "ready" else None
                return HealthState("output_lease", state, reason, at, "uno_q_mcu")

            if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
                return lease("unavailable", "invalid_session")
            if self._needs_discovery or self._capabilities is None:
                return lease("unavailable", "bridge_unavailable")
            if not self._outputs_supported:
                return lease("unavailable", "outputs_not_supported_by_firmware")
            try:
                data = await self._rpc("cc_lease", session_id)
            except Exception as exc:
                return lease("unavailable", await self._diagnose(exc))
            if data.get("ok") is True and _is_int(data.get("lease_ms")):
                self._lease_session = session_id
                return lease("ready", None)
            why = data.get("why")
            return lease("unavailable", why if isinstance(why, str) else "Invalid cc_lease response")

    async def read_outputs(self) -> OutputState | None:
        """Fresh output report, or None when outputs are unsupported/unreadable."""
        async with self._lock:
            self._ensure_open()
            if self._needs_discovery or not self._outputs_supported:
                return None
            await self._refresh_outputs()
            return None if self._outputs_error else self._outputs

    def stopped_state(self, reason: str) -> GatewayState:
        """Final record for a requested stop, distinct from any hardware failure.

        Values are withheld because the gateway will stop observing them.
        """
        state = self._state()
        reason = f"{GATEWAY_STOPPED}: {reason}"
        samples = tuple(
            replace(s, value=None, quality=Quality.UNAVAILABLE, error=GATEWAY_STOPPED)
            for s in state.samples
        )
        health = tuple(
            replace(h, state="stopped", reason=reason) for h in state.health
        )
        return replace(state, samples=samples, health=health)

    async def _read_module(self, module_id: str, method: str) -> Exception | None:
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
            return None
        except Exception as exc:
            tracker = self._modules[module_id]
            tracker.health = HealthState(
                module_id, "unavailable", str(exc),
                tracker.samples[0].received_at if tracker.samples else None,
                "uno_q_mcu",
            )
            return exc

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
            available_modules=self._available_modules(),
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
        if self._outputs_supported:
            health.extend(self._output_health(bridge))
        return GatewayState(
            SCHEMA_VERSION,
            self._capabilities.boot_id if self._capabilities else None,
            samples, tuple(health),
        )

    def _output_health(self, bridge: HealthState) -> list[HealthState]:
        health = []
        for module, field in (("pixels", "pixels_available"), ("buzzer", "buzzer_available")):
            if bridge.state != "ready":
                state, reason = "unavailable", "bridge_unavailable"
            elif self._outputs_error is not None:
                state, reason = "unavailable", self._outputs_error
            elif self._outputs is None:
                state, reason = "unavailable", "No output report received"
            elif getattr(self._outputs, field):
                state, reason = "ready", None
            else:
                state, reason = "unavailable", f"{module}_not_found"
            health.append(HealthState(
                module, state, reason,
                self._outputs_received_at if state == "ready" else None, "uno_q_mcu",
            ))
        return health

    def get_device_health(self) -> tuple[HealthState, ...]:
        return self._state().health

    async def close(self) -> None:
        async with self._lock:
            if not self._closed:
                if self._lease_session and self._outputs_supported and not self._needs_discovery:
                    try:  # stop at once instead of waiting for the lease watchdog
                        await self._rpc("cc_out", self._lease_session, f"stop-{uuid4().hex[:24]}",
                                        "outputs.stop", "", 0, 0, 0, MAX_COMMAND_TTL_MS)
                    except Exception:
                        pass  # the MCU still turns outputs off within 3 s without renewal
                await asyncio.to_thread(self._transport.close)
                self._closed = True
                self._outputs = None
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
