# ContextCue Remaining Modules Implementation Plan

## Purpose

This plan describes how to implement the ContextCue modules that are not yet
complete. The current milestone supports the real Modulino Thermo sensor,
Arduino Bridge communication, and the Linux hardware gateway. The remaining
work should extend those foundations without treating unsupported hardware as
available.

The main implementation boundary is:

```text
MCU modules
  -> Arduino Bridge protocol
  -> Python contracts and HardwareGateway
  -> ContextEngine and application policy
  -> UI, notifications, and audio
```

The existing Thermo implementation is in
`apps/contextcue-hardware/sketch/src/sensors/SensorController.cpp`. The firmware entry point
is `apps/contextcue-hardware/sketch/sketch.ino`, and the Linux gateway is
`apps/contextcue-hardware/python/contextcue_hardware/gateway.py`.

## Final plan review status

This plan review does not implement source code. Development may begin only
after the readiness gates in this document are satisfied. The current Thermo
path is ready for incremental extension; Light, Distance, Pixels, Buzzer,
touchscreen, Bluetooth audio, live providers, and the local LLM remain
implementation work.

The plan must be executed in thin vertical slices:

1. Prove one hardware device with the official Arduino example/API.
2. Integrate that device into the MCU cached-state contract.
3. Decode it on Linux and prove failure/recovery behavior.
4. Only then allow application policy to consume it.

No application behavior may depend on a module that has not passed its
physical bring-up gate.

## Official Arduino reuse findings

Before writing device drivers, reuse the pinned `Arduino_Modulino` library and
its official examples. The library already supplies the required device
abstractions over I2C:

- **Thermo:** existing project code uses `Modulino.begin(Wire1)` and the
  library's Thermo API.
- **Light:** Arduino's official `Light_Basic` example uses
  `ModulinoLight`, `light.begin()`, `light.update()`, `getAL()` for ambient
  light in lux, and `getIR()` for infrared. Use `getAL()` for the first
  ContextCue metric; do not reimplement the optical sensor protocol.
- **Distance:** the official `Distance_Basic` example uses
  `ModulinoDistance`, `distance.begin()`, `distance.available()`, and
  `distance.get()` in millimeters. Preserve the `available()` check and map
  invalid/`NaN` results to the project quality contract.
- **Pixels:** the official Pixels API provides `set(index, color, brightness)`,
  `clear()`, and `show()` for eight RGB LEDs. Build ContextCue patterns on
  these calls rather than accessing the LED controller directly.
- **Buzzer:** the official Buzzer API provides `tone(frequency, duration)` and
  `noTone()`. The project must wrap these calls with its own duration,
  session, lease, mute, and duplicate-command limits.

References to pin and verify during implementation:

