# Live Integrations Plan: Time, Location, Weather, Calendar and AI

Version 1 | 8 November 2026 | Proposed, not started

This plan replaces the remaining dummy data and unfinished integrations with
real functionality. It covers real time, device location, a live weather
adapter, a real calendar adapter and the on-board AI service. Nothing here is
implemented yet. Statements about the board's network, clock and storage are
marked as unverified where they still need checking on the hardware.

The [software baseline](../requirements/ContextCue_Software_Baseline_v1.md)
remains the authority for behavior and acceptance. Where this plan proposes a
change to a baseline decision (the calendar method), it says so and needs a
team decision first.

## 1. Current state

| Area | Implemented | Mocked or missing |
|---|---|---|
| Time | `clock.py` has `SystemClock` (real time) and `ScenarioClock` (demo time). Times are stored in UTC and shown in the user's time-zone setting. | `services/ui/main.py` always uses the demo `ScenarioClock`, even with real sensors. The app has one clock for everything, and the demo calendar and weather are fixed to 16 November 2026, so switching to real time alone would show stale data. |
| Location | Only a text label inside the demo forecast ("Demo City (fictional)"). The UI shows it and has a disabled "Change" button. | No location setting and no lookup. The UNO Q has no GPS, and no Modulino provides one. |
| Weather | Shared adapter contract (`SourceAdapter`), demo weather adapter, forecast selection for "now" and for the next event in `engine.py`, and UI icons for the five conditions. Arduino's `weather_forecast` Brick (Open-Meteo wrapper) is installed on the board. | The live adapter is a placeholder (`UnconnectedLiveAdapter`) that reports `disabled`. |
| Calendar | Demo calendar adapter, schedule logic (next event, urgency, busy time) and the same placeholder pattern for Google and Microsoft 365. | No live adapter. The baseline specifies OAuth through Clerk, which nothing implements. |
| AI | `RecommendationService` (queue, deadline, cancellation), the answer validator, the factual fallback and `LlamaCppModel` for llama.cpp's OpenAI-compatible API. The G1 benchmark app is committed (`apps/llm-benchmark/`). | Never connected: the board's dev app declares no `arduino:llm` Brick and passes no model address. The benchmark has only partly run (Qwen3.5-0.8B: about 21 s median per reply, 16/30 valid in prompt mode before the prompt fix, and the model server ran out of memory). |
| Running on the board | A hand-built App Lab dev app (`~/ArduinoApps/contextcue-ui-dev`) shows the UI with real sensors. | The full app is not packaged in the repo and cannot be rebuilt from it. Every on-board milestone below needs that first. |

## 2. Constraints that shape the plan

1. **The demonstration on 16 November uses the demo data by design.** The
   baseline makes live calendar and weather follow the demonstration, and makes
   actual AI generation required for it. AI is therefore the demonstration's
   critical path. Live weather and calendar are product work that can follow.
