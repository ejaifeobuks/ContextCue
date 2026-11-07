"""Code-calculated schedule facts (APP-04; baseline section 6 "Schedule totals").

The model never computes these. Valid non-cancelled timed events that
intersect the local day are merged before summing, so overlaps are not double
counted. All-day events are listed separately, never as 24 hours of work.
Local day boundaries come from the IANA zone, so DST days are 23 or 25 hours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .clock import iso, parse_iso
from .contracts import CalendarEvent

IMMINENT_MS = 10 * 60_000
SOON_MS = 30 * 60_000


@dataclass(frozen=True)
class TimedEvent:
    event_id: str
    title: str
    start: datetime
    end: datetime
    location: str | None


@dataclass
class ScheduleFacts:
    local_date: str
    event_count: int
    busy_ms: int
    next_event: TimedEvent | None
    next_event_in_ms: int | None
    urgency: str | None  # imminent | soon | later
    current_events: list[TimedEvent] = field(default_factory=list)
    remaining_events: list[TimedEvent] = field(default_factory=list)
    all_day: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)

    def to_dict(self, zone: ZoneInfo) -> dict:
        def event(e: TimedEvent | None):
            if e is None:
                return None
            return {
                "event_id": e.event_id, "title": e.title,
                "start": iso(e.start), "end": iso(e.end),
                "start_local": e.start.astimezone(zone).strftime("%H:%M"),
                "end_local": e.end.astimezone(zone).strftime("%H:%M"),
                "location": e.location,
            }
        return {
            "local_date": self.local_date,
            "event_count": self.event_count,
            "busy_ms": self.busy_ms,
            "next_event": event(self.next_event),
            "next_event_in_ms": self.next_event_in_ms,
            "urgency": self.urgency,
            "current_events": [event(e) for e in self.current_events],
            "remaining_events": [event(e) for e in self.remaining_events],
            "all_day": list(self.all_day),
            "excluded": dict(self.excluded),
        }


def local_day_bounds(now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime, date]:
    local_date = now.astimezone(zone).date()
    start = datetime.combine(local_date, time(0), tzinfo=zone)
    end = datetime.combine(local_date + timedelta(days=1), time(0), tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), local_date


def merged_duration_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    total = 0
    current_start = current_end = None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += int((current_end - current_start).total_seconds() * 1000)
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += int((current_end - current_start).total_seconds() * 1000)
    return total


def calculate_schedule(events: tuple[CalendarEvent, ...] | list[CalendarEvent],
                       now: datetime, zone: ZoneInfo) -> ScheduleFacts:
    day_start, day_end, local_date = local_day_bounds(now, zone)
    timed: list[TimedEvent] = []
    all_day: list[str] = []
    excluded: dict[str, str] = {}
    seen: set[str] = set()
    for event in events:
        if event.event_id in seen:
            excluded[event.event_id] = "duplicate_id"
            continue
        seen.add(event.event_id)
        if event.status == "cancelled":
            excluded[event.event_id] = "cancelled"
            continue
        if event.all_day:
            try:
                first, last = date.fromisoformat(event.start), date.fromisoformat(event.end)
            except ValueError:
                excluded[event.event_id] = "invalid_time"
                continue
            if last <= first:
                excluded[event.event_id] = "invalid_interval"
            elif first <= local_date < last:
                all_day.append(event.title)
            continue
        try:
            start, end = parse_iso(event.start), parse_iso(event.end)
        except ValueError:
            excluded[event.event_id] = "invalid_time"
            continue
        if end <= start:
            excluded[event.event_id] = "invalid_interval"
            continue
        if end <= day_start or start >= day_end:
            continue  # valid, just not today
        timed.append(TimedEvent(event.event_id, event.title, start, end, event.location))

    timed.sort(key=lambda e: (e.start, e.end, e.event_id))
    busy = merged_duration_ms([(max(e.start, day_start), min(e.end, day_end)) for e in timed])
    current = [e for e in timed if e.start <= now < e.end]
    upcoming = [e for e in timed if e.start > now]
    next_event = upcoming[0] if upcoming else None
    next_in = int((next_event.start - now).total_seconds() * 1000) if next_event else None
    if next_in is None:
        urgency = None
    elif next_in <= IMMINENT_MS:
        urgency = "imminent"
    elif next_in <= SOON_MS:
        urgency = "soon"
    else:
        urgency = "later"
    return ScheduleFacts(
        local_date=local_date.isoformat(),
        event_count=len(timed),
        busy_ms=busy,
        next_event=next_event,
        next_event_in_ms=next_in,
        urgency=urgency,
        current_events=current,
        remaining_events=upcoming,
        all_day=all_day,
        excluded=excluded,
    )
