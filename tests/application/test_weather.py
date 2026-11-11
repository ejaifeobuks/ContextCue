"""Live weather (Open-Meteo), forecast location and fixtures on the device clock.
No network: Open-Meteo replies are recorded-style dictionaries."""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402

from contextcue_app import ActionError, ContextCueApp  # noqa: E402
from contextcue_app.clock import SystemClock, parse_iso  # noqa: E402
from contextcue_app.dev_gateway import SimulatedGateway  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.settings import Settings  # noqa: E402
from contextcue_app.sources import FixtureCalendarAdapter, demo_shift, load_scenarios  # noqa: E402
from contextcue_app.weather import (OpenMeteoWeatherAdapter, condition_for, group_periods,  # noqa: E402
                                    search_locations)

CHICAGO = ZoneInfo("America/Chicago")
HUNTSVILLE = {"name": "Huntsville", "region": "Alabama", "country": "United States",
              "latitude": 34.7304, "longitude": -86.58594, "time_zone": "America/Chicago"}


def hourly(start_utc="2026-11-09T12:00", hours=6, codes=None, rain=None, temps=None):
    base = datetime.fromisoformat(start_utc)
    return {"time": [(base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)],
            "weather_code": codes or [3] * hours,
            "precipitation_probability": rain or [10] * hours,
            "temperature_2m": temps or [12.0] * hours}


class ConditionAndGroupingTests(unittest.TestCase):
    def test_wmo_codes_map_to_the_five_conditions(self):
        expected = {0: "sunny", 1: "sunny", 2: "cloudy", 3: "cloudy", 45: "foggy", 48: "foggy",
                    51: "rainy", 63: "rainy", 81: "rainy", 95: "rainy", 99: "rainy",
                    71: "snowy", 77: "snowy", 86: "snowy"}
        for code, condition in expected.items():
            self.assertEqual(condition_for(code), condition, code)
        self.assertIsNone(condition_for(None))
        self.assertIsNone(condition_for(42))  # unknown codes never become a guess

    def test_hours_group_into_local_three_hour_periods(self):
        # 12:00-17:00 UTC is 06:00-11:00 CST: periods 06-09 and 09-12 local.
        data = hourly(codes=[3, 3, 0, 61, 3, 3], rain=[10, 20, 5, 70, 40, 30],
                      temps=[8.0, 9.0, 10.0, 11.0, 12.0, 13.0])
        first, second = group_periods(data, CHICAGO, "Huntsville, Alabama")
        self.assertEqual((first.start, first.end), ("2026-11-09T12:00:00.000Z", "2026-11-09T15:00:00.000Z"))
        self.assertEqual((first.condition, first.precipitation_probability, first.temperature_c),
                         ("cloudy", 20.0, 9.0))
        self.assertEqual((second.condition, second.precipitation_probability), ("rainy", 70.0))
        self.assertEqual(second.location, "Huntsville, Alabama")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = {"hourly": hourly()}
        self.mono = 0.0

    def fetcher(self, url, timeout):
        self.calls.append(url)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def adapter(self, location=HUNTSVILLE):
        return OpenMeteoWeatherAdapter(location, fetcher=self.fetcher, monotonic=lambda: self.mono,
                                       background=False)

    def test_no_location_is_disabled_and_never_fetches(self):
        result = self.adapter(None).fetch(datetime(2026, 11, 9, 13, tzinfo=timezone.utc))
        self.assertEqual((result.quality, result.error, result.items), ("disabled", "no_location_set", ()))
        self.assertEqual(self.calls, [])

    def test_valid_then_cached_then_stale(self):
        adapter = self.adapter()
        now = datetime(2026, 11, 9, 13, tzinfo=timezone.utc)
        result = adapter.fetch(now)
        self.assertEqual((result.quality, result.source_mode), ("valid", "real"))
        self.assertIn("latitude=34.7304", self.calls[0])
        self.assertIn("Open-Meteo", result.label)
        self.mono += 60
        adapter.fetch(now + timedelta(minutes=1))
        self.assertEqual(len(self.calls), 1)  # cached between refreshes
        # The refresh fails; the old forecast stays until it is 3 h old, then is stale.
        self.reply = OSError("no route")
        self.mono += 31 * 60
        later = adapter.fetch(now + timedelta(minutes=32))
        self.assertEqual((later.quality, later.error), ("valid", "network_error: OSError"))
        self.assertTrue(later.items)
        self.mono += 3 * 3600
        stale = adapter.fetch(now + timedelta(hours=4))
        self.assertEqual(stale.quality, "stale")

    def test_failure_before_any_forecast_is_unavailable_not_faked(self):
        self.reply = {"unexpected": True}
        result = self.adapter().fetch(datetime(2026, 11, 9, 13, tzinfo=timezone.utc))
        self.assertEqual((result.quality, result.error, result.items),
                         ("unavailable", "malformed_forecast", ()))


