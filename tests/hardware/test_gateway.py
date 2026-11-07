"""Contract/fault tests only; test peers never replace the real runtime adapter."""

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/contextcue-hardware/python"))

from contextcue_contracts import DeviceCommand, Quality
from contextcue_hardware.gateway import HardwareGateway, ProtocolError

BOOT = "a" * 32


class TestClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def utcnow(self):
        return datetime(2026, 10, 27, tzinfo=timezone.utc) + timedelta(seconds=self.now)


def reading(**changes):
    result = {"v": "1.0", "boot": BOOT, "seq": 1, "up": 1100, "at": 1000,
              "q": "valid", "t": 24.25, "h": 45.0, "d": "modulino", "err": None}
    result.update(changes)
    return result


def light_reading(**changes):
    result = {"v": "1.0", "boot": BOOT, "seq": 1, "up": 1100, "at": 1000,
              "q": "unavailable", "lux": None, "err": "light_not_found"}
    result.update(changes)
    return result


def distance_reading(**changes):
    result = {"v": "1.0", "boot": BOOT, "seq": 1, "up": 1100, "at": 1000,
              "q": "valid", "mm": 430, "err": None}
    result.update(changes)
    return result


def outputs_reading(**changes):
    result = {"v": "1.0", "boot": BOOT, "up": 1200, "px": True, "pat": "off", "b": 0,
              "bz": True, "tone": False, "lease": False, "last": None, "o": None, "why": None}
    result.update(changes)
    return result


