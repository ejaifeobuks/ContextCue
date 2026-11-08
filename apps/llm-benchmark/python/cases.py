"""Fictional ContextCue snapshots for the G1 benchmark (no personal data).

Each case is an immutable evidence snapshot plus the requested category, in the
shape RecommendationService will send. Values are synthetic demo fixtures.
"""

CATEGORIES = ("morning", "preparation", "comfort", "context_answer")


def _event(eid, title, start, minutes):
    return {"id": eid, "type": "calendar_event", "title": title,
            "start_local": start, "minutes_until_start": minutes, "source": "fixture"}


def _forecast(eid, condition, rain_pct, high_c):
    return {"id": eid, "type": "forecast", "condition": condition,
            "precipitation_probability_pct": rain_pct, "high_degc": high_c, "source": "fixture"}


def _indoor(eid, metric, value, unit, condition):
    return {"id": eid, "type": "indoor_sensor", "metric": metric, "value": value,
            "unit": unit, "condition": condition, "source": "real"}


def _case(name, category, evidence, facts=None, question=None, answerable=True):
    request = {"category": category}
    if question:
        request["question"] = question
    return {
        "name": name,
        "answerable": answerable,
        "snapshot": {
            "snapshot_id": f"snap-{name}",
            "local_time": "08:15",
            "request": request,
            "evidence": evidence,
            "calculated_facts": facts or {},
        },
    }


def build_cases():
    cases = []
    # Morning briefings (8)
    days = [
        ("Senior Design meeting", "10:00", 105, "rain", 70, 14, 3),
        ("Circuits lab", "09:30", 75, "cloudy", 20, 18, 2),
        ("Thesis review", "11:00", 165, "sunny", 0, 24, 1),
        ("Team stand-up", "08:45", 30, "rain", 90, 12, 4),
        ("Office hours", "13:00", 285, "foggy", 10, 16, 2),
        ("Career fair", "10:30", 135, "snowy", 60, -2, 1),
        ("Library study block", "09:00", 45, "cloudy", 30, 20, 5),
        ("Dentist appointment", "12:15", 240, "sunny", 5, 27, 2),
    ]
    for i, (title, start, mins, cond, rain, high, count) in enumerate(days):
        ev = [_event(f"ev-cal-{i}", title, start, mins), _forecast(f"ev-wx-{i}", cond, rain, high)]
        cases.append(_case(f"morning-{i}", "morning", ev,
                           {"events_today": count, "next_event_id": f"ev-cal-{i}"}))
    # Preparation (8): next event within 30 minutes
    prep = [
        ("Senior Design meeting", "14:00", 24, "rain", 70),
        ("Physics lecture", "09:00", 15, "rain", 55),
        ("Club meeting", "18:30", 28, "cloudy", 40),
        ("Lab shift", "16:00", 10, "rain", 85),
        ("Advisor meeting", "11:30", 20, "sunny", 0),
        ("Group project", "15:15", 25, "snowy", 65),
        ("Exam", "08:45", 30, "rain", 50),
        ("Interview call", "13:00", 5, "cloudy", 15),
    ]
    for i, (title, start, mins, cond, rain) in enumerate(prep):
        ev = [_event(f"ev-cal-p{i}", title, start, mins), _forecast(f"ev-wx-p{i}", cond, rain, 15)]
        cases.append(_case(f"prep-{i}", "preparation", ev,
                           {"rain_threshold_pct": 50, "rain_expected": rain >= 50}))
    # Comfort (7): derived sustained conditions
    comfort = [
        ("temperature", 27.4, "degC", "sustained_above_preferred_range"),
        ("temperature", 19.1, "degC", "sustained_below_preferred_range"),
        ("humidity", 64.0, "%RH", "sustained_above_preferred_range"),
        ("humidity", 27.5, "%RH", "sustained_below_preferred_range"),
        ("temperature", 26.8, "degC", "sustained_above_preferred_range"),
        ("humidity", 61.2, "%RH", "sustained_above_preferred_range"),
        ("temperature", 18.6, "degC", "sustained_below_preferred_range"),
    ]
    for i, (metric, value, unit, cond) in enumerate(comfort):
        ev = [_indoor(f"ev-in-{i}", metric, value, unit, cond)]
        cases.append(_case(f"comfort-{i}", "comfort", ev,
                           {"preferred_range": "20-26 degC" if metric == "temperature" else "30-60 %RH",
                            "persisted_seconds": 60}))
    # Context questions (5 answerable, 2 not answerable from evidence)
    base = [_event("ev-cal-q", "Senior Design meeting", "14:00", 105),
            _forecast("ev-wx-q", "rain", 70, 14),
            _indoor("ev-in-q", "temperature", 24.2, "degC", "within_preferred_range")]
    questions = [
        ("When is my next meeting?", True),
        ("Will it rain today?", True),
        ("What is the room temperature?", True),
        ("Do I need an umbrella for my meeting?", True),
        ("How many minutes until my next event?", True),
        ("What did my manager say in yesterday's email?", False),
        ("What is the air quality outside?", False),
    ]
    for i, (q, ok) in enumerate(questions):
        cases.append(_case(f"question-{i}", "context_answer", base, question=q, answerable=ok))
    return cases