- [Arduino Modulino library API](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/docs/api.md)
- [Light Basic example](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/examples/Modulino_Light/Light_Basic/Light_Basic.ino)
- [Distance Basic example](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/examples/Modulino_Distance/Distance_Basic/Distance_Basic.ino)
- [Pixels Basic example](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/examples/Modulino_Pixels/Pixels_Basic/Pixels_Basic.ino)
- [Buzzer Basic example](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/examples/Modulino_Buzzer/Buzzer_Basic/Buzzer_Basic.ino)
- [Modulino Light documentation](https://docs.arduino.cc/hardware/modulino-light/)
- [Modulino Distance documentation](https://docs.arduino.cc/hardware/modulino-distance/)

The exact library commit/version must be pinned in `sketch/sketch.yaml` and
recorded in the bring-up report. If the installed version differs from these
references, stop and re-check method names, units, range behavior, and
examples before coding against it.

## Resolved product decisions

- Use the **Distance** module, not a motion sensor, for proximity.
- Distance may wake the UI and provide a routing hint, but it must not infer
  identity, occupancy, attention, sleep, or absence.
- Use **LLM-generated briefings** as the intended recommendation behavior.
- Keep deterministic templates as a clearly labeled fallback when generation
  fails; fallback text does not count as successful LLM acceptance.
- Support fixed/demo and live calendar sources side by side.
- Initially support live calendar providers through separate adapters for
  Google Calendar and Microsoft 365 / Outlook Calendar.
- Use fixed/demo sources for the Qualcomm demonstration.
- Use Pixels and Buzzer as the selected outputs; do not add a separate demo
  light to the required product path.
- Do not include a physical Knob; use the touchscreen for speech-volume
  control and the active notice's Why view.
- Require confirmed, allowlisted, bounded output commands.
- Suppress automatic speech and Buzzer output when Distance is clear and the
  device has remained idle for the configured long-idle interval, or when
  automatic Buzzer output is suppressed. Keep the notification visual and
  allow explicit playback when otherwise eligible.
- When the user appears away, announce a reminder at most once only if
  away-mode audio is explicitly enabled and Bluetooth is connected.
- When Bluetooth is disconnected, queue ordinary reminders silently and show
  a summary when the user returns.
- Do not repeat unanswered reminders indefinitely. Only reminders explicitly
  marked important may use a bounded repeat policy.
- Do Not Disturb suppresses ordinary sounds and collects reminders for later.
- Keep camera gestures as a stretch feature, limited to acknowledging a notice,
  disabled by default, and outside the core acceptance path.

The following decisions remain open and must not be hidden inside irreversible
interfaces:

- The exact long-idle interval and the behavior for unknown Distance readings.
- The exact local model, runtime, timeout, memory budget, and fallback format.
- The final UNO Q Linux touchscreen device/API and display toolkit.
- The Linux Bluetooth audio backend, endpoint identity model, and absolute
  volume support.
- The exact Clerk OAuth flow, provider scopes, token storage, and reauth UX.
- Open-Meteo attribution, rate, terms, location-resolution, and caching policy.
- The final Bridge response shape after measuring encoded size and latency.
- Whether all five selected Modulinos can share the intended Qwiic topology
  without address conflicts or power/throughput problems.
- The long-idle timer, away-mode transition/grace period, return-summary
  rules, and bounded important-reminder repeat count.

## Guiding rules

- Define shared contracts before adding new behavior.
- Keep sensor acquisition separate from application interpretation.
- Treat missing, stale, invalid, and unavailable values explicitly.
- Use `null` for missing measurements; never use fabricated zero values.
- Preserve boot IDs, sequence numbers, observation times, and receipt times.
- Keep hardware reads in the MCU main loop, not inside Bridge RPC callbacks.
- Do not advertise a module until it is implemented and verified.
- Keep output commands bounded and allowlisted.
- Validate real hardware behavior separately from unit-test behavior.
- Do not infer units, ranges, timing, or availability from a product name;
  verify them against the installed library, datasheet, and assembled board.
- Pin every firmware/library/Python dependency before collecting acceptance
  evidence.

## Phase 0: Establish shared contracts

### 0.1 Define stable module and metric IDs

Use these module IDs:

```text
thermo
light
distance
pixels
buzzer
```

Keep module IDs separate from metric names. For example:

```text
module_id = "thermo", metric = "temperature"
module_id = "thermo", metric = "humidity"
module_id = "light", metric = "ambient_level"
module_id = "distance", metric = "distance"
```

### 0.2 Reuse the common sensor contract

All sensor readings should use the existing `SensorSample` structure in
`apps/contextcue-hardware/python/contextcue_contracts/`.

Each reading should retain:

- Boot ID
- Sequence number
- Event ID
- Module ID
- Metric
- Value
- Unit
- Observation uptime
- Observation timestamp
- Receipt timestamp
- Clock quality
- Source mode
- Quality
- Error reason

### 0.3 Define common quality behavior

Use the existing quality values:

```text
valid
stale
unavailable
invalid
```

Rules:

- Zero remains a valid value when the device supports it.
- Invalid ranges are reported explicitly.
- Every acquisition attempt receives a sequence number.
- Repeated polling does not refresh an old observation timestamp.
- A firmware reboot creates a new boot identity.

### 0.4 Extend capability reporting

Extend `HardwareCapabilities` to report, where supported:

- Configured modules
- Available modules
- Units
- Supported sampling rates
- Driver and library versions
- Output patterns and limits

### 0.5 Freeze the build and hardware manifest

Before adding the second module:

1. Record the UNO Q board, Linux image, Zephyr/core version, RouterBridge,
   RPClite, `Arduino_Modulino`, and sensor dependency versions.
2. Keep the existing pins in `sketch/sketch.yaml` unless an upgrade is
   justified by a failed requirement and revalidated.
3. Record each module's exact product variant, I2C address, cable topology,
   power state, and physical mounting position.
4. Confirm the Qwiic bus used by the UNO Q is `Wire1`; do not assume the
   Arduino UNO default `Wire` bus applies.
5. Capture a clean baseline: compile, existing gateway tests, Thermo readings,
   Bridge reconnect, and MCU reboot.

The manifest is a prerequisite for all physical acceptance results. A test
result without its board, firmware, library, and wiring identity is not
reproducible.

### 0.6 Prove the shared Qwiic topology

Use the official library's initialization path and an instrumented bring-up
sketch to verify that all selected modules can coexist. Record detected
addresses, initialization order, bus errors, current draw if available, and
whether any module requires a different startup delay. Resolve address
collisions, cable/power limits, or unsupported combinations before
implementing multi-module scheduling.

## Phase 1: Refactor the MCU sensor layer

The current `SensorController` owns Thermo only. Expand it while keeping each
sensor's state and timing independent.

### 1.1 Add independent reading state

Add separate state for:

```cpp
ThermoReading thermoReading_;
LightReading lightReading_;
DistanceReading distanceReading_;
```

Each reading should contain:

- Sequence number
- Observation uptime
- Value
- Quality
- Error
- Availability state

Do not use one shared sensor sequence for all modules. A Light failure should
not make a valid Thermo sequence appear changed.

### 1.2 Add independent schedules

Use the project targets:

| Module   | Target rate |
| -------- | ----------: |
| Thermo   |        1 Hz |
| Light    |        5 Hz |
| Distance |       10 Hz |

Use separate due checks such as:

```cpp
readThermoIfDue(now);
readLightIfDue(now);
readDistanceIfDue(now);
```

### 1.3 Add initialization and recovery

Each module needs:

- Startup initialization
- Reachability checks
- Disconnect detection
- Reconnect behavior
- Named failure reasons

Example errors:

```text
light_not_found
light_disconnected
light_read_failed
light_out_of_range
distance_not_found
distance_disconnected
distance_timeout
distance_out_of_range
```

After a disconnect, old values must not remain marked valid.

### 1.4 Keep drivers out of RPC callbacks

Continue the existing design:

- Hardware drivers run in the main loop.
- RPC callbacks return cached readings.
- RPC callbacks do not perform slow I2C operations.

## Phase 2: Implement the Light module

### 2.1 Verify the physical API first

Before writing the driver:

1. Confirm the physical Modulino Light model.
2. Confirm the pinned Arduino library version and inspect the official
   `Light_Basic` example.
3. Use `ModulinoLight.begin()`, `update()`, and `getAL()` first.
4. Confirm that the installed version returns ambient light in lux; otherwise
   stop and revise the contract rather than relabeling raw data as lux.
5. Verify the supported sampling rate and whether `update()` blocks.

The project must not report lux until the raw-to-lux conversion is verified.

### 2.2 Choose the first exposed value

Preferred contract when verified:

```text
metric: ambient_light
unit: lux
```

Safe initial contract when conversion is not verified:

```text
metric: ambient_level
unit: normalized
value: 0.0 to 1.0
```

### 2.3 Validate Light readings

Validate:

- Communication success
- Numeric value
- Expected lower and upper bounds
- Calibration state
- Sampling interval

Invalid data should have a null value and a named reason, for example:

```text
quality: invalid
error: light_out_of_range
```

### 2.4 Add calibration

Support calibrated dark and bright references:

```text
dark_reference
bright_reference
```

Normalize raw values between those references when appropriate. Report
calibration failures instead of hiding them.

### 2.5 Test Light behavior

Cover:

- Dark, bright, and intermediate readings
- Zero values where valid
- Out-of-range values
- Disconnection
- Reconnection
- Staleness after two seconds
- Duplicate sequence handling
- Calibration behavior

## Phase 3: Implement the Distance module

### 3.1 Verify the physical ToF API

Before implementation:

1. Confirm the Modulino Distance model.
2. Confirm the pinned library version and inspect the official
   `Distance_Basic` example.
3. Use `ModulinoDistance.begin()`, `available()`, and `get()`.
4. Confirm how the installed version represents invalid readings, including
   `NaN`, timeout, and no-new-sample cases.
5. Measure the useful range and mounting effects on the assembled device.

The project requires time-of-flight distance, not ultrasonic distance.

### 3.2 Expose millimeters

Use:

```text
module_id: distance
metric: distance
unit: mm
```

### 3.3 Define failure behavior

Possible errors include:

```text
distance_not_found
distance_disconnected
distance_timeout
distance_out_of_range
distance_invalid
```

An out-of-range result must not be interpreted as “far away.” It is unknown.

### 3.4 Keep proximity policy outside the sensor controller

The MCU reports distance only. The application layer applies:

- Near threshold: `<= 500 mm`
- Clear threshold: `> 650 mm`
- Near persistence: `500 ms`
- Cooldown and wake behavior

The sensor controller must not infer identity, occupancy, attention, or sleep.

### 3.5 Test Distance behavior

Cover:

- Normal distances
- Physically valid near distances
- Timeouts
- Out-of-range readings
- Disconnect and reconnect
- Near threshold
- Clear threshold
- Hysteresis
- 500 ms persistence
- Staleness after two seconds

## Phase 4: Extend the MCU-to-Linux state protocol

The current `cc_read` response only represents Thermo. Add Light and Distance
without violating the compact transport limit.

### 4.1 Evaluate response size

First measure whether one response containing all three sensor groups fits
within the current 224-byte limit.

A possible structure is:

```json
{
  "v": "1.0",
  "boot": "...",
  "thermo": { "seq": 7, "at": 11500, "q": "valid", "t": 24.2, "h": 45.0 },
  "light": { "seq": 42, "at": 11900, "q": "valid", "v": 0.68 },
  "distance": { "seq": 84, "at": 11950, "q": "valid", "mm": 430 }
}
```

### 4.2 Choose a protocol shape based on measurement

If the combined response is too large, use separate RPC calls or a compact
array format. Do not silently increase the limit without confirming the
Arduino Bridge and MessagePack constraints.

Possible calls:

```text
cc_open
cc_read_thermo
cc_read_light
cc_read_distance
```

### 4.3 Preserve independent sensor state

A Light failure must not invalidate valid Thermo and Distance readings. Each
sensor must have its own:

- Sequence
- Observation time
- Quality
- Error
- Freshness state

## Phase 5: Extend Python contracts and HardwareGateway

### 5.1 Generalize decoding

Decode each module independently using the existing strict validation style in
`gateway.py`.

Expected combinations include:

```text
thermo: valid
light: unavailable
distance: valid
```

One missing sensor must not make the entire gateway fail.

### 5.2 Track freshness independently

Use these limits:

| Component        | Expiration |
| ---------------- | ---------: |
| Thermo           |  5 seconds |
| Light            |  2 seconds |
| Distance         |  2 seconds |
| Bridge heartbeat |  3 seconds |

Report health separately for:

- Bridge
- Thermo
- Light
- Distance

### 5.3 Preserve existing safety checks

For each module:

- Reject sequence regressions.
- Reject uptime regressions within one boot.
- Reject content changes under the same sequence.
- Reject invalid ranges.
- Clear old state after a boot ID changes.
- Keep observation time unchanged during duplicate polling.

### 5.4 Advertise only implemented features

For example:

```python
configured_modules = ("thermo", "light", "distance")
available_modules = ("thermo",)
```

Once Light is implemented and detected, it can be added to
`available_modules`.

## Phase 6: Implement touchscreen controls

Touchscreen controls run on the UNO Q Linux side and are the only user input
surface in the selected hardware.

The touchscreen should provide:

- Speech-volume adjustment for the selected Bluetooth speaker or headphones.
- Navigation to the active notice's Why view.
- Touch equivalents for acknowledgement, mute, and Focus.

Volume changes belong to the Linux audio layer, not the MCU. Keep volume
bounded from 0% through 100%, apply the selected endpoint's supported volume
operation, and report unsupported or failed changes explicitly.

Touch input must be debounced by the Linux input layer, produce explicit
acknowledgement, mute, Focus, navigation, and volume actions, and report
unsupported or failed audio operations visibly.

## Phase 7: Implement Pixels and Buzzer

### 7.1 Add an OutputController

It should own:

- Pixel patterns
- Pixel brightness
- Buzzer tone state
- Output lease
- Safe shutdown behavior

### 7.2 Use allowlisted commands

Supported commands should be fixed:

```text
pixels.set_pattern
pixels.off
buzzer.short_tone
buzzer.stop
outputs.stop
```

Do not permit raw GPIO, arbitrary bus addresses, shell commands, or
free-form model-generated hardware commands.

### 7.3 Enforce limits on the MCU

The MCU must independently enforce:

- Allowed patterns
- Brightness bounds
- Tone duration maximum
- Session identity
- Command expiration
- Duplicate-command rejection

Tone duration is capped at 250 ms without looping.

### 7.4 Add an output lease

Linux renews the lease approximately once per second. If it expires after
approximately three seconds:

- Stop the buzzer.
- Turn Pixels off.
- Enter a safe state.

### 7.5 Keep output execution separate from policy

The MCU executes bounded commands. It does not decide:

- Notice priority
- Focus state
- Mute policy
- Whether a model response is valid

### 7.6 Prove official output behavior before policy integration

Run the official Pixels and Buzzer examples on the assembled topology first.
Confirm that `show()` is required for Pixels, that brightness is bounded as
documented by the installed library, and that `noTone()` reliably stops a
tone. Measure actual tone duration and Pixel update latency. Only then add
the ContextCue command wrapper and lease behavior.

## Phase 8: Expand the Python gateway API

Add gateway operations in dependency order.

### State and discovery

```python
get_capabilities()
read_state(after_event_seq=None)
get_device_health()
```

### Outputs

```python
execute(command)
heartbeat(session_id)
```

### Linux peripherals

```python
audio_discover()
audio_pair()
audio_select()
audio_status()
audio_play()
audio_stop()
set_display_level()
```

Every operation needs strict validation, a timeout, explicit errors, recovery
behavior, and fake-peer tests.

## Phase 9: Implement application policy

### 9.1 Implement ContextEngine

The planned ContextEngine should:

- Normalize sensor values
- Check freshness
- Load calendar and weather data through interchangeable adapters
- Calculate schedule facts
- Apply comfort rules
- Apply proximity rules
- Build immutable context snapshots
- Decide when triggers are eligible

### 9.2 Implement calendar and weather adapters

Create a shared contract that both fixed/demo and live sources implement.
Adapters translate provider-specific data into ContextCue events containing
source identity, source mode, observation time, validity, freshness, and error
information.

Implement these initial adapters:

- `FixtureCalendarAdapter`
- `FixtureWeatherAdapter`
- `GoogleCalendarAdapter`
- `Microsoft365CalendarAdapter`

The ContextEngine must not depend on Google or Microsoft-specific response
formats. Fixed fixtures must remain available for offline development,
repeatable tests, and the Qualcomm demonstration. Live providers must report
authentication, network, rate-limit, malformed-response, and stale-data
failures explicitly; they must never silently substitute simulated data.

Use Clerk for application identity and the Google/Microsoft OAuth connection
flow. Clerk supplies provider authorization; it is not a calendar adapter.
The two calendar adapters remain responsible for calling Google Calendar or
Microsoft Graph and translating provider responses into the shared ContextCue
calendar contract.

Live calendar setup is optional during onboarding. Users can continue with
fixed/demo sources, or explicitly choose Google Calendar or Microsoft 365.
The Qualcomm demonstration uses fixed/demo data and must not require Clerk or
an internet connection.

Keep provider tokens and secrets backend-only. Handle consent, token expiry,
revocation, reauthorization, provider errors, and unavailable accounts
explicitly. Use **Open-Meteo** as the selected initial live weather provider. Implement an
`OpenMeteoWeatherAdapter` that returns the shared ContextCue weather contract.
Verify coverage, usage terms, attribution requirements, freshness, rate
handling, timeout behavior, malformed responses, and offline recovery before
production deployment.

Keep `FixtureWeatherAdapter` available for the Qualcomm demonstration,
repeatable tests, and controlled failure scenarios. Live failures must remain
visible and must never silently become simulated values.

Calendar and weather adapters run on the UNO Q Linux side. The Linux
application handles OAuth, provider API calls, caching, normalization,
time-zone conversion, and freshness. The MCU receives none of the account
credentials or external calendar data; it remains responsible for sensors,
inputs, and bounded outputs.

### 9.3 Implement Thermo comfort policy

Use temperature and humidity together. Require persistence, apply hysteresis
and cooldown, and stop the condition when readings become stale or unavailable.

### 9.4 Implement Light policy

Map calibrated ambient level to screen and Pixel brightness. Respect manual
brightness overrides and use the defined fallback brightness when Light is
unavailable.

### 9.5 Implement Distance policy

Require near readings to persist for 500 ms, use separate near and clear
thresholds, and avoid repeated wake events from a stationary object.

Never infer identity, room occupancy, sleep, or attention.

Automatic audio policy must also track the last meaningful user interaction.
If Distance is clear and the device remains idle for the configured long-idle
interval, suppress automatic speech and Buzzer output while retaining the
visual notice. Explicit playback remains possible when the notice is valid,
fresh, unacknowledged, and not blocked by mute or another policy.

The resulting routing rules are:

| Situation                                       | Behavior                                                                                          |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| User appears at the desk                        | Show the reminder; play one short chime or spoken notification if enabled.                        |
| User appears away and Bluetooth is connected    | Announce once only when away-mode audio is enabled; keep the reminder pending on screen.          |
| User appears away and Bluetooth is disconnected | Queue ordinary reminders silently; show a summary when the user returns.                          |
| Do Not Disturb is enabled                       | Suppress ordinary sounds and collect reminders for later.                                         |
| Reminder gets no response                       | Do not repeat indefinitely; allow limited repeats only for reminders explicitly marked important. |

### 9.6 Implement the RecommendationService

Build an asynchronous, bounded local LLM worker that receives an immutable
context snapshot and produces a concise briefing or answer. It must support
multiple categories, including morning, preparation, comfort, and bounded
context questions.

The service must:

- Include evidence IDs and the request/snapshot revision in each result.
- Enforce generation deadlines and output-size limits.
- Prevent model output from issuing hardware commands.
- Validate every factual claim against current evidence.
- Reject obsolete, malformed, unsupported, or expired results.
- Cancel or invalidate late results after relevant context changes.
- Produce a clearly labeled factual fallback when generation fails.

Benchmark the selected model and runtime on the UNO Q before treating LLM
generation as accepted. Record latency, memory use, output limits, and failure
behavior.

## Phase 10: Implement notifications and UI

Implement the planned notification behavior:

- Notice identity
- Priority
- Expiration
- Deduplication
- Acknowledgement
- Focus
- Mute
- Quiet hours
- Visual output
- Confirmed Pixel and Buzzer output
- Bluetooth audio subject to policy and endpoint readiness

The flow should be:

```text
ContextEngine
  -> creates an eligible notice
NotificationService
  -> applies delivery policy
HardwareGateway
  -> sends approved bounded output
```

The MCU must not bypass application policy.

### 10.1 Implement the camera stretch feature only after the core path

Keep camera capture and gesture recognition disabled by default. If the stretch
feature is attempted, allow only a deliberate gesture that acknowledges a
notice. Require a reliability gate, including controlled accuracy tests and
zero unsafe actions. Failure of this feature must not affect the core product
or demonstration.

## Phase 11: Testing strategy

### 11.1 Contract tests

Use fake Bridge responses to test:

- Valid readings
- Missing readings
- Invalid ranges
- Duplicate sequences
- Stale data
- Reboots
- Disconnects
- Malformed responses
- Independent module failures

Extend `tests/hardware/test_gateway.py` or add focused test modules as the gateway
surface grows.

Also test the output, policy, and adapter contracts without hardware:

- Allowlisted commands, bounds, expiration, session identity, and duplicate
  handling.
- Output lease expiry and recovery after reconnect.
- Clear/near hysteresis, persistence, stale/unknown Distance, and long-idle
  audio suppression.
- At-desk, away-mode opt-in, disconnected Bluetooth, Do Not Disturb, return
  summary, acknowledgement, expiration, and bounded important-reminder
  repeats.
- Fixture/live adapter normalization, malformed data, timeout, rate limit,
  token expiry, revoked consent, stale cache, and no silent fixture fallback.
- Touchscreen actions, volume bounds, unsupported endpoint operations, and
  explicit error presentation.
- Recommendation evidence validation, obsolete-result cancellation, timeout,
  and labeled fallback behavior.

Every test must assert both the action and the observable reason/state. A
successful function return alone is not proof that audio was heard or that a
Pixel/Buzzer physically changed state.

### 11.2 Build and static validation

At each vertical slice, run:

1. `git diff --check`.
2. The repository gateway test task:
   `python3 -m unittest discover -s tests -v`.
3. The pinned Arduino CLI compile task for the UNO Q sketch.
4. Python import/compile checks and type/lint checks when configured.
5. A protocol fixture compatibility test against the previous Thermo-only
   response.

Do not treat a laptop-only test pass as board validation.

### 11.3 Firmware tests

Test where practical:

- Sampling intervals
- Sequence increments
- Failed initialization
- Reconnection
- Range validation
- JSON size limits
- Command bounds
- Duplicate command handling
- Output lease expiry
- Safe output state
- Official library initialization failures and no-new-data behavior
- Non-blocking main-loop behavior while Bridge callbacks are serviced

### 11.4 Physical board verification

Unit tests cannot validate:

- Actual I2C behavior
- Electrical connections
- Sensor calibration
- Sensor conversion accuracy
- Driver blocking behavior
- Actual sampling rates

Record:

- Hardware model and variant
- Actual readings
- Disconnect and reconnect behavior
- MCU reboot behavior
- Bridge reset behavior
- Measured sampling rates
- Calibration values
- Timing and acquisition delays
- Qwiic addresses, topology, cable lengths, and power observations
- Pixels `show()` behavior and Buzzer `noTone()` stop behavior
- UI touch latency, Bluetooth endpoint loss/reconnect, and audio routing

### 11.5 Acceptance gates

Do not advance to the next layer until the current gate passes:

1. **Hardware gate:** official Arduino example works on the exact assembled
   module and the result is recorded with versions and wiring.
2. **Firmware gate:** cached acquisition is non-blocking, bounded, sequenced,
   and recovers from disconnects.
3. **Protocol gate:** encoded responses fit the measured limit and preserve
   independent module failures.
4. **Gateway gate:** strict decoding, freshness, reboot, and health behavior
   pass automated tests.
5. **Output gate:** Pixels/Buzzer commands are bounded and lease-safe on real
   hardware.
6. **Policy gate:** deterministic notification/audio scenarios pass with
   fixtures and simulated failures.
7. **Provider gate:** live adapters pass consent, expiry, outage, freshness,
   and no-fallback tests; the demo still works offline.
8. **LLM gate:** the selected local model meets the recorded latency,
   memory, evidence-grounding, and failure criteria on the UNO Q.
9. **System gate:** two clean setup/recovery runs complete and all failures
   remain visible without unsafe or repeated output.

## Recommended implementation order

1. Shared multi-module contracts
2. MCU `SensorController` refactor
3. Light sensor
4. Distance sensor
5. Multi-module Bridge response
6. Python gateway decoding and health
7. Physical sensor calibration
8. Calendar/weather adapter contracts and fixed fixtures
9. Google Calendar and Microsoft 365 adapters
10. Touchscreen controls and acknowledgement
11. `OutputController`
12. Output lease and safe state
13. `ContextEngine`
14. RecommendationService and local LLM validation
15. `NotificationService`
16. Peripheral and audio adapter
17. User interface
18. Optional camera gesture experiment
19. Full integration and acceptance testing

The critical dependency is reliable hardware contracts before application
behavior. This prevents ContextCue from making decisions based on unclear,
stale, invalid, or fabricated sensor data.

## Final readiness assessment

### Ready to begin

- Preserve and baseline the existing Thermo/Bridge/Gateway path.
- Freeze versions and create the hardware manifest.
- Prove the five-module Qwiic topology.
- Use the official `Arduino_Modulino` APIs and examples for Light, Distance,
  Pixels, and Buzzer.
- Add contract tests and protocol-size measurements before changing the
  response shape.

### Requires investigation before implementation

- Exact Light and Distance library behavior at the pinned version, including
  units, invalid values, blocking time, and sampling limits.
- Whether all five modules coexist on the selected bus topology and power
  budget.
- Final Bridge response encoding and size.
- Touchscreen and Bluetooth Linux APIs, endpoint/volume semantics, and failure
  UX.
- Away-mode, long-idle, unknown-Distance, Do Not Disturb, return-summary, and
  important-repeat parameters.
- Clerk scopes/token lifecycle and Open-Meteo operational requirements.
- UNO Q local LLM runtime/model feasibility.

### Technical risks

- The plan's target sampling rates may exceed what the official drivers,
  shared I2C bus, Bridge transport, or main loop can sustain.
- A response that fits as JSON may not fit after Bridge encoding or future
  error fields are added.
- Distance and Light readings depend on mounting, reflective surfaces, and
  calibration; nominal datasheet behavior is not desk-level behavior.
- Bluetooth connection does not prove that a person heard audio.
- A local LLM may fail the UNO Q memory or latency budget.
- Provider authentication, network failure, and stale caches can produce
  misleading context unless every state is labeled.

### Required review outcome

The plan is suitable for development planning, but it is **not yet a
pass-through implementation checklist**. The first work item must be the
manifest/topology/API verification gate. Any failed gate must update the
contract and acceptance tests before dependent implementation continues.
