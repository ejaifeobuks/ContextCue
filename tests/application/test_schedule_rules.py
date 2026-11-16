"""APP-04 schedule arithmetic and section 6 comfort/proximity/brightness rules."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (sets sys.path)
from contextcue_app.contracts import CalendarEvent
from contextcue_app.rules import (ComfortMonitor, ProximityMonitor, brightness)
from contextcue_app.schedule import calculate_schedule
from contextcue_app.settings import Settings, in_window, parse_hhmm

CHI = ZoneInfo("America/Chicago")


def ev(event_id, start, end, **kw):
    return CalendarEvent(event_id=event_id, title=event_id, start=start, end=end, **kw)


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class ScheduleTests(unittest.TestCase):
    def test_overlaps_are_merged_and_cancelled_excluded(self):
        events = [
            ev("a", "2026-11-16T16:00:00Z", "2026-11-16T17:00:00Z"),
            ev("b", "2026-11-16T16:30:00Z", "2026-11-16T18:00:00Z"),
            ev("c", "2026-11-16T19:00:00Z", "2026-11-16T20:00:00Z", status="cancelled"),
            ev("d", "2026-11-16T21:00:00Z", "2026-11-16T22:00:00Z"),
        ]
        facts = calculate_schedule(events, utc("2026-11-16T15:40:00Z"), CHI)
        self.assertEqual(facts.event_count, 3)
        self.assertEqual(facts.busy_ms, 3 * 3600_000)  # 16:00-18:00 + 21:00-22:00
        self.assertEqual(facts.next_event.event_id, "a")
        self.assertEqual(facts.next_event_in_ms, 20 * 60_000)
        self.assertEqual(facts.urgency, "soon")
        self.assertEqual(facts.excluded, {"c": "cancelled"})

    def test_invalid_intervals_and_times_are_excluded_not_guessed(self):
        events = [
            ev("bad", "2026-11-16T17:00:00Z", "2026-11-16T16:00:00Z"),
            ev("zero", "2026-11-16T17:00:00Z", "2026-11-16T17:00:00Z"),
            ev("garbage", "tomorrow", "later"),
            ev("naive", "2026-11-16T17:00:00", "2026-11-16T18:00:00"),
        ]
        facts = calculate_schedule(events, utc("2026-11-16T15:00:00Z"), CHI)
        self.assertEqual(facts.event_count, 0)
        self.assertEqual(facts.excluded, {"bad": "invalid_interval", "zero": "invalid_interval",
                                          "garbage": "invalid_time", "naive": "invalid_time"})
        self.assertIsNone(facts.next_event)

    def test_all_day_events_listed_separately(self):
        events = [ev("demo", "2026-11-16", "2026-11-17", all_day=True),
                  ev("other", "2026-11-17", "2026-11-18", all_day=True)]
        facts = calculate_schedule(events, utc("2026-11-16T15:00:00Z"), CHI)
        self.assertEqual(facts.all_day, ["demo"])
        self.assertEqual(facts.busy_ms, 0)
        self.assertEqual(facts.event_count, 0)

    def test_local_day_uses_zone_not_utc(self):
        # 23:30 local on 16 Nov is already 17 Nov in UTC.
        events = [ev("late", "2026-11-17T05:30:00Z", "2026-11-17T06:30:00Z")]
        facts = calculate_schedule(events, utc("2026-11-17T05:00:00Z"), CHI)
        self.assertEqual(facts.local_date, "2026-11-16")
        self.assertEqual(facts.busy_ms, 30 * 60_000)  # clipped at local midnight

    def test_dst_fall_back_day_has_25_hours(self):
        # 1 Nov 2026 in Chicago: clocks go back, the local day is 25 h long.
        events = [ev("all", "2026-11-01T05:00:00Z", "2026-11-02T06:00:00Z")]
        facts = calculate_schedule(events, utc("2026-11-01T12:00:00Z"), CHI)
        self.assertEqual(facts.busy_ms, 25 * 3600_000)

    def test_dst_spring_forward_day_has_23_hours(self):
        events = [ev("all", "2026-03-08T06:00:00Z", "2026-03-09T05:00:00Z")]
        facts = calculate_schedule(events, utc("2026-03-08T12:00:00Z"), CHI)
        self.assertEqual(facts.busy_ms, 23 * 3600_000)

    def test_current_and_imminent(self):
        events = [ev("now", "2026-11-16T15:00:00Z", "2026-11-16T16:00:00Z"),
                  ev("next", "2026-11-16T15:58:00Z", "2026-11-16T16:30:00Z")]
        facts = calculate_schedule(events, utc("2026-11-16T15:50:00Z"), CHI)
        self.assertEqual([e.event_id for e in facts.current_events], ["now"])
        self.assertEqual(facts.urgency, "imminent")
        self.assertEqual(facts.busy_ms, 90 * 60_000)

    def test_duplicate_ids_counted_once(self):
        events = [ev("x", "2026-11-16T16:00:00Z", "2026-11-16T17:00:00Z"),
                  ev("x", "2026-11-16T18:00:00Z", "2026-11-16T19:00:00Z")]
        facts = calculate_schedule(events, utc("2026-11-16T15:00:00Z"), CHI)
        self.assertEqual(facts.event_count, 1)
        self.assertEqual(facts.excluded, {"x": "duplicate_id"})


class WindowTests(unittest.TestCase):
    def test_window_across_midnight(self):
        start, end = parse_hhmm("22:00"), parse_hhmm("07:00")
        self.assertTrue(in_window(parse_hhmm("23:00"), start, end))
        self.assertTrue(in_window(parse_hhmm("06:59"), start, end))
        self.assertFalse(in_window(parse_hhmm("07:00"), start, end))
        self.assertFalse(in_window(parse_hhmm("12:00"), start, end))


class ComfortTests(unittest.TestCase):
    def setUp(self):
        self.m = ComfortMonitor()
        self.s = Settings()

    def test_requires_60_seconds_of_persistence(self):
        r = self.m.update(0, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertEqual(r.active, {})
        r = self.m.update(59.9, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertEqual(r.newly_eligible, [])
        r = self.m.update(60, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertEqual(r.newly_eligible, ["hot"])

    def test_dip_restarts_persistence(self):
        self.m.update(0, {"temperature": 27.0, "humidity": 45}, self.s)
        self.m.update(30, {"temperature": 25.0, "humidity": 45}, self.s)
        r = self.m.update(70, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertEqual(r.active, {})

    def test_hysteresis_and_cooldown(self):
        self.m.update(0, {"temperature": 27.0, "humidity": 45}, self.s)
        self.m.update(60, {"temperature": 27.0, "humidity": 45}, self.s)
        self.m.mark_notified("hot", 60)
        # 25.8 is inside the range but not below the 25.5 clear threshold.
        r = self.m.update(70, {"temperature": 25.8, "humidity": 45}, self.s)
        self.assertIn("hot", r.active)
        r = self.m.update(80, {"temperature": 25.4, "humidity": 45}, self.s)
        self.assertEqual(r.cleared, ["hot"])
        # Becomes hot again quickly: active, but cooldown (15 min) blocks a new cue.
        self.m.update(90, {"temperature": 27.0, "humidity": 45}, self.s)
        r = self.m.update(150, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertIn("hot", r.active)
        self.assertEqual(r.newly_eligible, [])
        r = self.m.update(60 + 900, {"temperature": 27.0, "humidity": 45}, self.s)
        self.assertEqual(r.newly_eligible, ["hot"])

    def test_unavailable_input_disables_and_clears(self):
        self.m.update(0, {"temperature": 27.0, "humidity": 45}, self.s)
        self.m.update(60, {"temperature": 27.0, "humidity": 45}, self.s)
        r = self.m.update(61, {"temperature": None, "humidity": 45}, self.s)
        self.assertFalse(r.available)
        self.assertEqual(r.cleared, ["hot"])
        self.assertNotIn("hot", r.active)

    def test_humidity_conditions(self):
        self.m.update(0, {"temperature": 22, "humidity": 70}, self.s)
        r = self.m.update(60, {"temperature": 22, "humidity": 70}, self.s)
        self.assertEqual(r.newly_eligible, ["humid"])


class ProximityTests(unittest.TestCase):
    def test_wake_needs_persistence_and_a_prior_clear(self):
        p = ProximityMonitor()
        p.update(0.0, 900)
        self.assertEqual(p.update(0.5, 900).state, "clear")
        self.assertFalse(p.update(1.0, 400).near_transition)
        self.assertFalse(p.update(1.3, 400).near_transition)
        self.assertTrue(p.update(1.5, 400).near_transition)
        # Stationary object: no repeated wake.
        self.assertFalse(p.update(5.0, 400).near_transition)
        self.assertEqual(p.update(6.0, 400).state, "near")

    def test_object_present_at_start_does_not_wake(self):
        p = ProximityMonitor()
        p.update(0.0, 400)
        self.assertFalse(p.update(0.6, 400).near_transition)
        self.assertEqual(p.confirmed, "near")

    def test_hysteresis_band_and_unknown(self):
        p = ProximityMonitor()
        p.update(0.0, 900)
        p.update(0.6, 900)
        self.assertEqual(p.update(1.0, 600).state, "clear")  # 500-650 keeps state
        status = p.update(1.1, None, "distance_bus_error")
        self.assertEqual((status.state, status.reason), ("unknown", "distance_bus_error"))
        self.assertFalse(status.near_transition)
        # An unknown (faulty) reading resets persistence.
        p.update(1.2, 400)
        p.update(1.5, None, "distance_disconnected")
        self.assertFalse(p.update(2.0, 400).near_transition)
        self.assertTrue(p.update(2.5, 400).near_transition)

    def test_nothing_in_range_is_clear_so_an_approach_wakes(self):
        # Empty desk: the sensor answers "nothing in range" (distance_timeout).
        p = ProximityMonitor()
        p.update(0.0, None, "distance_timeout")
        status = p.update(0.6, None, "distance_timeout")
        self.assertEqual((status.state, status.reason), ("clear", None))
        p.update(1.0, 420)
        self.assertTrue(p.update(1.5, 420).near_transition)
        # A single "nothing in range" blip while sitting there is not a new approach.
        p.update(2.0, None, "distance_timeout")
        p.update(2.1, 420)
        self.assertFalse(p.update(3.0, 420).near_transition)

    def test_flicker_through_unknown_does_not_rewake(self):
        p = ProximityMonitor()
        p.update(0, 900); p.update(0.6, 900)
        p.update(1, 400); self.assertTrue(p.update(1.5, 400).near_transition)
        p.update(2, None, "distance_timeout")
        p.update(2.1, 400)
        self.assertFalse(p.update(3, 400).near_transition)


class BrightnessTests(unittest.TestCase):
    def test_range_fallback_and_override(self):
        s = Settings()
        dark, bright = brightness(0, s), brightness(5000, s)
        self.assertEqual((dark["screen"], dark["pixels"]), (0.2, 0.05))
        self.assertEqual((bright["screen"], bright["pixels"]), (1.0, 0.4))
        failed = brightness(None, s)
        self.assertEqual((failed["screen"], failed["pixels"]), (0.6, 0.15))
        manual = brightness(0, s.updated({"brightness_override": 0.6}))
        self.assertEqual(manual["screen"], 0.6)
        self.assertEqual(manual["source"], "manual_override")


class SettingsTests(unittest.TestCase):
    def test_validation(self):
        s = Settings()
        with self.assertRaises(ValueError):
            s.updated({"comfort_temp_min_c": 30})
        with self.assertRaises(ValueError):
            s.updated({"time_zone": "Mars/Base"})
        with self.assertRaises(ValueError):
            s.updated({"speech_volume": 101})
        with self.assertRaises(ValueError):
            s.updated({"nonsense": 1})
        with self.assertRaises(ValueError):
            s.updated({"buzzer_enabled": "yes"})
        self.assertEqual(s.updated({"speech_volume": 40.0}).speech_volume, 40)


if __name__ == "__main__":
    unittest.main()
