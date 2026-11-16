"""Live calendar from a subscription link (ICS), behind the shared calendar
adapter contract (plan 9.2, LIVE_INTEGRATIONS_PLAN M5).

* Google Calendar: Settings > your calendar > "Secret address in iCal format".
* Outlook / Microsoft 365: Calendar settings > Shared calendars > "Publish a
  calendar" > ICS link.
* Any other calendar that publishes an https ICS link (iCloud, Proton, ...).

The link is a bearer secret: whoever has it can read the calendar. It is
stored outside the settings, never returned to the UI and never logged; error
reasons name only the failure type or HTTP status.

Parsing uses `icalendar` and `recurring-ical-events` (recurrence rules,
exceptions, moved occurrences, time zones). Network requests never run on the
event loop: `fetch()` returns the last result and refreshes in a background
thread. A failed or missing calendar is reported as stale or unavailable and is
never replaced by fixture data.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .clock import iso
from .contracts import CalendarEvent, SourceResult
from .schedule import clean_location

REFRESH_S = 10 * 60   # Google's secret link itself lags edits by minutes to hours
RETRY_S = 2 * 60
VALID_FOR = timedelta(hours=2)  # a calendar not refreshed for 2 h is stale
TIMEOUT_S = 15.0
MAX_BYTES = 5 * 1024 * 1024
WINDOW_DAYS = 2  # today and tomorrow, local

PROVIDERS = {"google": "Google Calendar", "microsoft365": "Microsoft 365 / Outlook calendar",
             "ics": "Calendar subscription link"}

Downloader = Callable[[str, float], bytes]  # (url, timeout) -> ICS bytes


class CalendarLinkError(ValueError):
    """A link or download problem, with a reason safe to show and log."""


def normalize_link(link: Any) -> str:
    """The https URL for a pasted subscription link (webcal:// is accepted)."""
    if not isinstance(link, str):
        raise CalendarLinkError("Paste the calendar's subscription link")
    link = link.strip()
    if link.lower().startswith("webcal://"):
        link = "https://" + link[len("webcal://"):]
    parts = urllib.parse.urlsplit(link)
    if parts.scheme != "https" or not parts.netloc:
        raise CalendarLinkError("The link must start with https:// or webcal://")
    if len(link) > 2048:
        raise CalendarLinkError("The link is too long")
    return link


def provider_for(link: str) -> str:
    host = (urllib.parse.urlsplit(link).hostname or "").lower()
    if host.endswith("calendar.google.com"):
        return "google"
    if host.startswith("outlook.") or host.endswith(".office365.com") or host.endswith("outlook.com"):
        return "microsoft365"
    return "ics"


def link_host(link: str | None) -> str | None:
    return urllib.parse.urlsplit(link).hostname if link else None


def _download(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ContextCue/1.0",
                                                   "Accept": "text/calendar"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:  # its text can echo the URL: keep only the code
        raise CalendarLinkError(f"http_{exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise CalendarLinkError(f"network_error: {type(exc).__name__}") from None
    if len(data) > MAX_BYTES:
        raise CalendarLinkError("calendar_too_large")
    return data


def _utc(value: datetime, zone: ZoneInfo) -> datetime:
    """Floating (zone-less) times are in the user's time zone."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(timezone.utc)


def parse_events(data: bytes, now: datetime, zone: ZoneInfo) -> tuple[CalendarEvent, ...]:
    """Occurrences from local midnight today through WINDOW_DAYS, sorted."""
    import icalendar
    import recurring_ical_events

    try:
        calendar = icalendar.Calendar.from_ical(data)
    except Exception:
        raise CalendarLinkError("not_a_calendar") from None
    if calendar.name != "VCALENDAR":
        raise CalendarLinkError("not_a_calendar")
    today = now.astimezone(zone).date()
    start = datetime.combine(today, datetime.min.time(), zone)
    end = start + timedelta(days=WINDOW_DAYS)
    events = []
    for component in recurring_ical_events.of(calendar).between(start, end):
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        begin = dtstart.dt
        if component.get("DTEND") is not None:
            finish = component["DTEND"].dt
        elif component.get("DURATION") is not None:
            finish = begin + component["DURATION"].dt
        else:  # RFC 5545: a date lasts one day, a date-time is instantaneous
            finish = begin + timedelta(days=1) if not isinstance(begin, datetime) else begin
        all_day = not isinstance(begin, datetime)
        if all_day:
            start_s, end_s = begin.isoformat(), (finish if isinstance(finish, date) else begin).isoformat()
        else:
            if not isinstance(finish, datetime):
                finish = datetime.combine(finish, datetime.min.time())
            start_s, end_s = iso(_utc(begin, zone)), iso(_utc(finish, zone))
        status = str(component.get("STATUS", "CONFIRMED")).lower()
        uid = str(component.get("UID", "")) or f"{component.get('SUMMARY', '')}"
        title = str(component.get("SUMMARY", "")).strip() or "Busy"
        location = clean_location(str(component.get("LOCATION", "")))
        events.append(CalendarEvent(
            # Stable across restarts, so evidence and notice keys do not churn.
            event_id="ics-" + hashlib.sha1(f"{uid}|{start_s}".encode()).hexdigest()[:12],
            title=title[:120], start=start_s, end=end_s, all_day=all_day,
            status=status if status in ("confirmed", "tentative", "cancelled") else "confirmed",
            location=location))
    events.sort(key=lambda e: (not e.all_day, e.start))
    return tuple(events)


class IcsCalendarAdapter:
    """Live calendar from one subscription link (or `None`: not connected)."""

    kind = "calendar"
    source_mode = "real"

    def __init__(self, provider: str, link: str | None, zone: ZoneInfo, *,
                 downloader: Downloader | None = None,
                 monotonic: Callable[[], float] = time.monotonic, background: bool = True):
        self.provider = provider if provider in PROVIDERS else "ics"
        self.source_id = f"ics-{self.provider}"
        self.label = PROVIDERS[self.provider]
        self._link = link
        self._zone = zone
        self._download = downloader or (lambda url, timeout: _download(url, timeout))
        self._mono = monotonic
        self._background = background
        self._lock = threading.Lock()
        self._raw: bytes | None = None
        self._events: tuple[CalendarEvent, ...] = ()
        self._expanded_for: date | None = None
        self._observed: datetime | None = None
        self._error: str | None = None
        self._next_attempt = float("-inf")
        self._running = False

    @property
    def connected(self) -> bool:
        return bool(self._link)

    def refresh(self, now: datetime) -> None:
        """One blocking download and parse; runs in a worker thread."""
        try:
            raw = self._download(self._link, TIMEOUT_S)
            events = parse_events(raw, now, self._zone)
            with self._lock:
                self._raw, self._events, self._observed, self._error = raw, events, now, None
                self._expanded_for = now.astimezone(self._zone).date()
                self._next_attempt = self._mono() + REFRESH_S
        except Exception as exc:  # keep the last calendar; never log the link
            reason = str(exc) if isinstance(exc, CalendarLinkError) else f"parse_error: {type(exc).__name__}"
            with self._lock:
                self._error = reason
                self._next_attempt = self._mono() + RETRY_S
        finally:
            with self._lock:
                self._running = False

    def _maybe_refresh(self, now: datetime) -> None:
        with self._lock:
            if self._running or self._mono() < self._next_attempt:
                return
            self._running = True
        if self._background:
            threading.Thread(target=self.refresh, args=(now,), daemon=True, name="ics-calendar").start()
        else:
            self.refresh(now)

    def fetch(self, now: datetime) -> SourceResult:
        received = iso(now)
        if not self._link:
            return SourceResult(self.source_id, "calendar", "real", "disabled", "calendar_not_connected",
                                None, received, None, self.label, ())
        self._maybe_refresh(now)
        with self._lock:
            # After local midnight, re-expand the cached calendar for the new day.
            if self._raw is not None and self._expanded_for != now.astimezone(self._zone).date():
                try:
                    self._events = parse_events(self._raw, now, self._zone)
                    self._expanded_for = now.astimezone(self._zone).date()
                except Exception:
                    pass
            events, observed, error = self._events, self._observed, self._error
        if observed is None:
            return SourceResult(self.source_id, "calendar", "real", "unavailable", error or "fetching",
                                None, received, None, self.label, ())
        valid_until = observed + VALID_FOR
        quality = "valid" if now < valid_until else "stale"
        return SourceResult(self.source_id, "calendar", "real", quality,
                            error if quality == "valid" else (error or "source_expired"),
                            iso(observed), received, iso(valid_until), self.label, events)


def check_link(link: str, now: datetime, zone: ZoneInfo, downloader: Downloader | None = None) -> int:
    """Download and parse once (blocking); the number of events today and tomorrow."""
    return len(parse_events((downloader or _download)(link, TIMEOUT_S), now, zone))
