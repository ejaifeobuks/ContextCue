# Application service

**Status (7 November 2026): implemented for the fixture demo path, tested on a
laptop, not yet run on the board.** Code is in `contextcue_app/`, tests in
`tests/application/`. The UI that drives it is in [`services/ui`](../ui/).

## What is implemented

| Module | Class / role | Baseline |
|---|---|---|
| `engine.py` | ContextEngine: evidence store with material-change revisions, sensor normalization, comfort/proximity rules, calendar/weather loading, trigger evaluation, immutable `ContextSnapshot`s, evidence and obsolescence checks | APP-04, APP-05, APP-10, APP-11 |
| `schedule.py` | Next event, count, urgency, merged busy time, all-day list, local day from the IANA zone (DST days are 23/25 h) | APP-04 |
| `rules.py` | Comfort (60 s persistence, hysteresis, 15 min cooldown, stops on bad input), proximity (near <=500 mm, clear >650 mm, 500 ms, unknown is not far, wake only on clear to near), brightness (20-100% screen, 5-40% Pixels, 60/15% on Light failure, manual override) | APP-11, section 6 |
| `notifications.py` | NotificationService: notice IDs before generation, dedupe keys, P0-P2 priority and tie-breaks, expiry, Focus deferral, mute, quiet hours, audio policy, long-idle and away rules, Buzzer opt-in, one audio retry, acknowledgement tombstones, Pixels pattern and chime commands through `execute()`, late-callback guard | APP-12 to APP-16 |
| `repository.py` | DataRepository on stdlib `sqlite3`: migrations, transactions, settings, evidence, notices, tombstones, redacted diagnostics, 7-day retention, visible corruption recovery, clear-data | APP-17, section 8 |
| `recommendation.py` | RecommendationService port: one active plus one coalesced request, deadline, cancellation, validator (schema, category, evidence IDs, every number checked against cited evidence), labeled factual fallback, `LlamaCppModel` client for the `arduino:llm` endpoint | APP-06 to APP-08 (port only) |
| `sources.py` + `fixtures/demo_scenarios.json` | Adapter contract, `FixtureCalendarAdapter`, `FixtureWeatherAdapter`, four fictional dated scenarios; live choices report `disabled`, never fixture data | APP-05, APP-20 |
| `clock.py` | Device clock and the labeled scenario clock (runs at real speed, can jump forward) | section 6 |
| `dev_gateway.py` | `SimulatedGateway`: same surface and contract types as `HardwareGateway`, every sample labeled `simulated`; selected only with `--simulated` | APP-19 |
| `app.py` | `ContextCueApp`: 100 ms control loop, 1 s slow tick (sources, triggers, expiry, lease heartbeat), view model and actions for the UI | APP-01, APP-18 |

Run it with the UI: `python3 services/ui/main.py --simulated` (see the UI
README). Run the tests: `python3 -m unittest discover -s tests -v`.

## Decisions made while implementing

- **App contracts live in `contextcue_app/contracts.py`** (`ContextEvent`,
  `ContextSnapshot`, `ModelResponse`, `Notice`, `SourceResult`). Hardware
  contracts were not touched. Move them into `contextcue_contracts` once the
  AI service is a second app that needs to import them.
- **Fixture data always runs on the scenario clock**, labeled "Scenario time
  (simulated)". Sensor timestamps stay on the device clock, so sensor
  freshness comes from the gateway's `quality`, and only calendar/weather
  validity is compared with scenario time.
- **No model is configured by default.** Every request ends as
  `error: model_not_configured` and the notice shows a labeled factual
  fallback. That is the required failure behavior and does not count as
  generation (T03). Pass `--model-url http://127.0.0.1:9999/v1` once the G1
  benchmark picks a model.
- **No TTS engine yet** (`NullSpeech`). Speech routes report
  `no_tts_engine_selected`; volume is saved but reported as not applied.
- **Preparation cues** fire for the next valid event within 30 min (P1, P2
  within 10 min) and mention rain only at >=50%. They expire at event start.
  No travel time is invented.
- **Morning briefing** is requested by a confirmed clear to near transition
  inside the morning window, at most once per local date (dedupe key
  `morning:<date>`, restored from the database after a restart).
  "Brief me now" is always available.
- **Bounded questions** are four fixed buttons (no on-screen keyboard on the
  board). Anything else is rejected, and a question without valid data gets a
  "Can't answer from current data" status, never invented text.
- **Light references** (`light_dark_lux` 10, `light_bright_lux` 400) are
  uncalibrated placeholders until plan section C.

## Still open (not in this change)