class LocationSearchTests(unittest.TestCase):
    def test_results_are_parsed_and_malformed_entries_dropped(self):
        reply = {"results": [
            {"name": "Huntsville", "admin1": "Alabama", "country": "United States",
             "latitude": 34.7304, "longitude": -86.58594, "timezone": "America/Chicago"},
            {"name": "Nowhere", "latitude": 1, "longitude": 2, "timezone": "Not/AZone"},
            {"name": "Broken"}]}
        places = search_locations("Huntsville", fetcher=lambda url, timeout: reply)
        self.assertEqual(places, [HUNTSVILLE])
        self.assertEqual(search_locations("H", fetcher=lambda *a: reply), [])  # too short

    def test_location_setting_is_validated(self):
        settings = Settings().updated({"location": HUNTSVILLE})
        self.assertEqual(settings.location["time_zone"], "America/Chicago")
        for bad in ({**HUNTSVILLE, "latitude": 95}, {**HUNTSVILLE, "time_zone": "Mars/Base"},
                    {**HUNTSVILLE, "name": ""}, "Huntsville"):
            with self.assertRaises(ValueError):
                Settings().updated({"location": bad})


class DeviceClockTests(unittest.IsolatedAsyncioTestCase):
    def test_scenario_replays_from_now_so_events_are_always_ahead(self):
        scenario = load_scenarios()["event_rain"]  # 09:38 demo time, review at 10:00
        now = datetime(2026, 11, 9, 3, 4, 30, tzinfo=timezone.utc)  # 21:04 CST on 8 November
        adapter = FixtureCalendarAdapter(scenario.calendar, demo_shift(scenario, now))
        result = adapter.fetch(now)
        self.assertEqual(result.quality, "valid")
        review = next(e for e in result.items if e.event_id == "evt-review")
        self.assertEqual(parse_iso(review.start) - now.replace(second=0), timedelta(minutes=22))
        demo_day = next(e for e in result.items if e.all_day)
        self.assertEqual(demo_day.start, "2026-11-08")  # all-day event moved onto the local day
        self.assertLessEqual(parse_iso(result.observed_at), now)  # never observed in the future
        self.assertEqual(parse_iso(result.valid_until) - parse_iso(scenario.calendar["valid_until"]),
                         now.replace(second=0) - scenario.start)

    async def test_app_on_device_clock_shows_real_time_and_switches_scenarios(self):
        scenarios = load_scenarios()
        clock = SystemClock()
        gateway = SimulatedGateway(monotonic=clock.monotonic, utcnow=clock.now)
        repo = DataRepository()
        self.addCleanup(repo.close)
        app = ContextCueApp(gateway=gateway, clock=clock, repository=repo, gateway_mode="simulated",
                            scenario_id="morning", scenarios=scenarios)
        await app._tick()
        view = app.view_model()
        self.assertEqual((view["clock"]["mode"], view["clock"]["label"]), ("real", "Device time"))
        self.assertFalse(view["simulation"]["time_controls"])
        self.assertEqual(view["schedule"]["quality"], "valid")  # fixture moved onto today
        result = await app.action("sim_scenario", {"scenario_id": "event_rain"})
        self.assertEqual(result["status"], "loaded")
        with self.assertRaises(ActionError):
            await app.action("sim_advance", {"seconds": 60})  # only with the scenario clock
        await app.stop()

    async def test_set_location_switches_time_zone_and_live_adapter(self):
        scenarios = load_scenarios()
        clock = support.frozen_clock(scenarios["morning"].start)
        gateway = SimulatedGateway(monotonic=clock.monotonic, utcnow=clock.now)
        repo = DataRepository()
        self.addCleanup(repo.close)
        app = ContextCueApp(gateway=gateway, clock=clock, repository=repo, gateway_mode="simulated",
                            scenario_id="morning", scenarios=scenarios)
        place = {**HUNTSVILLE, "name": "Phoenix", "region": "Arizona", "time_zone": "America/Phoenix"}
        await app.action("set_location", {"location": place})
        self.assertEqual(app.settings.time_zone, "America/Phoenix")
        self.assertEqual(app.settings.location["name"], "Phoenix")
        with self.assertRaises(ActionError):
            await app.action("set_location", {"location": {**place, "latitude": 400}})
        # The generic settings action cannot write a location.
        await app.action("settings", {"changes": {"location": None, "units": "imperial"}})
        self.assertEqual(app.settings.location["name"], "Phoenix")
        await app.action("settings", {"changes": {"weather_source": "open_meteo"}})
        self.assertIsInstance(app._weather, OpenMeteoWeatherAdapter)
        self.assertEqual(app._weather.location["name"], "Phoenix")
        app._weather._background = False
        app._weather._fetcher = lambda url, timeout: (_ for _ in ()).throw(OSError("offline"))
        await app._tick()
        view = app.view_model()
        self.assertEqual(view["forecast"]["quality"], "unavailable")  # never fixture data
        self.assertEqual(view["forecast"]["source_mode"], "real")
        await app.stop()


if __name__ == "__main__":
    unittest.main()