class TestPeer:
    def __init__(self):
        self.info = {"v": "1.0", "boot": BOOT, "fw": "0.1.0", "ok": True,
                     "driver": "modulino", "core": "1.0.0", "bridge": "0.4.4", "modulino": "0.10.0",
                     "hs": "1.0.1", "sht": "0.5.3"}
        self.data = reading()
        self.light_data = light_reading()
        self.distance_data = distance_reading()
        self.calls = []
        self.failure = None
        self.router_alive = True
        self.closed = False
        # Outputs (firmware 0.2.0+). Sensor tests use firmware 0.1.0 without them.
        self.out_calls = []
        self.out_failure = None
        self.out_response = None
        self.lease_data = {"v": "1.0", "ok": True, "lease_ms": 3000}
        self.outputs_data = outputs_reading()

    def router_version(self, timeout):
        if not self.router_alive:
            raise TimeoutError("Request '$/version' timed out")
        return "0.11.0"

    def connect(self, timeout):
        if self.failure:
            raise self.failure

    def call(self, method, *args, timeout):
        self.calls.append((method, args, timeout))
        if self.failure:
            raise self.failure
        if method == "cc_out":
            self.out_calls.append(args)
            if self.out_failure:
                raise self.out_failure
            response = self.out_response or {"v": "1.0", "id": args[1], "r": "accepted", "why": None}
            return json.dumps(response)
        result = {
            "cc_open": self.info,
            "cc_read": self.data,
            "cc_read_light": self.light_data,
            "cc_read_distance": self.distance_data,
            "cc_lease": self.lease_data,
            "cc_read_outputs": self.outputs_data,
        }.get(method)
        if result is None:
            raise AssertionError(f"Unexpected RPC method: {method}")
        return json.dumps(result, separators=(",", ":")) if isinstance(result, dict) else result

    def close(self):
        self.closed = True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = TestPeer()
        self.clock = TestClock()
        self.gateway = HardwareGateway(self.peer, clock=self.clock)

    async def asyncTearDown(self):
        await self.gateway.close()

    async def test_real_samples_preserve_identity_and_observation_age(self):
        state = await self.gateway.read_state()
        temperature, humidity = tuple(s for s in state.samples if s.module_id == "thermo")
        self.assertEqual(temperature.value, 24.25)
        self.assertEqual(humidity.unit, "%RH")
        self.assertEqual(temperature.source_mode, "real")
        self.assertEqual(temperature.observed_at, "2026-10-27T00:01:39.900Z")
        self.assertEqual(temperature.event_id, f"{BOOT}:1:temperature")
        self.assertEqual(
            {h.component_id: h.state for h in state.health},
            {"bridge": "ready", "thermo": "ready",
             "light": "unavailable", "distance": "ready"},
        )
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(capabilities.available_modules, ("thermo", "distance"))

    async def test_zero_is_a_valid_measurement(self):
        self.peer.data = reading(t=0, h=0)
        state = await self.gateway.read_state()
        thermo = tuple(s for s in state.samples if s.module_id == "thermo")
        self.assertTrue(all(s.value == 0 and s.quality == Quality.VALID for s in thermo))

    async def test_repeated_poll_does_not_refresh_old_observation(self):
        first = await self.gateway.read_state()
        self.clock.now += 6
        self.peer.data["up"] += 6000
        repeated = await self.gateway.read_state()
        first_thermo = tuple(s for s in first.samples if s.module_id == "thermo")
        repeated_thermo = tuple(s for s in repeated.samples if s.module_id == "thermo")
        self.assertEqual(first_thermo[0].observed_at, repeated_thermo[0].observed_at)
        self.assertEqual(first_thermo[0].received_at, repeated_thermo[0].received_at)
        thermo = tuple(s for s in repeated.samples if s.module_id == "thermo")
        self.assertTrue(all(s.quality == Quality.STALE for s in thermo))
        self.assertEqual(repeated.health[0].state, "ready")
        self.assertEqual(repeated.health[1].state, "unavailable")

    async def test_new_sample_restores_freshness(self):
        await self.gateway.read_state()
        self.clock.now += 6
        self.peer.data = reading(seq=2, up=7100, at=7000)
        state = await self.gateway.read_state()
        thermo = tuple(s for s in state.samples if s.module_id == "thermo")
        self.assertEqual(thermo[0].quality, Quality.VALID)
        self.assertEqual(thermo[0].seq, 2)

    async def test_sensor_disconnect_keeps_bridge_healthy(self):
        self.peer.data = reading(q="unavailable", t=None, h=None, err="thermo_disconnected")
        state = await self.gateway.read_state()
        self.assertIsNone(next(s for s in state.samples if s.module_id == "thermo").value)
        self.assertEqual(state.health[0].state, "ready")
        self.assertEqual(state.health[1].reason, "thermo_disconnected")

    async def test_transport_failure_invalidates_previously_valid_values(self):
        await self.gateway.read_state()
        self.peer.failure = TimeoutError("RPC deadline")
        state = await self.gateway.read_state()
        self.assertTrue(all(s.value is None and s.quality == Quality.UNAVAILABLE for s in state.samples))
        self.assertEqual(state.health[1].state, "unavailable")
        self.peer.failure = None
        recovered = await self.gateway.read_state()
        self.assertEqual(
            next(s for s in recovered.samples if s.module_id == "thermo").value,
            24.25,
        )
        self.assertEqual(recovered.health[0].state, "ready")

    async def test_timeout_with_live_router_blames_the_mcu(self):
        await self.gateway.read_state()
        self.peer.failure = TimeoutError("Request 'cc_read' timed out after 0.75s")
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].state, "unavailable")
        self.assertTrue(state.health[0].reason.startswith("mcu_unresponsive: "))

    async def test_timeout_with_dead_router_blames_the_router(self):
        await self.gateway.read_state()
        self.peer.failure = TimeoutError("Request 'cc_read' timed out after 0.75s")
        self.peer.router_alive = False
        state = await self.gateway.read_state()
        self.assertTrue(state.health[0].reason.startswith("router_unavailable: "))

    async def test_refused_router_socket_blames_the_router(self):
        self.peer.failure = ConnectionError("Arduino Router unavailable")
        state = await self.gateway.read_state()
        self.assertTrue(state.health[0].reason.startswith("router_unavailable: "))

    async def test_protocol_errors_are_not_attributed_to_a_side(self):
        self.peer.data = "not json"
        self.peer.light_data = "not json"
        self.peer.distance_data = "not json"
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].reason, "All sensor RPCs failed")

    async def test_stopped_state_is_distinct_from_failures(self):
        await self.gateway.read_state()
        state = self.gateway.stopped_state("SIGTERM received")
        self.assertTrue(all(h.state == "stopped" for h in state.health))
        self.assertTrue(all(h.reason == "gateway_stopped: SIGTERM received" for h in state.health))
        self.assertTrue(all(s.value is None and s.error == "gateway_stopped" for s in state.samples))

    async def test_reboot_discards_old_boot_and_sequence(self):
        await self.gateway.read_state()
        new_boot = "b" * 32
        self.peer.info["boot"] = new_boot
        self.peer.data = reading(boot=new_boot, seq=1, up=100, at=50)
        self.peer.light_data = light_reading(boot=new_boot)
        self.peer.distance_data = distance_reading(boot=new_boot)
        state = await self.gateway.read_state()
        self.assertEqual(state.boot_id, new_boot)
        self.assertTrue(all(s.boot_id == new_boot for s in state.samples))
        self.assertEqual(len([c for c in self.peer.calls if c[0] == "cc_open"]), 2)

    async def test_reboot_handshake_offers_a_fresh_boot_token(self):
        # Model the actual firmware's assign-once boot identity behavior.
        original_call = self.peer.call

        def boot_aware_call(method, *args, timeout):
            if method == "cc_open" and not self.peer.info["boot"]:
                self.peer.info["boot"] = args[0]
                self.peer.data["boot"] = args[0]
            return original_call(method, *args, timeout=timeout)

        self.peer.call = boot_aware_call
        self.peer.info["boot"] = ""
        first = await self.gateway.read_state()
        self.peer.info["boot"] = ""
        self.peer.data = reading(boot="", seq=1, up=100, at=50)
        self.peer.light_data = light_reading(boot="")
        self.peer.distance_data = distance_reading(boot="")
        second = await self.gateway.read_state()
        self.assertNotEqual(first.boot_id, second.boot_id)
        self.assertEqual(second.health[0].state, "ready")

    async def test_health_expires_without_further_polling(self):
        await self.gateway.read_state()
        self.clock.now += 3
        bridge, sensor, _, _ = self.gateway.get_device_health()
        self.assertEqual(bridge.state, "unavailable")
        self.assertEqual(sensor.state, "unavailable")

    async def test_transient_boot_mismatch_does_not_erase_unchanged_good_sample(self):
        await self.gateway.read_state()
        original_call = self.peer.call
        wrong_once = True

        def transient_call(method, *args, timeout):
            nonlocal wrong_once
            if method == "cc_read" and wrong_once:
                wrong_once = False
                return json.dumps(reading(boot=""), separators=(",", ":"))
            return original_call(method, *args, timeout=timeout)

        self.peer.call = transient_call
        state = await self.gateway.read_state()
        self.assertEqual(
            next(s for s in state.samples if s.module_id == "thermo").value,
            24.25,
        )
        self.assertEqual(state.health[0].state, "ready")

    async def test_incompatible_version_is_not_accepted(self):
        self.peer.info["v"] = "2.0"
        with self.assertRaises(ProtocolError):
            await self.gateway.get_capabilities()
        state = await self.gateway.read_state()
        self.assertEqual(state.samples, ())
        self.assertEqual(state.health[0].state, "unavailable")

    async def test_malformed_responses_are_unavailable(self):
        for payload in ("{broken", "[]", "x" * 225, "null"):
            with self.subTest(payload=payload[:10]):
                self.peer.data = payload
                state = await self.gateway.read_state()
                self.assertEqual(state.health[1].state, "unavailable")

    async def test_invalid_sensor_fields_do_not_become_facts(self):
        for changes in ({"t": True}, {"t": float("nan")}, {"h": 101},
                        {"at": 1200}, {"seq": -1}, {"up": True},
                        {"q": "unknown"}, {"t": None}, {"err": "failure"}):
            with self.subTest(changes=changes):
                self.peer.data = reading(**changes)
                state = await self.gateway.read_state()
                self.assertTrue(
                    all(s.quality != Quality.VALID
                        for s in state.samples if s.module_id == "thermo")
                )
                self.assertEqual(state.health[1].state, "unavailable")

    async def test_failed_sample_must_have_null_values(self):
        self.peer.data = reading(q="invalid", err="read_failed")
        state = await self.gateway.read_state()
        self.assertEqual(state.health[1].state, "unavailable")

    async def test_sequence_and_uptime_regressions_are_rejected(self):
        await self.gateway.read_state()
        self.peer.data = reading(seq=0)
        state = await self.gateway.read_state()
        self.assertEqual(state.health[1].state, "unavailable")
        self.peer.data = reading(seq=2, up=500, at=400)
        state = await self.gateway.read_state()
        self.assertEqual(state.health[1].state, "unavailable")

    async def test_sample_cannot_change_under_same_sequence(self):
        await self.gateway.read_state()
        self.peer.data["t"] = 30
        state = await self.gateway.read_state()
        self.assertEqual(state.health[1].state, "unavailable")

    async def test_capabilities_only_advertise_implemented_features(self):
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(
            capabilities.configured_modules, ("thermo", "light", "distance")
        )
        self.assertEqual(capabilities.output_patterns, ())
        self.assertFalse(capabilities.native_brightness)
        self.assertIsNone(capabilities.audio_adapter_version)
        self.assertNotIn("execute", capabilities.supported_operations)
        self.assertEqual(
            capabilities.units,
            {"temperature": "degC", "humidity": "%RH",
             "ambient_light": "lux", "distance": "mm"},
        )

    async def test_handshake_advertises_each_reported_module(self):
        self.peer.info.update({"light": True, "distance": False})
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(capabilities.available_modules, ("thermo", "light"))

    async def test_handshake_without_module_flags_advertises_only_thermo(self):
        # Firmware before 0.1.3 reports only Thermo availability.
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(capabilities.available_modules, ("thermo",))

    async def test_handshake_rejects_non_boolean_module_flags(self):
        self.peer.info["distance"] = "yes"
        with self.assertRaises(ProtocolError):
            await self.gateway.get_capabilities()

    async def test_close_disconnects_and_prevents_more_reads(self):
        await self.gateway.close()
        self.assertTrue(self.peer.closed)
        with self.assertRaises(RuntimeError):
            await self.gateway.read_state()

    async def test_rpc_wait_does_not_block_event_loop(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        original_call = self.peer.call

        def blocking_call(*args, **kwargs):
            entered.set()
            if not release.wait(1):
                raise TimeoutError("Test event loop failed to release worker")
            return original_call(*args, **kwargs)

        self.peer.call = blocking_call
        pending = asyncio.create_task(self.gateway.read_state())
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        self.assertTrue(entered.is_set())
        release.set()
        state = await pending
        self.assertEqual(state.health[0].state, "ready")


def command(**changes):
    fields = {"schema_version": "1.0", "session_id": "s-test", "command_id": "cmd-1",
              "target": "pixels", "action": "set_pattern",
              "parameters": {"pattern": "notice", "brightness": 0.25}, "ttl_ms": 2000}
    fields.update(changes)
    return DeviceCommand(**fields)


class OutputGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.peer = TestPeer()
        self.peer.info["fw"] = "0.2.0"
        self.clock = TestClock()
        self.gateway = HardwareGateway(self.peer, clock=self.clock)
        await self.gateway.read_state()  # handshake

    async def asyncTearDown(self):
        await self.gateway.close()

    async def test_capabilities_and_health_include_outputs(self):
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(capabilities.output_patterns, ("ready", "notice", "fault", "off"))
        self.assertIn("execute", capabilities.supported_operations)
        self.assertIn("pixels", capabilities.configured_modules)
        self.assertIn("buzzer", capabilities.available_modules)
        health = {h.component_id: h for h in (await self.gateway.read_state()).health}
        self.assertEqual((health["pixels"].state, health["buzzer"].state), ("ready", "ready"))
        self.assertEqual(health["pixels"].execution_location, "uno_q_mcu")

    async def test_missing_module_is_named(self):
        self.peer.outputs_data = outputs_reading(px=False)
        state = await self.gateway.read_state()
        pixels = next(h for h in state.health if h.component_id == "pixels")
        self.assertEqual((pixels.state, pixels.reason), ("unavailable", "pixels_not_found"))
        capabilities = await self.gateway.get_capabilities()
        self.assertNotIn("pixels", capabilities.available_modules)

    async def test_execute_maps_command_to_bounded_mcu_call(self):
        result = await self.gateway.execute(command())
        self.assertEqual((result.status, result.reason, result.command_id), ("accepted", None, "cmd-1"))
        session, command_id, action, pattern, percent, frequency, duration, ttl = self.peer.out_calls[0]
        self.assertEqual((session, command_id, action, pattern, percent, frequency, duration),
                         ("s-test", "cmd-1", "pixels.set_pattern", "notice", 25, 0, 0))
        self.assertTrue(0 < ttl <= 2000)
        tone = await self.gateway.execute(command(
            command_id="cmd-2", target="buzzer", action="short_tone",
            parameters={"frequency_hz": 880, "duration_ms": 200}))
        self.assertEqual(tone.status, "accepted")
        self.assertEqual(self.peer.out_calls[1][2:7], ("buzzer.short_tone", "", 0, 880, 200))

    async def test_linux_rejects_invalid_commands_without_sending(self):
        cases = {
            "incompatible_schema_version": command(schema_version="2.0"),
            "invalid_session": command(session_id="bad session"),
            "invalid_command_id": command(command_id="x" * 33),
            "unknown_action": command(target="gpio", action="write", parameters={}),
            "invalid_parameters": command(parameters={"pattern": "notice"}),
            "invalid_pattern": command(parameters={"pattern": "rainbow", "brightness": 0.5}),
            "invalid_brightness": command(parameters={"pattern": "notice", "brightness": 1.5}),
            "invalid_duration": command(target="buzzer", action="short_tone",
                                        parameters={"frequency_hz": 880, "duration_ms": 300}),
            "invalid_frequency": command(target="buzzer", action="short_tone",
                                         parameters={"frequency_hz": True, "duration_ms": 100}),
            "invalid_ttl": command(ttl_ms=0),
        }
        for reason, bad in cases.items():
            with self.subTest(reason):
                result = await self.gateway.execute(bad)
                self.assertEqual((result.status, result.reason), ("rejected", reason))
        self.assertEqual(self.peer.out_calls, [])

    async def test_duplicate_command_is_never_resent(self):
        await self.gateway.execute(command())
        duplicate = await self.gateway.execute(command())
        self.assertEqual((duplicate.status, duplicate.reason), ("rejected", "duplicate_command"))
        self.assertEqual(len(self.peer.out_calls), 1)
        # A new session may reuse IDs, as on the MCU.
        fresh = await self.gateway.execute(command(session_id="s-other"))
        self.assertEqual(fresh.status, "accepted")

    async def test_mcu_rejection_reason_is_reported(self):
        self.peer.out_response = {"v": "1.0", "r": "rejected", "why": "no_lease"}
        result = await self.gateway.execute(command())
        self.assertEqual((result.status, result.reason), ("rejected", "no_lease"))

    async def test_transport_failure_is_unknown_outcome_and_not_retried(self):
        self.peer.out_failure = TimeoutError("Request 'cc_out' timed out after 0.75s")
        result = await self.gateway.execute(command())
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.reason.startswith("mcu_unresponsive: "))
        self.assertTrue(result.reason.endswith("(outcome unknown)"))
        self.peer.out_failure = None
        retry = await self.gateway.execute(command())
        self.assertEqual(retry.reason, "duplicate_command")
        self.assertEqual(len(self.peer.out_calls), 1)

    async def test_time_waiting_in_gateway_comes_out_of_the_ttl(self):
        async with self.gateway._lock:  # a poll holds the gateway
            pending = asyncio.create_task(self.gateway.execute(command(ttl_ms=500)))
            await asyncio.sleep(0)
            self.clock.now += 0.2
        await pending
        self.assertLessEqual(self.peer.out_calls[-1][7], 300)
        async with self.gateway._lock:
            late = asyncio.create_task(self.gateway.execute(command(command_id="cmd-late", ttl_ms=100)))
            await asyncio.sleep(0)
            self.clock.now += 0.2
        result = await late
        self.assertEqual((result.status, result.reason), ("expired", "ttl_elapsed_in_gateway"))
        self.assertEqual(len(self.peer.out_calls), 1)

    async def test_heartbeat_renews_lease(self):
        lease = await self.gateway.heartbeat("s-test")
        self.assertEqual((lease.component_id, lease.state), ("output_lease", "ready"))
        self.peer.lease_data = {"v": "1.0", "ok": False, "why": "invalid_session"}
        refused = await self.gateway.heartbeat("s-test")
        self.assertEqual((refused.state, refused.reason), ("unavailable", "invalid_session"))
        self.assertEqual((await self.gateway.heartbeat("bad id")).reason, "invalid_session")

    async def test_read_outputs_reports_completion(self):
        self.peer.outputs_data = outputs_reading(pat="notice", b=25, lease=True,
                                                 last="cmd-1", o="completed")
        outputs = await self.gateway.read_outputs()
        self.assertEqual((outputs.pattern, outputs.brightness, outputs.lease_active),
                         ("notice", 0.25, True))
        self.assertEqual((outputs.last_result.command_id, outputs.last_result.status),
                         ("cmd-1", "completed"))
        first_seen = outputs.last_result.at
        self.clock.now += 5
        again = await self.gateway.read_outputs()
        self.assertEqual(again.last_result.at, first_seen)  # time of first observation

    async def test_malformed_output_report_does_not_affect_sensors(self):
        self.peer.outputs_data = outputs_reading(pat="rainbow")
        state = await self.gateway.read_state()
        health = {h.component_id: h for h in state.health}
        self.assertEqual(health["bridge"].state, "ready")
        self.assertEqual(health["thermo"].state, "ready")
        self.assertEqual(health["pixels"].state, "unavailable")
        self.assertIsNone(await self.gateway.read_outputs())

    async def test_old_firmware_has_no_outputs(self):
        peer = TestPeer()  # firmware 0.1.0
        gateway = HardwareGateway(peer, clock=self.clock)
        state = await gateway.read_state()
        self.assertNotIn("pixels", {h.component_id for h in state.health})
        result = await gateway.execute(command())
        self.assertEqual((result.status, result.reason), ("rejected", "outputs_not_supported_by_firmware"))
        await gateway.close()

    async def test_close_stops_outputs_for_the_leased_session(self):
        await self.gateway.heartbeat("s-test")
        await self.gateway.close()
        self.assertEqual(self.peer.out_calls[-1][0], "s-test")
        self.assertEqual(self.peer.out_calls[-1][2], "outputs.stop")

    async def test_session_ids_are_valid_for_the_mcu(self):
        session = HardwareGateway.new_session_id()
        self.assertRegex(session, r"^[A-Za-z0-9_-]{1,32}$")
        self.assertNotEqual(session, HardwareGateway.new_session_id())


class CliStopTests(unittest.TestCase):
    def test_sigterm_ends_with_a_stopped_record_and_clean_exit(self):
        import os
        import signal
        import subprocess

        app = Path(__file__).resolve().parents[2] / "apps/contextcue-hardware/python"
        # No Arduino Router here: the gateway keeps polling with failures,
        # which is exactly when a stop must still be recognizable.
        proc = subprocess.Popen(
            [sys.executable, "-m", "contextcue_hardware.cli", "--router", "unix:///nonexistent"],
            cwd=app, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            env={**os.environ, "PYTHONPATH": str(app)},
        )
        try:
            first = proc.stdout.readline()
            self.assertTrue(first, "CLI printed nothing before the stop")
            proc.send_signal(signal.SIGTERM)
            rest, _ = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        last = json.loads(rest.strip().splitlines()[-1])
        self.assertTrue(all(h["state"] == "stopped" for h in last["health"]))
        self.assertEqual(last["health"][0]["reason"], "gateway_stopped: SIGTERM received")


if __name__ == "__main__":
    unittest.main()