- Real generation: G1 benchmark, model choice, prompt tuning (AI service).
- TTS engine and Bluetooth playback adapter (`SpeechPort`), volume applied
  through PipeWire (peripherals service, plan section D).
- Google / Microsoft 365 (Clerk) and Open-Meteo adapters.
- Packaging as an App Lab app (see the UI README) and the board run.
- Product decisions listed in the plan: long-idle interval (600 s default
  here), away-mode grace, return summary, important-repeat count.

---

*Original planning notes below.*


The single application backend (baseline §1: one node, no microservices). Owns
the SWE domain classes:

- **ContextEngine** — fixtures and live calendar/weather adapters, sensor
  normalization, schedule calculation, freshness and trigger rules, immutable
  context snapshots.
- **NotificationService** — notice priority, delivery policy, mute/Focus,
  acknowledgement, local TTS and the audio queue.
- **DataRepository** — settings, evidence, acknowledgements, diagnostics and
  retention.

Uses the `contextcue-hardware` app's `HardwareGateway` in-process for readings, health
and approved output commands, and the AI service for grounded advice. Uses
the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

## What already exists

**HardwareGateway** (`apps/contextcue-hardware/python/contextcue_hardware/gateway.py`):

- `read_state()` returns Thermo, Light and Distance `SensorSample`s and
  separate health for `bridge`, `thermo`, `light`, `distance`, with freshness
  limits already applied (Thermo 5 s, Light/Distance 2 s, Bridge 3 s). Stale
  samples come back as `stale`, never refreshed.
- Bridge health reasons say which side failed: `mcu_unresponsive:`,
  `router_unavailable:`, or `gateway_stopped:` (health state `stopped`).
  Treat `stopped` as a requested shutdown, not a fault.
- Outputs (firmware 0.2.0+): `execute(DeviceCommand) -> CommandResult`,
  `heartbeat(session_id)` (call about once a second; the lease lasts 3 s) and
  `read_outputs()`. Create one session per app start with
  `HardwareGateway.new_session_id()`. `accepted` only means queued on the MCU;
  completion appears in `read_outputs().last_result`. A failed send is
  reported as `failed ... (outcome unknown)` and is never retried, so
  NotificationService decides whether to issue a new command with a new ID.
  Use these methods, not the raw RPCs.

**Arduino Bricks that fit this service** (installed on the board):

| Brick | Use | Notes |
|---|---|---|
| `arduino:dbstorage_sqlstore` | DataRepository storage | `SQLStore("file.db")`: thread-safe SQLite, `create_table`, insert/update/delete, filtered queries, raw SQL. Migrations, transactions and retention (baseline §8) are still ours |
| `arduino:weather_forecast` | Post-demo `OpenMeteoWeatherAdapter` | Open-Meteo by city or coordinates, `forecast_days`, WMO codes mapped to `sunny`/`cloudy`/`rainy`/`snowy`/`foggy`. Check which timestamps it exposes before relying on it for freshness |

Neither replaces our contracts: wrap them behind ContextCue adapters so
fixtures and live sources share one interface.

## Implementation considerations

- **App packaging:** App Lab apps are `app.yaml` (with `bricks:`), `python/main.py`
  and `python/requirements.txt` (installed with `uv`), running in the
  `python-apps-base:0.13.2` container (Python 3.13). App Lab stops apps with
  SIGTERM; handle it as `contextcue_hardware/cli.py` does so shutdown is clean.
- **Speech:** Arduino's `arduino:tts` Brick supports only the VENTUNO Q, so
  `generateSpeech()` needs espeak-ng or Piper (choice and container
  installation are open; plan section D). Playback goes through the
  peripherals service.
- **Demo sources:** fixtures only, no network or account. Live Google,
  Microsoft 365 (Clerk OAuth) and Open-Meteo adapters follow the demo and must
  never silently fall back to fixtures.
- **Hardware semantics:** Distance out-of-range (`distance_timeout`) means
  unknown, not far away; Light is `ambient_light` in lux, not yet calibrated.
  Keep calibration offsets in the hardware manifest, not in policy code.
- Observed sampling on 6 November: Distance about 5.8 Hz against its 10 Hz
  target (being checked during calibration); size the 500 ms near-persistence
  rule accordingly.

## Recommended starting point

1. Phase 9.1/9.2 of the [implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md):
   adapter contract plus `FixtureCalendarAdapter` and `FixtureWeatherAdapter`
   with a virtual clock (shared with the simulator).
2. Comfort, proximity and preparation rules (9.3-9.5) against recorded or
   simulated `GatewayState` values, with deterministic tests.
3. NotificationService policy (Phase 10) once the gateway's `execute` and
   `heartbeat` exist.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md),
[implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md) phases 9 and 10.
