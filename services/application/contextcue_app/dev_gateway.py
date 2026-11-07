"""Simulated gateway for laptop development and the Simulation view.

It exposes the same surface as `HardwareGateway` (read_state, execute,
heartbeat, read_outputs, get_capabilities, close) and returns the same
contract types, but every sample is labeled `source_mode="simulated"`. It is
only ever selected explicitly (`--simulated`); the real app never falls back
to it (APP-19).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from contextcue_contracts import (OUTPUT_LEASE_MS, PIXEL_PATTERNS, SCHEMA_VERSION, CommandResult,
                                  DeviceCommand, GatewayState, HardwareCapabilities, HealthState,
                                  OutputState, Quality, SensorSample)
from contextcue_contracts.outputs import MAX_TONE_DURATION_MS, OUTPUT_ACTIONS

RATES_S = {"thermo": 1.0, "light": 0.2, "distance": 0.1}
METRICS = {"thermo": (("temperature", "degC"), ("humidity", "%RH")),
           "light": (("ambient_light", "lux"),), "distance": (("distance", "mm"),)}
STALE_S = {"thermo": 5.0, "light": 2.0, "distance": 2.0}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SimulatedGateway:
    source_mode = "simulated"

    def __init__(self, *, monotonic: Callable[[], float], utcnow: Callable[[], datetime] | None = None):
        self._mono = monotonic
        self._utc = utcnow or (lambda: datetime.now(timezone.utc))
        self.values = {"temperature": 22.5, "humidity": 45.0, "ambient_light": 180.0, "distance": 900.0}
        self.module_fault: dict[str, str | None] = {"thermo": None, "light": None, "distance": None}
        self.frozen: dict[str, bool] = {"thermo": False, "light": False, "distance": False}
        self.bridge_fault: str | None = None
        self.pixels_available = True
        self.buzzer_available = True
        self._boot = uuid4().hex
        self._seq = {m: 0 for m in RATES_S}
        self._last = {m: None for m in RATES_S}
        self._observed_mono = {m: None for m in RATES_S}
        self._samples: dict[str, tuple[SensorSample, ...]] = {m: () for m in RATES_S}
        self._start = monotonic()
        self._pattern, self._brightness, self._tone_until = "off", 0.0, None
        self._lease_until: float | None = None
        self._lease_session: str | None = None
        self._last_result: CommandResult | None = None
        self._seen: set[str] = set()
        self.commands: list[DeviceCommand] = []
        self.closed = False

    # ------------------------------------------------------------- control
    def set_value(self, metric: str, value: float) -> None:
        if metric not in self.values:
            raise ValueError(f"Unknown metric {metric}")
        self.values[metric] = float(value)

    def reboot(self) -> None:
        """New MCU boot identity: old samples and outputs are invalid."""
        self._boot = uuid4().hex
        self._seq = {m: 0 for m in RATES_S}
        self._samples = {m: () for m in RATES_S}
        self._observed_mono = {m: None for m in RATES_S}
        self._pattern, self._brightness, self._tone_until = "off", 0.0, None
        self._lease_until = None
        self._seen.clear()

    # --------------------------------------------------------------- reads
    async def get_capabilities(self) -> HardwareCapabilities:
        return HardwareCapabilities(
            schema_version=SCHEMA_VERSION, boot_id=self._boot, firmware_version="simulated",
            configured_modules=("thermo", "light", "distance"),
            available_modules=tuple(m for m, f in self.module_fault.items() if f is None),
            sensor_driver="simulated", units={"temperature": "degC", "humidity": "%RH",
                                              "ambient_light": "lux", "distance": "mm"},
            versions={"gateway": "simulated"},
            supported_operations=("read_state", "execute", "heartbeat", "read_outputs"),
            output_patterns=PIXEL_PATTERNS, native_brightness=False)

    async def read_state(self) -> GatewayState:
        now = self._mono()
        utc = self._utc()
        if self.bridge_fault:
            samples = tuple(
                replace(s, value=None, quality=Quality.UNAVAILABLE, error="bridge_unavailable")
                for m in RATES_S for s in self._samples[m])
            health = [HealthState("bridge", "unavailable", self.bridge_fault, None, "simulated")]
            health += [HealthState(m, "unavailable", "bridge_unavailable", None, "simulated") for m in RATES_S]
            return GatewayState(SCHEMA_VERSION, None, samples, tuple(health))
        health = [HealthState("bridge", "ready", None, _iso(utc), "simulated")]
        out = []
        for module in RATES_S:
            fault = self.module_fault[module]
            due = self._last[module] is None or now - self._last[module] >= RATES_S[module]
            if due and not self.frozen[module]:
                self._last[module] = now
                self._seq[module] += 1
                self._observed_mono[module] = now
                self._samples[module] = tuple(
                    SensorSample(
                        schema_version=SCHEMA_VERSION,
                        event_id=f"{self._boot}:{self._seq[module]}:{metric}",
                        boot_id=self._boot, seq=self._seq[module], module_id=module,
                        metric=metric, value=None if fault else self.values[metric], unit=unit,
                        observed_uptime_ms=int((now - self._start) * 1000),
                        observed_at=_iso(utc), received_at=_iso(utc),
                        clock_quality="simulated", clock_uncertainty_ms=0.0,
                        source_mode="simulated",
                        quality=Quality.UNAVAILABLE if fault else Quality.VALID, error=fault)
                    for metric, unit in METRICS[module])
            samples = self._samples[module]
            observed = self._observed_mono[module]
            if observed is not None and now - observed >= STALE_S[module]:
                samples = tuple(
                    replace(s, quality=Quality.STALE, error="sample_expired")
                    if s.quality == Quality.VALID else s for s in samples)
            out.extend(samples)
            ok = bool(samples) and all(s.quality == Quality.VALID for s in samples)
            reason = None if ok else (samples[0].error if samples else "No sample received")
            health.append(HealthState(module, "ready" if ok else "unavailable", reason,
                                      _iso(utc) if ok else None, "simulated"))
        lease_ok = self._lease_until is not None and now < self._lease_until
        if not lease_ok:
            self._pattern, self._tone_until = "off", None
        for module, available in (("pixels", self.pixels_available), ("buzzer", self.buzzer_available)):
            health.append(HealthState(module, "ready" if available else "unavailable",
                                      None if available else f"{module}_not_found",
                                      _iso(utc) if available else None, "simulated"))
        return GatewayState(SCHEMA_VERSION, self._boot, tuple(out), tuple(health))

    # ------------------------------------------------------------- outputs
    async def execute(self, command: DeviceCommand) -> CommandResult:
        self.commands.append(command)
        at = _iso(self._utc())

        def result(status, reason=None):
            self._last_result = CommandResult(SCHEMA_VERSION, command.command_id, status, reason, at)
            return self._last_result

        if self.bridge_fault:
            return result("failed", "bridge_unavailable (outcome unknown)")
        if command.command_id in self._seen:
            return result("rejected", "duplicate_command")
        self._seen.add(command.command_id)
        allowed = OUTPUT_ACTIONS.get((command.target, command.action))
        if allowed is None or set(command.parameters) - allowed:
            return result("rejected", "not_allowlisted")
        if command.ttl_ms <= 0:
            return result("expired", "ttl_elapsed")
        if command.target == "pixels":
            if not self.pixels_available:
                return result("rejected", "pixels_unavailable")
            if command.action == "set_pattern":
                pattern = command.parameters.get("pattern")
                level = command.parameters.get("brightness", 0)
                if pattern not in PIXEL_PATTERNS or not 0 <= level <= 1:
                    return result("rejected", "out_of_range")
                self._pattern, self._brightness = pattern, float(level)
            else:
                self._pattern = "off"
        elif command.target == "buzzer":
            if not self.buzzer_available:
                return result("rejected", "buzzer_unavailable")
            if command.action == "short_tone":
                duration = command.parameters.get("duration_ms", 0)
                if not 0 < duration <= MAX_TONE_DURATION_MS:
                    return result("rejected", "out_of_range")
                self._tone_until = self._mono() + duration / 1000
            else:
                self._tone_until = None
        else:
            self._pattern, self._tone_until = "off", None
        return result("accepted")

    async def heartbeat(self, session_id: str) -> HealthState:
        if self.bridge_fault:
            return HealthState("output_lease", "unavailable", "bridge_unavailable", None, "simulated")
        self._lease_session = session_id
        self._lease_until = self._mono() + OUTPUT_LEASE_MS / 1000
        return HealthState("output_lease", "ready", None, _iso(self._utc()), "simulated")

    async def read_outputs(self) -> OutputState | None:
        if self.bridge_fault:
            return None
        now = self._mono()
        return OutputState(
            schema_version=SCHEMA_VERSION, boot_id=self._boot,
            pixels_available=self.pixels_available, buzzer_available=self.buzzer_available,
            pattern=self._pattern, brightness=self._brightness,
            tone_active=self._tone_until is not None and now < self._tone_until,
            lease_active=self._lease_until is not None and now < self._lease_until,
            last_result=self._last_result)

    async def close(self) -> None:
        self.closed = True
        self._pattern, self._tone_until = "off", None
