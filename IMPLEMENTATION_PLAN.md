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
`apps/contextcue_hardware/sketch/SensorController.h`. The firmware entry point
is `apps/contextcue_hardware/sketch/sketch.ino`, and the Linux gateway is
`apps/contextcue_hardware/python/contextcue/gateway.py`.

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

## Phase 0: Establish shared contracts

### 0.1 Define stable module and metric IDs

Use these module IDs:

```text
thermo
light
distance
buttons
knob
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
`apps/contextcue_hardware/python/contextcue/contracts.py`.

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

| Module | Target rate |
|---|---:|
| Thermo | 1 Hz |
| Light | 5 Hz |
| Distance | 10 Hz |

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
2. Confirm its Arduino library and version.
3. Identify the raw reading API.
4. Determine whether the API returns lux or a raw value.
5. Verify the supported sampling rate.

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
2. Confirm the installed library.
3. Verify the measurement API.
4. Identify valid and out-of-range values.
5. Measure the useful range after mounting.

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
  "thermo": {"seq": 7, "at": 11500, "q": "valid", "t": 24.2, "h": 45.0},
  "light": {"seq": 42, "at": 11900, "q": "valid", "v": 0.68},
  "distance": {"seq": 84, "at": 11950, "q": "valid", "mm": 430}
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

| Component | Expiration |
|---|---:|
| Thermo | 5 seconds |
| Light | 2 seconds |
| Distance | 2 seconds |
| Bridge heartbeat | 3 seconds |

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

## Phase 6: Implement Buttons and Knob

Buttons and the Knob are input controls rather than environmental sensors.

### 6.1 Add an InputController

The MCU-side controller should:

- Read Buttons
- Read Knob rotation
- Read Knob presses
- Debounce inputs
- Produce ordered control events
- Request immediate local buzzer cancellation

### 6.2 Define ControlEvent

Each event should include:

- Event ID
- Boot ID
- Sequence number
- Control ID
- Event type
- Rotation delta when applicable
- Observation time

Examples:

```json
{"control_id": "button_a", "event_type": "press"}
```

```json
{"control_id": "knob", "event_type": "rotate", "delta": 2}
```

### 6.3 Add retained events

Implement a 128-event ring buffer with:

- Stable event IDs
- Gateway acknowledgement
- Visible overflow errors
- No silent event loss

Potential operations:

```text
read_state(after_event_seq)
ack_events(boot_id, through_seq)
```

### 6.4 Debounce inputs

Use the project targets:

- Input scan: no slower than 20 ms
- Stable debounce: approximately 30 ms

Verify actual behavior on the board.

### 6.5 Stop the local buzzer

Designated controls should immediately stop the MCU buzzer even if Linux is
temporarily unavailable. This does not stop Bluetooth audio, which remains a
Linux responsibility.

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

## Phase 8: Expand the Python gateway API

Add gateway operations in dependency order.

### State and discovery

```python
get_capabilities()
read_state(after_event_seq=None)
get_device_health()
```

### Input events

```python
ack_events(boot_id, through_seq)
```

### Outputs

```python
execute(command)
heartbeat(session_id)
```

### Later Linux peripherals

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
- Load labeled calendar and weather fixtures
- Calculate schedule facts
- Apply comfort rules
- Apply proximity rules
- Build immutable context snapshots
- Decide when triggers are eligible

### 9.2 Implement Thermo comfort policy

Use temperature and humidity together. Require persistence, apply hysteresis
and cooldown, and stop the condition when readings become stale or unavailable.

### 9.3 Implement Light policy

Map calibrated ambient level to screen and Pixel brightness. Respect manual
brightness overrides and use the defined fallback brightness when Light is
unavailable.

### 9.4 Implement Distance policy

Require near readings to persist for 500 ms, use separate near and clear
thresholds, and avoid repeated wake events from a stationary object.

Never infer identity, room occupancy, sleep, or attention.

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
- Optional buzzer output
- Optional Bluetooth audio

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

Extend `tests/test_gateway.py` or add focused test modules as the gateway
surface grows.

### 11.2 Firmware tests

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

### 11.3 Physical board verification

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

## Recommended implementation order

1. Shared multi-module contracts
2. MCU `SensorController` refactor
3. Light sensor
4. Distance sensor
5. Multi-module Bridge response
6. Python gateway decoding and health
7. Physical sensor calibration
8. `InputController`
9. Event retention and acknowledgement
10. `OutputController`
11. Output lease and safe state
12. `ContextEngine`
13. `NotificationService`
14. Peripheral and audio adapter
15. User interface
16. Full integration and acceptance testing

The critical dependency is reliable hardware contracts before application
behavior. This prevents ContextCue from making decisions based on unclear,
stale, invalid, or fabricated sensor data.
