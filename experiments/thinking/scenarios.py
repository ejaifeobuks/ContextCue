"""Synthetic ContextCue scenarios with known ground truth for the thinking
experiment. Each scenario builds the same `ContextSnapshot` the app would send
and states what a correct reply must, and must not, contain.

Checks (case-insensitive regular expressions):
  must      every group must match (a group is a list of alternatives)
  forbid    any match is an invented or wrong claim (hallucination)
  missing   the reply must admit the information is not available
The app's own `validate_response` is applied on top (schema, length,
characters, every number traceable to the evidence).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from contextcue_app.contracts import ContextEvent, ContextSnapshot

MISSING = r"(not available|unavailable|don'?t have|do not have|can'?t|cannot|no (calendar|weather|forecast|temperature|data|information)|unknown|isn'?t available|not sure)"


def ev(eid, metric, value, unit=None, quality="valid", source_type="sensor"):
    return ContextEvent(eid, 1, "src", source_type, metric, value, unit, None, None, None, None,
                        "simulated", quality, "experiment", eid)


def temp(v, q="valid"):
    return ev("sensor.temperature", "temperature", v, "degC", q)


def hum(v, q="valid"):
    return ev("sensor.humidity", "humidity", v, "%RH", q)


def pref_t(lo=20.0, hi=26.0):
    return ev("pref.temperature_range", "preferred_temperature_range", [lo, hi], "degC", source_type="preference")


def pref_h(lo=30.0, hi=60.0):
    return ev("pref.humidity_range", "preferred_humidity_range", [lo, hi], "%RH", source_type="preference")


def comfort(name, text):
    return ev(f"derived.comfort.{name}", f"comfort_{name}", text, source_type="derived")


def event(eid, title, start_local, location=None, minutes=60):
    return {"event_id": eid, "title": title, "start_local": start_local,
            "start": f"2026-11-16T{start_local}:00Z", "location": location}


def schedule(events_ahead, current=(), total=None, busy_ms=3_600_000, all_day=None, q="valid"):
    value = None if q != "valid" else {
        "local_date": "2026-11-16", "event_count": total if total is not None else len(events_ahead) + len(current),
        "busy_ms": busy_ms, "next_event": events_ahead[0] if events_ahead else None,
        "current_events": list(current), "remaining_events": list(events_ahead), "all_day": all_day}
    return ev("calendar.schedule", "schedule_today", value, quality=q, source_type="calendar")


def cal_event(e):
    return ev(f"calendar.event.{e['event_id']}", "event", e, source_type="calendar")


def fc_now(cond, pct, t, night=False, q="valid"):
    v = None if q != "valid" else {"start_local": "08:00", "end_local": "11:00", "condition": cond,
                                    "precipitation_probability": pct, "temperature_c": t, "night": night}
    return ev("weather.now", "forecast_next", v, quality=q, source_type="weather")


def fc_later(start, end, cond, pct, t, q="valid"):
    v = None if q != "valid" else {"start_local": start, "end_local": end, "condition": cond,
                                    "precipitation_probability": pct, "temperature_c": t}
    return ev("weather.later", "forecast_later", v, quality=q, source_type="weather")


def fc_event(cond, pct, t):
    return ev("weather.event", "forecast_event", {"condition": cond, "precipitation_probability": pct,
                                                  "temperature_c": t}, source_type="weather")


@dataclass
class Scenario:
    sid: str
    group: str            # which of the 9 required scenario types it covers
    category: str
    evidence: tuple
    facts: dict
    question: str | None = None
    must: list = field(default_factory=list)
    forbid: list = field(default_factory=list)
    missing: bool = False
    truth: str = ""       # what a correct, helpful reply says

    def snapshot(self, n: int = 0) -> ContextSnapshot:
        return ContextSnapshot(f"req-{self.sid}-{n}", f"snap-{self.sid}-{n}", 1, self.category, "experiment",
                               tuple(self.evidence), dict(self.facts), {}, "normal", "visual_only",
                               "2026-11-16T08:00:00Z", "2026-11-16T08:01:00Z", self.question)


standup = event("e1", "Team stand-up", "09:00")
review = event("e2", "Design review", "10:00", "Room 4")
lunch = event("e3", "Lunch with Sam", "12:30")
review2 = event("e4", "Budget review", "14:00")
gym = event("e5", "Gym", "18:00")
dentist = event("e6", "Dentist", "15:30")
call = event("e7", "Client call", "11:05")

SCENARIOS = [
    # 1. Simple temperature / humidity notifications
    Scenario("comfort_hot", "1 sensor notice", "comfort",
             (temp(27.4), pref_t(), comfort("hot", "sustained_above_preferred_temperature")),
             {"condition": "hot", "advice": ["Open a window or lower the heating"]},
             must=[[r"27\.4"], [r"\b26\b|20.{0,8}26"]], forbid=[r"\bcold\b|\btoo cool\b|below (your|the) (preferred )?range"],
             truth="Room is 27.4 °C, above the preferred 20-26 °C; suggest cooling."),
    Scenario("comfort_humid", "1 sensor notice", "comfort",
             (hum(68.0), pref_h(), comfort("humid", "sustained_above_preferred_humidity")),
             {"condition": "humid", "advice": ["Air the room out"]},
             must=[[r"\b68\b"], [r"\b60\b|30.{0,8}60"]], forbid=[r"\bdry\b"],
             truth="Humidity 68%, above the preferred 30-60%; suggest airing out."),
    # 2. Upcoming calendar events
    Scenario("prep_imminent", "2 upcoming event", "preparation",
             (cal_event(review), schedule([review, lunch])),
             {"event": review, "minutes_until_event": 8},
             must=[[r"design review"], [r"10(:00)?\s*(AM|a\.m\.)|10:00"], [r"\b8\b|eight"]],
             forbid=[r"lunch", r"\b12:30\b"],
             truth="Design review at 10:00 AM starts in 8 minutes (Room 4 optional)."),
    # 3. Calendar + weather
    Scenario("prep_rain", "3 event + weather", "preparation",
             (cal_event(dentist), schedule([dentist]), fc_event("rainy", 80, 12)),
             {"event": dentist, "minutes_until_event": 25, "rain_probability": 80, "advice": ["Bring an umbrella"]},
             must=[[r"dentist"], [r"3:30"], [r"umbrella|rain"]], forbid=[r"\bno rain\b|\bdry\b"],
             truth="Dentist at 3:30 PM in 25 min; 80% rain, bring an umbrella."),
    # 4. Morning and evening briefings
    Scenario("briefing_morning", "4 briefing", "morning",
             (schedule([standup, review, lunch]), fc_now("cloudy", 10, 9), fc_later("15:00", "18:00", "rainy", 70, 11),
              temp(22.0), hum(45.0)),
             {"advice": ["Bring an umbrella"]},
             must=[[r"stand-?up"], [r"\b9(:00)?\s*(AM|a\.m\.)|9:00"], [r"rain|umbrella"]],
             forbid=[r"\bno rain\b", r"\b10% chance of rain later"],
             truth="Stand-up at 9:00 AM; cloudy now, 70% rain from 3 PM, bring an umbrella."),
    Scenario("briefing_evening", "4 briefing", "morning",
             (schedule([], total=3), fc_now("sunny", 0, 6, night=True), temp(21.0), hum(40.0)),
             {"advice": ["Wear a warm coat"]},
             must=[[r"nothing (else|more)|no (more|other|further) (events|meetings)|calendar is (clear|free)|free"]],
             forbid=[r"stand-?up|meeting at|\bsunny\b|\b\d{1,2}:\d{2}\b"],
             truth="Nothing else on the calendar today; clear night, 0% rain, wear a warm coat."),
    # 5. Several upcoming events, different urgency
    Scenario("q_next_of_many", "5 multiple events", "context_answer",
             (schedule([lunch, review2, gym], current=[standup]),), {}, "next_event",
             must=[[r"lunch"], [r"12:30"]], forbid=[r"\bgym\b.{0,20}next|next.{0,20}\bgym\b"],
             truth="Next is Lunch with Sam at 12:30 PM (stand-up is happening now)."),
    Scenario("prep_two_close", "5 multiple events", "preparation",
             (cal_event(call), schedule([call, lunch])),
             {"event": call, "minutes_until_event": 4},
             must=[[r"client call"], [r"11:05"], [r"\b4\b|four"]], forbid=[r"lunch"],
             truth="Client call at 11:05 AM, in 4 minutes."),
    Scenario("q_busy", "5 multiple events", "context_answer",
             (schedule([review2, gym], current=[], total=4, busy_ms=4 * 3_600_000),), {}, "schedule_today",
             must=[[r"\b4\b|four|\b2\b|two"]], forbid=[r"\b(3|5|6) events"],
             truth="4 events today, 2 still to come (Budget review 2:00 PM next)."),
    # 6. Questions needing several sources
    Scenario("q_umbrella_next_event", "6 multi-source", "context_answer",
             (schedule([review2]), fc_now("cloudy", 20, 10), fc_later("13:00", "16:00", "rainy", 70, 9)),
             {"advice": ["Bring an umbrella"]}, "Do I need an umbrella for my next event?",
             must=[[r"budget review|2:00|next event"], [r"yes|umbrella|70"]], forbid=[r"\bno\b,? you (don'?t|do not) need"],
             truth="Yes: Budget review at 2:00 PM falls in the 1-4 PM window with 70% rain."),
    Scenario("q_room_before_meeting", "6 multi-source", "context_answer",
             (schedule([review]), temp(27.4), hum(45.0), pref_t()), {},
             "Is the room comfortable for my next meeting?",
             must=[[r"27\.4|warm|hot|above"], [r"design review|10:00|meeting"]], forbid=[r"\bcold\b"],
             truth="Warm: 27.4 °C, above the preferred 20-26 °C, before the 10:00 AM Design review."),
    # 7. Missing / stale / unavailable data
    Scenario("missing_calendar", "7 missing data", "context_answer",
             (schedule([], q="unavailable"),), {}, "next_event", missing=True,
             forbid=[r"\b\d{1,2}:\d{2}\b", r"stand-?up|review|lunch|meeting at"],
             truth="Say the calendar is not available; invent no event."),
    Scenario("missing_temperature", "7 missing data", "context_answer",
             (temp(None, "unavailable"), hum(45.0)), {}, "room_comfort", missing=True,
             must=[[r"\b45\b"]], forbid=[r"°C|degrees|\bdeg"],
             truth="Humidity 45%; temperature reading is unavailable."),
    Scenario("stale_weather", "7 missing data", "context_answer",
             (fc_now("cloudy", 20, 10, q="stale"), fc_later("15:00", "18:00", "rainy", 70, 9, q="stale")), {},
             "rain_today", missing=True, forbid=[r"\b(20|70)\b", r"\bwill rain\b|\bno rain\b"],
             truth="Say the forecast is not available right now; do not guess."),
    # 8. Contradictory / ambiguous / incomplete
    Scenario("contradictory_forecast", "8 contradictory", "context_answer",
             (fc_now("sunny", 60, 18), fc_later("15:00", "18:00", "rainy", 10, 16)), {"advice": ["Bring an umbrella"]},
             "rain_today", must=[[r"\b60\b"]], forbid=[r"\bno (chance of )?rain\b"],
             truth="Quote the 60% chance now (sunny but 60%); 10% later; umbrella advice."),
    Scenario("all_day_event", "8 ambiguous", "context_answer",
             (schedule([], total=1, all_day=["Conference"]),), {}, "next_event",
             must=[[r"conference|all[- ]day|nothing"]], forbid=[r"\b\d{1,2}:\d{2}\b"],
             truth="Only an all-day Conference; no timed events, no invented time."),
    # 9. Insufficient information: must acknowledge rather than invent
    Scenario("insufficient_location", "9 insufficient", "context_answer",
             (schedule([lunch]),), {}, "Where is my lunch meeting?", missing=True,
             must=[[r"lunch"]], forbid=[r"\b(room|office|cafe|café|restaurant|downtown|street)\b"],
             truth="Lunch with Sam at 12:30 PM; no location is listed."),
]
