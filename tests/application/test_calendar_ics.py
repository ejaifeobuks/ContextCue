"""Live calendar from a subscription link (ICS). No network: the downloader is
a stub returning recorded-style calendar files."""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402

from contextcue_app import ActionError, ContextCueApp  # noqa: E402
from contextcue_app.calendar_ics import (CalendarLinkError, IcsCalendarAdapter,  # noqa: E402
                                         normalize_link, parse_events, provider_for)
from contextcue_app.clock import parse_iso  # noqa: E402
from contextcue_app.dev_gateway import SimulatedGateway  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.sources import load_scenarios  # noqa: E402

CHICAGO = ZoneInfo("America/Chicago")
LINK = "https://calendar.google.com/calendar/ical/someone%40example.com/private-abc123/basic.ics"

# Monday 9 November 2026. A weekly stand-up (Mon/Tue/Wed) with Tuesday moved
# and Wednesday removed, a meeting in New York time, an all-day event, a
# floating-time event, a cancelled event and an event last month.
ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//ContextCue//EN
BEGIN:VTIMEZONE
TZID:America/Chicago
BEGIN:STANDARD
DTSTART:20071104T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
TZOFFSETFROM:-0500
TZOFFSETTO:-0600
TZNAME:CST
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:20070311T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
TZOFFSETFROM:-0600
TZOFFSETTO:-0500
TZNAME:CDT
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:standup@test
DTSTART;TZID=America/Chicago:20261102T090000
DTEND;TZID=America/Chicago:20261102T091500
RRULE:FREQ=DAILY;COUNT=10
EXDATE;TZID=America/Chicago:20261111T090000
SUMMARY:Team stand-up
LOCATION:Room 204
END:VEVENT
BEGIN:VEVENT
UID:standup@test
RECURRENCE-ID;TZID=America/Chicago:20261110T090000
DTSTART;TZID=America/Chicago:20261110T093000
DTEND;TZID=America/Chicago:20261110T094500
SUMMARY:Team stand-up (moved)
END:VEVENT
BEGIN:VEVENT
UID:nyc@test
DTSTART;TZID=America/New_York:20261109T140000
DTEND;TZID=America/New_York:20261109T150000
SUMMARY:Call with New York
END:VEVENT
BEGIN:VEVENT
UID:allday@test
DTSTART;VALUE=DATE:20261109
DTEND;VALUE=DATE:20261110
SUMMARY:Demo week
END:VEVENT
BEGIN:VEVENT
UID:floating@test
DTSTART:20261109T170000
DURATION:PT1H
SUMMARY:Study group
END:VEVENT
BEGIN:VEVENT
UID:cancelled@test
DTSTART:20261109T200000Z
DTEND:20261109T210000Z
STATUS:CANCELLED
SUMMARY:Office hours
END:VEVENT
BEGIN:VEVENT
UID:old@test
DTSTART:20261001T150000Z
DTEND:20261001T160000Z
SUMMARY:Last month
END:VEVENT
END:VCALENDAR
"""

NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)  # 07:00 CST, Monday 9 November


def local(event):
    return parse_iso(event.start).astimezone(CHICAGO).strftime("%m-%d %H:%M")


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.events = parse_events(ICS, NOW, CHICAGO)
        self.by_title = {e.title: e for e in self.events}

    def test_window_is_today_and_tomorrow(self):
        self.assertNotIn("Last month", self.by_title)
        standups = [e for e in self.events if e.title.startswith("Team stand-up")]
        self.assertEqual([local(e) for e in standups], ["11-09 09:00", "11-10 09:30"])

    def test_recurrence_exceptions_and_moved_occurrences(self):
        self.assertEqual(local(self.by_title["Team stand-up (moved)"]), "11-10 09:30")
        self.assertEqual(self.by_title["Team stand-up"].location, "Room 204")

    def test_time_zones_all_day_floating_and_cancelled(self):
        self.assertEqual(local(self.by_title["Call with New York"]), "11-09 13:00")  # 14:00 EST
        demo = self.by_title["Demo week"]
        self.assertEqual((demo.all_day, demo.start, demo.end), (True, "2026-11-09", "2026-11-10"))
        study = self.by_title["Study group"]  # floating: the user's time zone
        self.assertEqual(local(study), "11-09 17:00")
        self.assertEqual(parse_iso(study.end) - parse_iso(study.start), timedelta(hours=1))
        self.assertEqual(self.by_title["Office hours"].status, "cancelled")

    def test_event_ids_are_stable_and_distinct(self):
        again = parse_events(ICS, NOW, CHICAGO)
        self.assertEqual([e.event_id for e in self.events], [e.event_id for e in again])
        self.assertEqual(len({e.event_id for e in self.events}), len(self.events))

    def test_not_a_calendar(self):
        with self.assertRaises(CalendarLinkError):
            parse_events(b"<html>Sign in</html>", NOW, CHICAGO)


class LinkTests(unittest.TestCase):
    def test_links_are_normalised_and_checked(self):
        self.assertEqual(normalize_link(" webcal://p01-caldav.icloud.com/x.ics "),
                         "https://p01-caldav.icloud.com/x.ics")
        for bad in ("http://example.com/a.ics", "calendar", "", None, "https://"):
            with self.assertRaises(CalendarLinkError):
                normalize_link(bad)

    def test_provider_from_host(self):
        self.assertEqual(provider_for(LINK), "google")
        self.assertEqual(provider_for("https://outlook.office365.com/owa/calendar/a/b/calendar.ics"),
                         "microsoft365")
        self.assertEqual(provider_for("https://p01-caldav.icloud.com/x.ics"), "ics")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.reply = ICS
        self.calls = 0
        self.mono = 0.0

    def download(self, url, timeout):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def adapter(self, link=LINK):
        return IcsCalendarAdapter("google", link, CHICAGO, downloader=self.download,
                                  monotonic=lambda: self.mono, background=False)

    def test_not_connected_is_disabled(self):
        result = self.adapter(None).fetch(NOW)
        self.assertEqual((result.quality, result.error, result.items), ("disabled", "calendar_not_connected", ()))
        self.assertEqual(self.calls, 0)

    def test_valid_cached_then_stale_and_errors_never_contain_the_link(self):
        adapter = self.adapter()
        result = adapter.fetch(NOW)
        self.assertEqual((result.quality, result.source_mode, result.label),
                         ("valid", "real", "Google Calendar"))
        self.assertTrue(result.items)
        adapter.fetch(NOW + timedelta(minutes=1))
        self.assertEqual(self.calls, 1)  # cached between refreshes
        self.reply = CalendarLinkError("http_404")
        self.mono += 11 * 60
        later = adapter.fetch(NOW + timedelta(minutes=11))
        self.assertEqual((later.quality, later.error), ("valid", "http_404"))
        self.mono += 3 * 3600
        stale = adapter.fetch(NOW + timedelta(hours=3))
        self.assertEqual(stale.quality, "stale")
        self.assertNotIn("private-abc123", json.dumps(stale.__dict__, default=str))

    def test_new_day_re_expands_without_downloading(self):
        adapter = self.adapter()
        adapter.fetch(NOW)
        tomorrow = adapter.fetch(NOW + timedelta(days=1))
        self.assertEqual(self.calls, 1)
        titles = {e.title for e in tomorrow.items}
        self.assertIn("Team stand-up (moved)", titles)
        self.assertNotIn("Call with New York", titles)  # yesterday's event dropped


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scenarios = load_scenarios()
        clock = support.frozen_clock(NOW)
        gateway = SimulatedGateway(monotonic=clock.monotonic, utcnow=clock.now)
        self.repo = DataRepository()
        self.addCleanup(self.repo.close)
        self.app = ContextCueApp(gateway=gateway, clock=clock, repository=self.repo,
                                 gateway_mode="simulated", scenario_id="morning", scenarios=scenarios)
        import contextcue_app.calendar_ics as ics
        self._orig = ics._download
        ics._download = lambda url, timeout: ICS
        self.addCleanup(setattr, ics, "_download", self._orig)

    async def test_connect_stores_a_secret_and_never_shows_it(self):
        with self.assertRaises(ActionError):
            await self.app.action("calendar_connect", {"link": "http://insecure.example/a.ics"})
        result = await self.app.action("calendar_connect", {"link": LINK})
        self.assertEqual((result["status"], result["provider"]), ("connected", "google"))
        self.assertNotIn(LINK, json.dumps(result))
        self.assertEqual(self.app.settings.calendar_source, "google")
        self.assertEqual(self.repo.load_secret("calendar_link"), LINK)
        self.app._calendar._background = False
        await self.app._tick()
        view = self.app.view_model()
        dump = json.dumps(view, default=str)
        self.assertNotIn("private-abc123", dump)
        self.assertEqual(view["calendar_link"], {"connected": True, "host": "calendar.google.com"})
        self.assertEqual(view["schedule"]["source_mode"], "real")
        self.assertEqual(view["schedule"]["next_event"]["title"], "Team stand-up")
        self.assertNotIn("private-abc123", json.dumps(self.repo.diagnostics(), default=str))
        self.assertNotIn("private-abc123", json.dumps(self.app.settings.to_dict()))
        # Back to the demo and forward again keeps the link.
        await self.app.action("settings", {"changes": {"calendar_source": "fixture"}})
        self.assertEqual(self.app.view_model()["calendar_link"]["connected"], True)
        await self.app.action("calendar_disconnect", {})
        self.assertIsNone(self.repo.load_secret("calendar_link"))
        self.assertEqual(self.app.settings.calendar_source, "fixture")
        await self.app.stop()

    async def test_live_without_a_link_is_disabled_not_demo(self):
        await self.app.action("settings", {"changes": {"calendar_source": "microsoft365"}})
        await self.app._tick()
        schedule = self.app.view_model()["schedule"]
        self.assertEqual((schedule["quality"], schedule["source_mode"]), ("disabled", "real"))
        await self.app.stop()


if __name__ == "__main__":
    unittest.main()