2. **The "dummy time" comes from coupling the clock to the demo data.** The fix
   has two parts: the real clock becomes the default, and the demo calendar and
   weather are anchored to today (events stored as offsets such as "today at
   10:00") so the demo data still works on real time. The demo clock remains a
   developer tool for jumping forward in time. The baseline already requires
   that a scenario clock is never shown as wall-clock time.

## 3. Dependencies

```text
Real time + time zone --> everything (schedule arithmetic, forecast selection,
                          notice expiry, AI prompts)
Location (lat/lon, place name, time zone) --> Weather
Calendar --> needs real time and time zone only, not location
AI --> needs only the ContextSnapshot contract. It can be built and tested on
       demo data now; live sources only make its inputs real.
Packaged app --> every on-board test of all of the above
```

## 4. What runs on the UNO Q and what needs an external service

| Capability | On the UNO Q (Linux side) | External service |
|---|---|---|
| Time | Linux system clock, read inside the app container. The MCU only knows its uptime, which the baseline says is not calendar time. | Network time (NTP) over Wi-Fi, the OS default. Unverified: whether the time survives a power cut without network (is there a battery-backed real-time clock?). |
| Location | Store the chosen place and derive local time from its time zone. | City lookup (geocoding) through Open-Meteo, no key. Optional rough detection by IP address through a third-party service, with user consent. |
| Weather | Adapter, cache, mapping to the five conditions, freshness rules. | Open-Meteo forecast API: free for non-commercial use, no key, attribution required. |
| Calendar | Fetching, parsing the calendar format (including recurrence and time zones), time arithmetic. | Google or Microsoft (a calendar subscription link or OAuth). |
| AI | All of it. The llama.cpp model runs on the board on the CPU only (the benchmark confirmed this runner has no Hexagon DSP build). | None. The baseline forbids silent cloud use; Arduino's `cloud_llm` Brick may only be an explicitly declared contingency. |

### 4.1 Location options

| Option | Extra hardware | Needs | Recommendation |
|---|---|---|---|
| Choose a city, looked up through Open-Meteo geocoding | None | Network and a way to type the city | **Use this.** The lookup also returns the place's IANA time zone, which sets the app's time zone. |
| Rough location from the IP address | None | A third-party service and its terms, user consent; city-level accuracy | Optional "detect roughly" action later. |
| Arduino `tps` Brick (Wi-Fi geolocation) | None | Arduino cloud credentials; the underlying service and cost need checking | Investigate; not required. |
| Qwiic GPS module | Yes (about $20-40) | Antenna with sky view; poor indoors | Not recommended for a desk device. |
| Browser geolocation in the kiosk browser | None | A Google location API key | No. |

### 4.2 Calendar options

The baseline chose Clerk-managed OAuth for Google and Microsoft 365. On a
kiosk without a keyboard that is the heaviest option: it needs a hosted web
app, HTTPS redirect URLs, a Clerk account and provider app registrations. This
plan proposes a staged approach, which **needs a team decision to amend the
baseline** (record it in the conflict resolution review):

1. **Calendar subscription link (ICS).** Google's "Secret address in iCal
   format" or Outlook's "Publish calendar" link. Read-only, with no OAuth or
   account setup on our side; the board fetches the link. It needs a library
   for recurring events and time zones (`icalendar` and
   `recurring-ical-events`, pure Python). Drawbacks: the link is effectively a
   password, and Google's feed can lag behind edits.
2. **Later, if needed: OAuth device-code sign-in** (enter a code on a phone).
   Microsoft Graph supports it for calendars. Whether Google allows calendar
   scopes in its device flow needs checking. Needs a Google Cloud project and a
   Microsoft Entra app registration.
3. **Clerk** only if the product later needs a real web account system.

## 5. Credentials, packages, configuration and hardware

| Item | Needed for | Notes |
|---|---|---|
| No key | Open-Meteo forecast and geocoding | Show "Weather data by Open-Meteo.com" (CC BY 4.0); stay under 10,000 calls a day (expected use is about 100). |
| Calendar subscription link | Calendar, stage 1 | Store as a secret in the app's data folder; never in logs, the UI or git. |
| Google Cloud OAuth client / Microsoft Entra app registration | Calendar, stage 2 | Only if we go beyond the subscription link. |
| `icalendar`, `recurring-ical-events` | Calendar | Add to the app's `requirements.txt`. App Lab installs it at start, so the board needs internet then. |
| `arduino:llm` Brick with Qwen3.5-0.8B (507 MB, already on the board) and possibly Gemma 3 1B (722 MB) | AI | The board's root partition had 2.1 GB free; keep one model and delete the other. |
| Model server settings `LLAMA_ARG_CACHE_RAM=0`, `LLAMA_ARG_N_PARALLEL=1` | AI | Without them the model server was killed for running out of memory. |
| Text entry on the touchscreen | City search; pasting the calendar link | No on-screen keyboard is installed. Proposed: a small on-screen keyboard in the UI for the city, and a `scripts/unoq.sh config ...` command (or a setup page reached from a laptop) for the long calendar link. |
| Hardware | None required | GPS only for automatic precise location, which is not recommended. |

## 6. Architecture fit and files likely to change

All new adapters run on the Linux side behind the existing `SourceAdapter`
contract and report source mode, freshness, validity and errors. They never
fall back to demo data. The MCU is unaffected. The AI worker stays isolated
behind `RecommendationService` and its validator.

| File | Change |
|---|---|
| New `apps/contextcue/` (`app.yaml`, `python/main.py`, `requirements.txt`) and `scripts/unoq.sh` | Packaged App Lab app: sensor firmware sketch, `arduino:llm` Brick and model server settings, plus deploy and config commands. |
| `services/ui/main.py` | Real clock by default (demo clock only with an explicit option); pass the model address and settings path. |
| `services/application/contextcue_app/clock.py` | Clock quality (synced, unsynced, scenario) for the UI and health view. |
| `services/application/contextcue_app/sources.py`, `services/application/fixtures/demo_scenarios.json` | Demo data anchored to today; new `OpenMeteoWeatherAdapter`, `IcsCalendarAdapter` and a geocoding helper. |
| `services/application/contextcue_app/settings.py`, `repository.py` | Location (name, latitude, longitude, time zone, how it was chosen) and calendar source settings; secrets stored separately from normal settings. |
| `services/application/contextcue_app/app.py` | Separate the clock from the scenario; actions to search for and set a location and to connect or test a calendar; scenario actions only with the demo clock; health entries for network, clock and each source. |
| `services/application/contextcue_app/engine.py` | Probably small. Real forecasts have hourly periods, so check that forecast selection still picks the right one. |
| `services/application/contextcue_app/recommendation.py` | Align the prompt and JSON-schema mode with the benchmark winner and set the measured timeout. Keep prompts small: the board reads input at about 12 tokens/s, so a 250-token prompt takes about 20 s and the baseline's 2048-token cap would take about 170 s. |
| `services/ui/assets/app.js`, `ui-logic.js` | Location search with an on-screen keyboard, calendar connection status, clock label ("Device time, synced"), Open-Meteo attribution, a "Generating..." state. |
| `docs/planning/CONFLICT_RESOLUTION_REVIEW.md` and the baseline | Record the calendar-method decision, if agreed. |

## 7. Implementation order and milestones

Each milestone ends with a check before the next one starts.

| # | Milestone | Why in this position | Exit criteria |
|---|---|---|---|
| M0 | Package the app in the repo with one-command deploy | Every later check happens on the board, and the current dev app cannot be rebuilt | `scripts/unoq.sh` deploys and opens the UI from a laptop; real sensors stable for 10 minutes. |
| M1 | Real time | Small, and everything else depends on it; anchoring the demo data keeps the demonstration working | The board shows the correct local time and date labelled "Device time"; demo events land on today; time zone and DST tests pass; clock quality shown. |
| M2 | AI on the board | Required for the demonstration, the highest risk and the most board time, so the benchmark starts first | Benchmark finished (Qwen in prompt and schema modes, Gemma); model and timeout chosen and recorded; the app produces validated AI-written notices on the board, with Why showing "AI generated, on device"; sensing and UI stay responsive during generation (T02). |
| M3 | Location | Weather needs it | City search from the touchscreen; latitude, longitude and time zone saved and restored after reboot; clearly labelled when offline. |
| M4 | Live weather | Uses M1 and M3 | Real forecast for the chosen city with the correct icon and rain chance; marked stale offline and never replaced by demo data; attribution shown. |
| M5 (done 8 November) | Real calendar (subscription link) | Independent of location; needs the secret-entry path and the calendar decision | Real events with correct times, including a recurring and an all-day event; the link never appears in logs or the UI; failures clearly labelled. |
| M6 | OAuth sign-in (optional, after the demonstration) | Only if the subscription link is not enough | Team decision first. |

**Parallel work:** the benchmark (M2) runs on the board unattended while M1, M3
and the parsing parts of M4 and M5 are built and tested on a laptop. With the
demonstration on 16 November, the target before it is M0, M1 and M2; M3 to M5
if time allows, since the baseline does not require them for the
demonstration.

## 8. Testing strategy

| Integration | Laptop tests | On the board |
|---|---|---|
| Time | Fixed-clock unit tests: DST changes, midnight, time-zone changes; demo data anchored to the current day. | Shown time against a phone; behavior with Wi-Fi off and after a reboot. |
| Location | Recorded Open-Meteo geocoding responses (no network in tests): no match, several matches, network error, malformed data. | Search, save, reboot, confirm it is restored. |
| Weather | Recorded forecast responses; mapping of every WMO weather code to the five conditions; freshness and staleness, timeouts, rate-limit errors; no fallback to demo data. | Compare with the Open-Meteo website; turn Wi-Fi off and check the labels. |
| Calendar | A set of test calendar files: recurring events with exceptions, all-day events, time zones, DST, cancelled and overlapping events; network and authentication failures; the link never logged. | A throwaway Google calendar with known events. |
| AI | Existing validator, fallback and cancellation tests against a fake model server. | The G1 benchmark (latency, validity, peak memory) plus human review for T03; trigger-to-text timing; sensing responsiveness during generation (T02); stopping the model service mid-request and confirming a labelled fallback. |

## 9. First investigations (next time the board is connected)

1. **Clock:** `timedatectl` (NTP synchronized? time zone?); reboot with Wi-Fi
   off and check the time.
2. **Network from inside the app container:** can it reach
   `api.open-meteo.com` (DNS and internet access)? Live weather, calendar and
   package installation depend on it.
3. **AI resources:** free space on the root partition; memory with the model
   server and the UI app running together; whether the `arduino:llm` Brick
   works in the same app as the sensor firmware and the router connection.
4. **Arduino `weather_forecast` Brick:** which timestamps and fields it
   exposes. If it hides forecast or update times, call Open-Meteo directly and
   reuse only its weather-code mapping.
5. **Calendar:** how far Google's secret calendar link lags behind edits;
   whether the team's Microsoft 365 organization allows publishing calendars;
   whether Google's device-code flow allows calendar scopes.
6. **Setup path:** whether the UI can be reached from a laptop on the same
   network (port 7000), which decides how secrets are entered.

## 10. Risks, blockers and unknowns

- **AI quality and speed (largest risk).** A 1B-class model may not reach T03
  (at least 90% acceptable grounded replies), and about 20 s per reply is slow.
  Mitigations: very small prompts, JSON-schema mode, a deadline with the
  factual fallback. If it still fails, the baseline requires an explicitly
  declared decision (a smaller task or an off-board model), never a silent
  cloud fallback.
- **Storage and memory.** Limited space on the root partition and a 2.5 GB
  memory cap for the model server.
- **Calendar method differs from the baseline.** A subscription link is not
  Clerk OAuth and needs a team decision. The link is a bearer secret, and
  Google's feed can lag.
- **Time without network.** Without a battery-backed real-time clock, a reboot
  with no Wi-Fi may give the wrong time, so clock quality must be shown rather
  than assumed.
- **Text entry.** The device has no keyboard, which affects location and
  calendar setup.
- **Privacy (APP-20).** Real calendar content must stay out of logs, and the
  demonstration keeps using fictional data.

## 11. Decisions needed

| Decision | Needed before | Proposed |
|---|---|---|
| Calendar method: subscription link (ICS) first instead of Clerk OAuth | M5 | Yes; amend the baseline and record it in the conflict resolution review. |
| Location setup: city search only, or also rough detection by IP address | M3 | City search first; IP detection optional later. |
| AI model and release timeout | End of M2 benchmark | Decided from measured results. |
