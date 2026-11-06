"""Contract/fault tests only; test peers never replace the real runtime adapter."""

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps/contextcue-hardware/python"))

from contextcue_contracts import Quality
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
        result = {
            "cc_open": self.info,
            "cc_read": self.data,
            "cc_read_light": self.light_data,
            "cc_read_distance": self.distance_data,
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
