"""Contract/fault tests only; test peers never replace the real runtime adapter."""

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/contextcue_hardware/python"))

from contextcue.contracts import ProtocolError, Quality
from contextcue.gateway import HardwareGateway

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


class TestPeer:
    def __init__(self):
        self.info = {"v": "1.0", "boot": BOOT, "fw": "0.1.0", "ok": True,
                     "driver": "modulino", "core": "1.0.0", "bridge": "0.4.4", "modulino": "0.10.0",
                     "hs": "1.0.1", "sht": "0.5.3"}
        self.data = reading()
        self.calls = []
        self.failure = None
        self.closed = False

    def connect(self, timeout):
        if self.failure:
            raise self.failure

    def call(self, method, *args, timeout):
        self.calls.append((method, args, timeout))
        if self.failure:
            raise self.failure
        result = self.info if method == "cc_open" else self.data
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
        temperature, humidity = state.samples
        self.assertEqual(temperature.value, 24.25)
        self.assertEqual(humidity.unit, "%RH")
        self.assertEqual(temperature.source_mode, "real")
        self.assertEqual(temperature.observed_at, "2026-10-27T00:01:39.900Z")
        self.assertEqual(temperature.event_id, f"{BOOT}:1:temperature")
        self.assertTrue(all(h.state == "ready" for h in state.health))

    async def test_zero_is_a_valid_measurement(self):
        self.peer.data = reading(t=0, h=0)
        state = await self.gateway.read_state()
        self.assertTrue(all(s.value == 0 and s.quality == Quality.VALID for s in state.samples))

    async def test_repeated_poll_does_not_refresh_old_observation(self):
        first = await self.gateway.read_state()
        self.clock.now += 6
        self.peer.data["up"] += 6000
        repeated = await self.gateway.read_state()
        self.assertEqual(first.samples[0].observed_at, repeated.samples[0].observed_at)
        self.assertEqual(first.samples[0].received_at, repeated.samples[0].received_at)
        self.assertTrue(all(s.quality == Quality.STALE for s in repeated.samples))
        self.assertEqual(repeated.health[0].state, "ready")
        self.assertEqual(repeated.health[1].state, "unavailable")

    async def test_new_sample_restores_freshness(self):
        await self.gateway.read_state()
        self.clock.now += 6
        self.peer.data = reading(seq=2, up=7100, at=7000)
        state = await self.gateway.read_state()
        self.assertEqual(state.samples[0].quality, Quality.VALID)
        self.assertEqual(state.samples[0].seq, 2)

    async def test_sensor_disconnect_keeps_bridge_healthy(self):
        self.peer.data = reading(q="unavailable", t=None, h=None, err="thermo_disconnected")
        state = await self.gateway.read_state()
        self.assertIsNone(state.samples[0].value)
        self.assertEqual(state.health[0].state, "ready")
        self.assertEqual(state.health[1].reason, "thermo_disconnected")

    async def test_transport_failure_invalidates_previously_valid_values(self):
        await self.gateway.read_state()
        self.peer.failure = TimeoutError("RPC deadline")
        state = await self.gateway.read_state()
        self.assertTrue(all(s.value is None and s.quality == Quality.UNAVAILABLE for s in state.samples))
        self.assertEqual(state.health[0].state, "unavailable")
        self.peer.failure = None
        recovered = await self.gateway.read_state()
        self.assertEqual(recovered.samples[0].value, 24.25)
        self.assertEqual(recovered.health[0].state, "ready")

    async def test_reboot_discards_old_boot_and_sequence(self):
        await self.gateway.read_state()
        new_boot = "b" * 32
        self.peer.info["boot"] = new_boot
        self.peer.data = reading(boot=new_boot, seq=1, up=100, at=50)
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
        second = await self.gateway.read_state()
        self.assertNotEqual(first.boot_id, second.boot_id)
        self.assertEqual(second.health[0].state, "ready")

    async def test_health_expires_without_further_polling(self):
        await self.gateway.read_state()
        self.clock.now += 3
        bridge, sensor = self.gateway.get_device_health()
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
        self.assertEqual(state.samples[0].value, 24.25)
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
                self.assertEqual(state.health[0].state, "unavailable")

    async def test_invalid_sensor_fields_do_not_become_facts(self):
        for changes in ({"t": True}, {"t": float("nan")}, {"h": 101},
                        {"at": 1200}, {"seq": -1}, {"up": True},
                        {"q": "unknown"}, {"t": None}, {"err": "failure"}):
            with self.subTest(changes=changes):
                self.peer.data = reading(**changes)
                state = await self.gateway.read_state()
                self.assertTrue(all(s.quality != Quality.VALID for s in state.samples))
                self.assertEqual(state.health[0].state, "unavailable")

    async def test_failed_sample_must_have_null_values(self):
        self.peer.data = reading(q="invalid", err="read_failed")
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].state, "unavailable")

    async def test_sequence_and_uptime_regressions_are_rejected(self):
        await self.gateway.read_state()
        self.peer.data = reading(seq=0)
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].state, "unavailable")
        self.peer.data = reading(seq=2, up=500, at=400)
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].state, "unavailable")

    async def test_sample_cannot_change_under_same_sequence(self):
        await self.gateway.read_state()
        self.peer.data["t"] = 30
        state = await self.gateway.read_state()
        self.assertEqual(state.health[0].state, "unavailable")

    async def test_capabilities_only_advertise_implemented_features(self):
        capabilities = await self.gateway.get_capabilities()
        self.assertEqual(capabilities.configured_modules, ("thermo",))
        self.assertEqual(capabilities.output_patterns, ())
        self.assertFalse(capabilities.native_brightness)
        self.assertIsNone(capabilities.audio_adapter_version)
        self.assertNotIn("execute", capabilities.supported_operations)

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


if __name__ == "__main__":
    unittest.main()
