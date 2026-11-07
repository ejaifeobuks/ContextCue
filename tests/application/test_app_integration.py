"""End-to-end application scenarios on the simulated gateway, plus one run on
the real HardwareGateway driven by the hardware tests' fake Bridge peer."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402

from contextcue_app import ActionError, ContextCueApp  # noqa: E402
from contextcue_app.dev_gateway import SimulatedGateway  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.sources import load_scenarios  # noqa: E402

SCENARIOS = load_scenarios()


class AppHarness(unittest.IsolatedAsyncioTestCase):
    def build(self, scenario="event_rain", repo=None):
        self.clock = support.frozen_clock(SCENARIOS[scenario].start)
        self.gw = SimulatedGateway(monotonic=self.clock.monotonic, utcnow=self.clock.now)
        sensors = SCENARIOS[scenario].sensors
        for metric, key in (("temperature", "temperature"), ("humidity", "humidity"),
                            ("ambient_light", "lux"), ("distance", "distance_mm")):
            self.gw.set_value(metric, sensors[key])
        if repo is None:
            repo = DataRepository()
            self.addCleanup(repo.close)
        self.repo = repo
        self.app = ContextCueApp(gateway=self.gw, clock=self.clock, repository=repo,
                                 gateway_mode="simulated", scenario_id=scenario, scenarios=SCENARIOS)
        return self.app

    async def step(self, seconds=1.0, n=1):
        for _ in range(n):
            self.clock.advance(seconds)
            await self.app._tick()
            await self.settle()

    async def settle(self):
        """Let in-flight generation callbacks finish (loop scheduling differs
        between Python versions)."""
        for _ in range(100):
            await asyncio.sleep(0)
            if not self.app._pending and not self.app.recommender.busy:
                break

    def active(self, category=None):
        return [n for n in self.app.notifications.notices.values()
                if n.lifecycle.value == "active" and (category is None or n.category == category)]


class PreparationFlow(AppHarness):
    async def test_event_with_rain_escalates_then_expires_at_start(self):
        self.build("event_rain")
        await self.step(0.1)
        await self.step()
        [prep] = self.active("preparation")
        self.assertEqual(prep.priority, 1)
        self.assertEqual(prep.origin, "factual_fallback")
        self.assertEqual(prep.request_outcome, "error")  # no model configured: labeled
        self.assertEqual(prep.source_mode, "simulated")
        self.assertIn("Design review starts at 10:00", prep.text)
        self.assertIn("70%", prep.text)
        await self.step(60, 12)  # 09:50
        self.assertEqual(prep.priority, 2)
        self.assertEqual(len(self.active("preparation")), 1)  # deduplicated, not repeated
        await self.step(60, 10)  # 10:00
        self.assertEqual(prep.lifecycle.value, "expired")

    async def test_acknowledgement_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DataRepository(Path(tmp) / "cc.db")
            self.build("event_rain", repo=repo)
            await self.step(0.1)
            await self.step()
            [prep] = self.active("preparation")
            await self.app.action("acknowledge", {"notice_id": prep.notice_id})
            await self.app.action("focus", {"enabled": True})
            repo.close()
            repo2 = DataRepository(Path(tmp) / "cc.db")
            self.addCleanup(repo2.close)
            self.build("event_rain", repo=repo2)
            await self.step(0.1)
            await self.step(1, 3)
            self.assertEqual(self.active("preparation"), [])
            self.assertTrue(self.app.notifications.focus)  # settings restored
            with self.assertRaises(ActionError):
                await self.app.action("acknowledge", {"notice_id": prep.notice_id})


class MorningFlow(AppHarness):
    async def test_approach_requests_briefing_once_per_date(self):
        self.build("morning")
        await self.step(0.2, 5)  # clear confirmed
        self.assertEqual(self.active("morning"), [])
        self.gw.set_value("distance", 350)
        await self.step(0.2, 5)
        [morning] = self.active("morning")
        self.assertIn("4 events today", morning.text)
        self.assertIn("Indoors 22.5 °C", morning.text)
        await self.app.action("acknowledge", {"notice_id": morning.notice_id})
        self.gw.set_value("distance", 1200)
        await self.step(0.2, 5)
        self.gw.set_value("distance", 350)
        await self.step(0.2, 5)
        self.assertEqual(self.active("morning"), [])
        # On-request briefing is still available.
        await self.app.action("briefing")
        await self.settle()
        await self.step(0.2)
        self.assertEqual(len(self.active("morning")), 1)

    async def test_focus_defers_the_automatic_briefing(self):
        self.build("morning")
        await self.app.action("focus", {"enabled": True})
        await self.step(0.2, 5)
        self.gw.set_value("distance", 350)
        await self.step(0.2, 5)
        deferred = [n for n in self.app.notifications.notices.values() if n.lifecycle.value == "deferred"]
        self.assertEqual(len(deferred), 1)
        await self.app.action("focus", {"enabled": False})
        await self.step(0.2, 6)
        self.assertEqual(len(self.active("morning")), 1)

    async def test_no_briefing_when_distance_is_unknown(self):
        self.build("morning")
        await self.step(0.2, 5)
        await self.app.action("sim_fault", {"target": "distance", "active": True})
        self.gw.set_value("distance", 350)
        await self.step(0.2, 10)
        self.assertEqual(self.active("morning"), [])
        self.assertEqual(self.app.view_model()["indoor"]["proximity"], "unknown")


class ComfortFlow(AppHarness):
    async def test_comfort_after_persistence_and_ends_on_sensor_loss(self):
        self.build("comfort")
        await self.step(1, 30)
        self.assertEqual(self.active("comfort"), [])
        await self.step(1, 32)
        [comfort] = self.active("comfort")
        self.assertIn("27.4 °C", comfort.text)
        self.assertEqual(comfort.priority, 0)
        await self.app.action("sim_fault", {"target": "thermo", "active": True})
        await self.step(1, 2)
        self.assertEqual(comfort.lifecycle.value, "expired")
        self.assertIn("derived.comfort.hot", comfort.ended_reason)
        vm = self.app.view_model()
        self.assertFalse(vm["indoor"]["comfort_available"])
        self.assertEqual(vm["indoor"]["temperature"]["quality"], "unavailable")

    async def test_questions(self):
        self.build("comfort")
        await self.step(0.1)
        await self.step()
        with self.assertRaises(ActionError):
            await self.app.action("ask", {"question": "what is the meaning of life"})
        await self.app.action("ask", {"question": "room_comfort"})
        await self.settle()
        await self.step(0.1)
        [answer] = self.active("context_answer")
        self.assertEqual(answer.text, "Indoors 27.4 °C, 48% RH.")
        await self.app.action("sim_fault", {"target": "weather", "active": True})
        await self.step()
        await self.app.action("ask", {"question": "rain_today"})
        await self.settle()
        await self.step(0.1)
        status = [n for n in self.active("context_answer") if n.origin == "status"]
        self.assertEqual(len(status), 1)
        self.assertIn("Can't answer", status[0].text)


class FaultsAndOutputs(AppHarness):
    async def test_bridge_loss_and_recovery_resynchronizes_pixels(self):
        self.build("event_rain")
        await self.step(0.1)
        await self.step()
        pixel_cmds = [c for c in self.gw.commands if c.target == "pixels"]
        self.assertEqual(pixel_cmds[-1].parameters["pattern"], "notice")
        await self.app.action("sim_fault", {"target": "bridge", "active": True})
        await self.step()
        vm = self.app.view_model()
        bridge = next(h for h in vm["health"] if h["component_id"] == "bridge")
        self.assertEqual(bridge["state"], "unavailable")
        self.assertEqual(vm["indoor"]["temperature"]["quality"], "unavailable")
        self.assertEqual(len(self.active("preparation")), 1)  # calendar cue unaffected
        count = len(self.gw.commands)
        await self.app.action("sim_fault", {"target": "bridge", "active": False})
        await self.step()
        await self.step()
        new = [c for c in self.gw.commands[count:] if c.target == "pixels"]
        self.assertEqual(new[0].parameters["pattern"], "notice")

    async def test_missing_pixels_module_is_not_hammered(self):
        self.build("event_rain")
        await self.app.action("sim_fault", {"target": "pixels", "active": True})
        await self.step(0.1, 30)
        self.assertEqual([c for c in self.gw.commands if c.target == "pixels"], [])
        pixels = next(h for h in self.app.view_model()["health"] if h["component_id"] == "pixels")
        self.assertEqual((pixels["state"], pixels["reason"]), ("unavailable", "pixels_not_found"))
        await self.app.action("sim_fault", {"target": "pixels", "active": False})
        await self.step(0.1, 2)
        self.assertEqual(len([c for c in self.gw.commands if c.target == "pixels"]), 1)

    async def test_calendar_adapter_loss_is_labeled_not_replaced(self):
        self.build("event_rain")
        await self.step(0.1)
        await self.step()
        await self.app.action("sim_fault", {"target": "calendar", "active": True})
        await self.step()
        vm = self.app.view_model()
        self.assertEqual(vm["schedule"]["quality"], "unavailable")
        self.assertEqual(vm["schedule"]["error"], "simulated_adapter_loss")
        self.assertNotIn("next_event", vm["schedule"])
        self.assertEqual(self.active("preparation"), [])  # evidence gone, cue expired

    async def test_live_source_choice_is_disabled_not_faked(self):
        self.build("event_rain")
        await self.app.action("settings", {"changes": {"calendar_source": "google"}})
        await self.step(0.1)
        await self.step()
        vm = self.app.view_model()
        self.assertEqual(vm["schedule"]["quality"], "disabled")
        self.assertEqual(vm["schedule"]["error"], "live_adapter_not_connected")

    async def test_view_model_labels(self):
        self.build("quiet_hours")
        await self.step(0.1)
        await self.step()
        vm = self.app.view_model()
        self.assertEqual(vm["clock"]["label"], "Scenario time (simulated)")
        self.assertEqual(vm["clock"]["local_time"], "22:30")
        self.assertTrue(vm["status"]["quiet_hours"])
        self.assertEqual(vm["forecast"]["quality"], "stale")  # demo forecast ended at 18:00
        self.assertEqual(vm["indoor"]["temperature"]["source_mode"], "simulated")


class RealGatewayContract(unittest.IsolatedAsyncioTestCase):
    async def test_engine_consumes_real_gateway_state(self):
        sys.path.insert(0, str(support.ROOT / "tests" / "hardware"))
        from test_gateway import TestClock, TestPeer
        from contextcue_hardware.gateway import HardwareGateway

        peer, hw_clock = TestPeer(), TestClock()
        gateway = HardwareGateway(peer, clock=hw_clock)
        clock = support.frozen_clock(SCENARIOS["event_rain"].start)
        repo = DataRepository()
        self.addCleanup(repo.close)
        app = ContextCueApp(gateway=gateway, clock=clock, repository=repo, gateway_mode="hardware",
                            scenario_id="event_rain", scenarios=SCENARIOS)
        await app._tick()
        vm = app.view_model()
        self.assertEqual(vm["indoor"]["temperature"]["value"], 24.25)
        self.assertEqual(vm["indoor"]["temperature"]["source_mode"], "real")
        self.assertEqual(vm["indoor"]["ambient_light"]["error"], "light_not_found")
        self.assertEqual(vm["indoor"]["distance"]["value"], 430)
        with self.assertRaises(ActionError):
            await app.action("sim_sensor", {"metric": "temperature", "value": 30})
        await gateway.close()


if __name__ == "__main__":
    unittest.main()
