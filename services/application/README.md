# Application service — planned, not implemented

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
