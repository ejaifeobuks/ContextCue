# Simulator — planned, not implemented

Owns **Simulator** (SWE/testing): labeled scenarios, virtual time, simulated
readings and controlled device/model failures (baseline APP-19, INT-01).

- Exercises ContextEngine, RecommendationService and HardwareGateway through
  the same `contextcue_contracts` package used in production (today in
  `apps/contextcue-hardware/python/`).
- Never selected automatically at runtime; simulation labels must survive
  every stage.

## What already exists to build on

| Piece | Where | Reuse |
|---|---|---|
| Fake Bridge transport | `TestPeer` in [`tests/hardware/test_gateway.py`](../../tests/hardware/test_gateway.py) | Implements `connect`, `call`, `router_version`, `close`; injects failures (`failure`, `router_alive`) and returns MCU-format JSON for `cc_open`/`cc_read*`. Starting point for a scripted simulated transport |
| Virtual clock | `TestClock` in the same file | `monotonic()` and `utcnow()` driven by a settable `now`; `HardwareGateway(transport, clock=...)` accepts it, so freshness and expiry can be tested without waiting |
| Firmware-in-the-loop harness | [`tests/hardware/firmware/`](../../tests/hardware/firmware/) | Compiles the real sketch on a laptop with stubs for Modulino (Thermo, Light, Distance, Pixels, Buzzer), `Wire1`, Bridge and Zephyr; drives reachability, values and uptime from the test |
| MCU wire format | [hardware README](../../apps/contextcue-hardware/README.md#protocol-and-recovery) | Exact JSON shapes and limits (224 bytes) the simulated transport must produce |

## Implementation considerations

- **Labeling:** `HardwareGateway` currently sets `source_mode="real"` on every
  sample (`gateway.py`, `_ingest_module`). Feeding simulated MCU JSON through
  it unchanged would mislabel data. Add an explicit source-mode setting (or a
  simulated gateway) before the simulator produces anything a user can see.
- **Same contracts:** produce `GatewayState`/`SensorSample`/`HealthState`
  exactly as the real gateway does, including `quality`, `error` and health
  reasons, so ContextEngine cannot tell the paths apart except by the label.
- **Fixtures:** none exist yet. Baseline §6 requires deterministic, visibly
  labeled fictional calendar/weather fixtures with dated scenarios; the
  `arduino:weather_forecast` Brick's categories (`sunny`, `cloudy`, `rainy`,
  `snowy`, `foggy`) are a reasonable shape for weather fixtures. Never show the
  scenario clock as real wall-clock time.
- **Faults to cover** (baseline §8 scenario families): sensor
  disconnect/out-of-range/timeout, Bridge reset (new boot ID), MCU
  unresponsive versus router unavailable, requested stop, model
  timeout/malformed output, Bluetooth loss.
- **Real-fault reproduction on the board** (for checking the simulator against
  reality):
  - MCU hang: halt the MCU over SWD with OpenOCD (`/opt/openocd`,
    `openocd_gpiod.cfg`: `init; halt; sleep 6000; resume`); the gateway
    reports `mcu_unresponsive`.
  - Requested stop: `scripts/unoq.sh stop` ends with a `stopped` record.
  - Sensor faults: unplug a Modulino; the reason names it
    (`distance_disconnected`, `thermo_not_found`, ...).

## Recommended starting point

1. Extract `TestPeer`/`TestClock` into a reusable simulated transport and
   virtual clock with scripted timelines (values, faults, reboots).
2. Add the source-mode setting to the gateway so simulated samples are
   labeled `simulated`.
3. Write the first fixture scenarios (morning briefing, event plus rain,
   comfort persistence) shared with the application service's fixture
   adapters.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md) §6 and §8.
