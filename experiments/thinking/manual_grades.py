"""Hand grades for every distinct reply in results.jsonl (16 November 2026 run).

G = correct and helpful, P = correct but incomplete or vague,
W = wrong, invented, contradictory or garbled.
Keys: (scenario, start of the reply text). Unmatched replies print as "?".

    python3 experiments/thinking/manual_grades.py results.jsonl
"""

import json
import sys
from collections import Counter, defaultdict

GRADES = {
    "comfort_hot": [("The room is currently hot at 27.4°C, which exceeds", "G", ""),
                    ("The room is currently hot at 27.4°C, so you", "P", "no preferred range"),
                    ("The room is currently hot at 27.4 degrees Celsius", "P", "no preferred range"),
                    ("The temperature is 27.4 degrees, which is 10%", "W", "invented 10%"),
                    ("You are currently 27.4 degrees", "P", "odd wording, no range"),
                    ("The room temperature is 27.4°C, which is above", "G", "")],
    "comfort_humid": [("The room is currently 68%", "G", ""),
                      ("The room feels cool and comfortable", "W", "says comfortable; invented 50%"),
                      ("Humid conditions may require", "P", "no reading or range")],
    "prep_imminent": [("Design review is scheduled for 10:00 AM in Room 4. Rain chance is 0%", "W", "invented 0% rain; no countdown"),
                      ("Design event starts", "W", "wrong name; invented 0% rain"),
                      ("Next event is Design review scheduled for 10:00 AM at Room 4. The minutes", "P", "8 min right; rambles about weather"),
                      ("The event is scheduled for 10:00 AM in Room 4. The minutes", "W", "talks about its own word limit"),
                      ("Design review is scheduled for 10:00 AM in Room 4, with a 50%", "W", "invented 50% rain")],
    "prep_rain": [("Next event is a dentist", "G", ""), ("Next event is Dentist at 3:30 PM with 80%", "G", ""),
                  ("Next event is the dentist", "P", "no rain chance"), ("The dentist event starts at 3:30 PM with 25", "G", ""),
                  (":[", "W", "garbage")],
    "briefing_morning": [("Next event is Team stand-up at 9:00 AM with cloudy weather", "P", "umbrella but rain not explained"),
                         ("Next event is Team stand-up at 9:00 AM, followed by a 70%", "G", ""),
                         ("Next event is Team stand at", "P", "truncated name"),
                         ("Next event is Team stand-up at 9:00 AM, weather is cloudy with 10% rain chance later", "W", "later is 70%, not 10%"),
                         ("Next event starts at 9:00 AM with Team stand-up. Current weather is cloudy at 10%", "G", ""),
                         ("Team stand-up starts at 9:00 AM, it is cloudy with 10% rain chance later", "W", "later is 70%"),
                         ("Next event is Team stand-up at 9:00 AM, followed by a 50%", "W", "invented 50%"),
                         ("Next event is Team stand-up at 9:00 AM, followed by a cloudy sky with 10% rain chance later", "W", "later is 70%")],
    "briefing_evening": [("Next event is scheduled for 10:00 AM", "W", "invented event"),
                         ("Next event is a meeting at 10:00 AM", "W", "invented event and 50%"),
                         ("Next event is tomorrow at 9:00 AM", "W", "invented event"),
                         ("Next event: Calendar event scheduled for 15:00", "W", "invented event"),
                         ("Next event is a morning meeting at 8:00 AM", "W", "invented meeting; passes validator")],
    "q_next_of_many": [("Next event is Lunch with Sam at 12:30 PM, followed by Gym", "G", "skips 2 PM review but correct next"),
                       ("Your next event is Lunch with Sam", "G", ""), ("Next event is Lunch with Sam at 12:30 PM, then Gym", "G", ""),
                       ("Next event is lunch with Sam at 12:30 PM. The gym", "G", ""),
                       ("Team stand-up is next event", "W", "wrong event, foreign text"),
                       ("Next event is Lunch with Sam at 12:30 PM,orea", "W", "garbled; passes validator")],
    "prep_two_close": [("Client call is scheduled for 11:05 AM. Rain chance is 0%", "W", "invented 0% rain"),
                       ("Client call scheduled for 11:05 AM with 4 minutes", "G", ""),
                       ("Client call at 11:00 AM", "W", "wrong time 11:00; passes validator"),
                       ("Client call at 11:05 AM is the event. 4 minutes", "P", "stilted"),
                       ("Client call on 11:05 AM. Preparation required", "W", "talks about word limit"),
                       ("The event is scheduled for 11:05 AM. Please prepare", "P", "no countdown"),
                       ("Client call is scheduled for 11:05 AM, which is 4 minutes before", "W", "contradictory nonsense"),
                       ("Client call at 11:05 AM is the next event. No more events today", "W", "Lunch is still ahead")],
    "q_busy": [("Today is busy with a budget review", "P", "right events, no count"),
               ("Today is 14:00 PM", "W", "invented current time"),
               ("Today has two events scheduled", "P", "4 today, 2 still to come")],
    "q_umbrella_next_event": [("You should bring an umbrella for the Budget review event at 2:00 PM, as it is cloudy", "P", "right answer, muddled reason"),
                              ("You need an umbrella for your next event, as the weather is cloudy with 20%", "W", "right yes, wrong reason (70% later)"),
                              ("You need an umbrella for your next event, as it is cloudy with 20% rain chance later today.", "W", "wrong reason"),
                              ("You need an umbrella for your next event, as it is cloudy with 20% rain chance later味", "W", "garbled"),
                              ("You can't get an umbrella", "W", "wrong"),
                              ("You need an umbrella because your next event is at 2:00 PM", "P", "right, says cloudy not rainy"),
                              ("You need an umbrella for your next event, as the facts indicate you will be in a rainy", "G", "right and right reason"),
                              ("You need an umbrella for your next event, as the facts indicate you will have one more", "W", "runaway repetition; passes validator")],
    "q_room_before_meeting": [("The room is currently comfortable at 27.4", "W", "27.4 is above range, not comfortable"),
                              ("The room is comfortable for your next meeting, as the temperature is 20-26", "W", "says comfortable"),
                              ("The room is comfortable for your meeting", "W", "says comfortable"),
                              ("The room has 27.4 degrees C", "W", "says comfortable"),
                              ("The room is comfortable for your next meeting at 10:00", "W", "says comfortable"),
                              ("The room is currently comfortable for your next meeting, as the temperature is above", "W", "self-contradictory")],
    "missing_calendar": [("Next event", "W", "invented meeting (app never sends this case to the model)"),
                         ("The next event is 9:00 AM", "W", "invented")],
    "missing_temperature": [("The room is currently 45% humidity.", "P", "does not say temperature is missing"),
                            ("The room has a humidity of 45.0% RH", "P", "does not say temperature is missing"),
                            ("The room is currently at 45% humidity, which is relatively low", "P", "opinion; temperature not mentioned"),
                            ("The room is currently 45% humidity, which is a moderate", "P", "does not say temperature is missing")],
    "stale_weather": [("", "W", "invented forecast (app never sends this case to the model)")],
    "contradictory_forecast": [("It will be sunny today, but it will rain later today", "W", "later is 10%; 60% is now"),
                               ("No, the forecast purpose", "W", "rambling"),
                               ("Yes, it will be sunny today. However", "W", "no numbers, confused"),
                               ("It will rain later today, but it is sunny now", "W", "later is 10%")],
    "all_day_event": [("Conference is the next event today", "G", ""),
                      ("Conference is my next event, starting at 09:00", "W", "invented time"),
                      ("The next event is Conference on 11:00", "W", "invented time"),
                      ("No more events today, so the next event is Conference, which starts at 10:00", "W", "invented time"),
                      ("Conference is the only event today", "G", "")],
    "insufficient_location": [("There is no lunch meeting scheduled", "W", "lunch is at 12:30"),
                              ("Your lunch meeting is scheduled for 12:30 PM. Check your calendar", "G", "admits no location"),
                              ("You have lunch meeting at 12:30 PM with Sam", "P", "location not addressed")],
}


def grade(sid, text):
    text = text or ""
    for prefix, g, why in GRADES.get(sid, []):
        if text.startswith(prefix):
            return g, why
    return "?", ""


if __name__ == "__main__":
    rows = [json.loads(line) for line in open(sys.argv[1])]
    stats = defaultdict(Counter)
    unmatched = []
    for r in rows:
        g, _ = grade(r["scenario"], r.get("text") or r.get("raw", ""))
        if g == "?":
            unmatched.append((r["scenario"], r["config"], r.get("text")))
        c = stats[r["config"]]
        c[g] += 1
        c["n"] += 1
        if r["valid"]:
            c["shown"] += 1
            c["shown_" + g] += 1
    print("| Config | n | Good | Partial | Wrong | Shown by app (valid) | Wrong but shown |")
    print("|---|---|---|---|---|---|---|")
    for cfg in ["A", "B", "Bs", "C"]:
        c = stats[cfg]
        print(f"| {cfg} | {c['n']} | {c['G']} | {c['P']} | {c['W']} | {c['shown']} | {c['shown_W']} |")
    for u in unmatched:
        print("UNMATCHED", u)
