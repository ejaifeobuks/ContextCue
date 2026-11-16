"""Sunrise and sunset calculated on the board (contextcue_app.sun)."""

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

from contextcue_app.sun import sun_times, sun_view  # noqa: E402


def hm(t):
    return t.hour * 60 + t.minute


class SunTests(unittest.TestCase):
    def assertNear(self, actual, hhmm, tolerance=3):
        h, m = map(int, hhmm.split(":"))
        self.assertLessEqual(abs(hm(actual) - (h * 60 + m)), tolerance, f"{actual:%H:%M} vs {hhmm}")

    def test_matches_published_times(self):
        # Published sunrise/sunset tables (minutes).
        london = ZoneInfo("Europe/London")
        rise, set_ = sun_times(date(2026, 6, 21), 51.5074, -0.1278, london)
        self.assertNear(rise, "04:43")
        self.assertNear(set_, "21:21")
        rise, set_ = sun_times(date(2026, 12, 21), 51.5074, -0.1278, london)
        self.assertNear(rise, "08:04")
        self.assertNear(set_, "15:53")

    def test_polar_night_has_no_sunrise(self):
        self.assertIsNone(sun_times(date(2026, 12, 21), 69.65, 18.96, ZoneInfo("Europe/Oslo")))

    def test_view_uses_the_display_zone_and_needs_a_location(self):
        zone = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 11, 16, 8, 23, tzinfo=timezone.utc)  # 00:23 in San Diego
        view = sun_view(now, {"latitude": 32.71571, "longitude": -117.16472}, zone)
        self.assertEqual(set(view), {"sunrise", "sunset"})
        self.assertTrue("06:15" <= view["sunrise"] <= "06:25", view)
        self.assertTrue("16:40" <= view["sunset"] <= "16:52", view)
        self.assertIsNone(sun_view(now, None, zone))
        self.assertIsNone(sun_view(now, {"name": "no coordinates"}, zone))


if __name__ == "__main__":
    unittest.main()
