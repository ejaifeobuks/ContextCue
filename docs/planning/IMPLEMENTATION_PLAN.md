# ContextCue Implementation Plan

Version 2 | 6 November 2026 | Replaces the 25 October remaining-modules plan

## Purpose and authority

This plan tracks what is built, what remains, and how the remaining work will
reuse Arduino's existing UNO Q software before anything is written from
scratch. The [software baseline v1.1](../requirements/ContextCue_Software_Baseline_v1.md)
remains the authority for behavior, thresholds and acceptance; the
[conflict resolution review](CONFLICT_RESOLUTION_REVIEW.md) records the
decisions the baseline adopted.

The demonstration target is **16 November 2026**. Work is ordered so that the
items most likely to force a change of plan (on-board LLM, speech, Bluetooth)
are measured first.

The implementation boundary is unchanged:

```text
MCU modules
  -> Arduino Bridge protocol
  -> Python contracts and HardwareGateway
  -> ContextEngine and application policy
  -> UI, notifications, and audio
```

Work still proceeds in thin vertical slices: prove the device with the
official Arduino API, integrate it into the MCU contract, decode it on Linux
with failure/recovery tests, and only then let application policy consume it.

## Status at 6 November 2026

| Area | Status | Evidence |
|---|---|---|
| Thermo, Light, Distance acquisition (HW-01) | Done | Real readings on the board; independent RPCs `cc_read`, `cc_read_light`, `cc_read_distance`; 37 automated tests |
| Gateway decoding, freshness, reboot detection, health (HW-04, sensing part) | Done | `contextcue_hardware/gateway.py`; boot ID, sequence and staleness checks |
| Failure attribution and clean stop | Done | Bridge reasons `mcu_unresponsive` / `router_unavailable` / `gateway_stopped`; SIGTERM handled; verified by freezing the MCU over SWD |
| SSH deploy/run workflow | Done | `scripts/unoq.sh`, VS Code tasks |
| Pixels and Buzzer, output lease (HW-03, HW-07) | In progress | MCU `OutputController` and RPCs `cc_out`, `cc_lease`, `cc_read_outputs` (firmware 0.2.0) with host tests. On the board: Buzzer detected, tone and lease expiry verified; **Pixels not detected at `0x36`**. Gateway `execute`, `heartbeat`, `read_outputs` and output health done with 14 tests and verified on the board on 7 November (lease, tone accepted then completed, Pixels rejected as unavailable, stop on close) |
| Calibration and hardware manifest (HW-06) | Not started | Plan below; one sampling-rate observation recorded |
| On-board LLM (APP-06 to APP-08, G1) | Researched, not benchmarked | Findings below |
| Speech (TTS) | Researched | Arduino's TTS Brick does not support the UNO Q |
| Display/touch and Bluetooth (HW-02, HW-05) | Researched | Existing desktop stack identified; no speaker paired yet |
| SWE application (ContextEngine, policy, UI, persistence, simulator) | In progress (7 November) | `services/application` and `services/ui` implemented with fixture sources, factual fallback, simulated gateway and 73 tests; not yet run on the board. Model, TTS, live adapters and App Lab packaging remain |

## Platform findings (6 November 2026)

Measured on the team's board over SSH (hostname `Obuks`) and from the Brick
catalogue and Python sources installed on it. Versions: `arduino-app-cli`
0.14.1, Brick assets 0.13.2, Arduino Router 0.11.0.

### Board resources

| Resource | Measured |
|---|---|
| RAM | 3.6 GiB usable (4 GB variant); about 2.2 GiB available with the desktop and the sensing app running |
| Swap | 1.8 GiB |
| CPU | 4 cores |
| OS | Debian 13 (trixie), kernel 7.0.0 |
| Storage | 29.1 GiB eMMC: root `/` 9.8 GB with **2.5 GB free**, and `/home/arduino` 18 GB with **16 GB free** |

Docker images (`/var/lib/docker`) and App Lab's model folder
(`/var/lib/arduino-app-cli/models`) are on the root partition, so its 2.5 GB,
not the 16 GB under `/home/arduino`, limits model and container downloads.
Moving them to `/home` would need root access and is not supported by App Lab
as far as we know.

### LLM: Arduino `arduino:llm` Brick

**What exists**

- The `arduino:llm` Brick supports `unoq` and runs a **llama.cpp** service
  (`ghcr.io/arduino/app-bricks/llamacpp-runner:0.13.2`, already pulled on the
  board, 203 MB). It serves an OpenAI-compatible API on `127.0.0.1:9999/v1`.
- Service limits from its compose file: `mem_limit` 2500 MB, 4 threads,
  one loaded model (`LLAMA_ARG_MODELS_MAX=1`), reasoning off, context size
  `LLAMA_ARG_CTX_SIZE` (default 16384, configurable per app).
- Models catalogued for the UNO Q: **Qwen3.5-0.8B Q4_0** (default, 507 MB)
  and **Gemma 3 1B Q4_0** (722 MB). Larger models (Gemma 4, Qwen3 4B via
  Genie) are VENTUNO Q only. No model has been downloaded to this board yet;
  App Lab downloads it into `/var/lib/arduino-app-cli/models/llamacpp/`.
- Python API: `LargeLanguageModel(system_prompt, temperature=0.7,
  max_tokens=512, timeout=None, tools=None, model=None)` with `chat()`,
  `chat_stream()`, `stop_stream()`, `with_memory()` and `list_models()`,
  built on LangChain's OpenAI client.
- Reference implementation: `examples/inspirational/platform_unoq/edge-ai-assistant`
  (`web_ui` + `llm`), which streams tokens to the browser over WebSocket and
  stops generation with `stop_stream()`.

**Fit with the baseline**

| Baseline need | Brick support | Assessment |
|---|---|---|
| Actual on-board generation, no silent cloud fallback | Local llama.cpp | Fits. `cloud_llm` exists but may only be used as a disclosed contingency |
| One active request (§6) | Service loads one model; our worker serializes requests | Fits |
| Deadline and cancellation (APP-07) | `timeout` (whole seconds), `stop_stream()` | Fits with a thin async wrapper; must verify that cancellation frees the server promptly |
| <=160 output tokens, <=2048 input tokens | `max_tokens`; context size variable | Fits; set context to about 4096 instead of 16384 to save memory |
| Structured `ModelResponse` with evidence IDs (APP-08) | `chat()` returns free text | Gap. Either prompt for JSON and validate strictly through the Brick, or call the llama.cpp endpoint directly with JSON-schema constrained output (unverified on this runner) |
| No model hardware authority | `tools` parameter exists | Never pass `tools`; never enable memory (each request is an independent snapshot) |

**Assessment:** feasible to build on, **not yet proven**. Use the Brick to
provision and run the model (declare `arduino:llm` in `app.yaml`) and keep
ContextCue's own validator. Whether a 0.8B or 1B model can meet T03 (>=90%
acceptable grounded generations) and the latency budget is the main unknown.

**Benchmark before committing (G1)**

1. Add `arduino:llm` to a benchmark app and let App Lab download the default
   model (507 MB of the 2.5 GB free on the root partition).
2. Run about 30 representative ContextCue prompts (snapshot JSON in, short
   briefing out) at <=2048 input and <=160 output tokens: cold load time, warm
   p50/p95 latency, tokens per second, peak memory.
3. Measure the JSON/evidence-ID validity rate with prompting alone; then test
   the direct endpoint with a JSON schema.
4. Measure the effect on the sensing loop and UI responsiveness during
   generation (T02).
5. Repeat with Gemma 3 1B and with context 4096 versus 16384; record the
   chosen model, timeout and memory budget.

### Speech (TTS)

- Arduino's `arduino:tts` Brick (Piper English voice on the Genie/QNN audio
  service) lists **only `ventunoq`** as a supported board. The `asr` and
  `vlm` Bricks are VENTUNO Q only as well. They cannot be used on the UNO Q.
- No speech engine is installed on the board (no `espeak-ng` or Piper).
- Candidates: **espeak-ng** (very small and fast, robotic voice) and
  **Piper on the CPU** (more natural; voice files about 20-60 MB; speed on
  this CPU and licensing must be checked). Both produce a WAV file that the
  audio path below can play.
- Open: how to add a system package or binary to an App Lab app container
  (apps install Python requirements only); a pip-installable engine may be
  required.

### Display, touch and the existing GUI

- The "Arduino GUI" on the touchscreen is the standard Debian desktop:
  **LightDM, Xorg and XFCE 4** (panel, desktop, window manager), the
  `light-locker` screen locker, the NetworkManager applet and the **Blueman**
  Bluetooth applet, plus the **App Lab** desktop application (WebKitGTK).
  It is not an application framework that ContextCue can extend, but its
  components can be reused.
- Screen: **Waveshare 7-inch** with USB touch (`0eef:0005`, WS170120) at
  **1024x600 on DP-1**, connected through a StarTech USB-C multiport dock.
- There is **no `/sys/class/backlight` device** and the panel has no DDC/CI,
  but the panel is the **7inch HDMI LCD (C)** and its touch controller has a
  vendor HID collection (usage page `0xFF00`, report ID 4, 63-byte output
  report; see 8 November findings). Waveshare documents USB backlight
  control for Rev4.1 of this panel, and the published (B) protocol is
  `04 AA 01 00 00 00 <0-100>` written to that report. Until that is
  confirmed on this unit, the UI keeps the visual dimming overlay.
- Chromium is installed; `xinput` and a system on-screen keyboard are not.
  Text entry (city search) uses ContextCue's own on-screen keyboard.
- Arduino's **`web_ui` Brick** serves static HTML/JavaScript, REST endpoints
  and a Socket.IO WebSocket on port 7000, and declares
  `requires_display: webview`. Every reference app with a UI uses it.

**Assessment:** build the ContextCue UI as a `web_ui` app (the baseline's
React/TypeScript recommendation can be served as static assets) shown full
screen on the Waveshare, using App Lab's webview or Chromium in kiosk mode.
Screen blanking and `light-locker` must be disabled for kiosk use.

### Bluetooth and audio

- **BlueZ 5.82** is running; the controller is powered and pairable and
  advertises the **Audio Source** (A2DP source) profile, so it can stream to
  a speaker. **Blueman 2.4.4** provides the desktop pairing UI.
- **PipeWire 1.4.2 with WirePlumber and the Bluetooth plugin**
  (`libspa-0.2-bluetooth`) is running, so a connected speaker appears as a
  PipeWire output node. No device is paired yet.
- Built-in audio also offers line-out and headphone-jack outputs, a possible
  wired fallback.
- Arduino's `Speaker` peripheral (used by `sound_generator`; ALSA via
  `pyalsaaudio`) lists USB and jack outputs and also opens an explicit
  `pipewire:NODE=<node.name>` device, which should reach a Bluetooth node.

**Assessment:** reuse the existing stack instead of a new bring-up. Pair the
speaker once from Blueman (an explicit, user-triggered setup step), then have
the app play synthesized speech to the speaker's PipeWire node through the
`Speaker` peripheral. Readiness comes from the node's presence and BlueZ's
`Connected` state; neither proves that a person heard the audio.

### Other reusable Bricks

| Brick | What it gives ContextCue | Use |
|---|---|---|
| `weather_forecast` | Open-Meteo forecasts by city or coordinates, WMO codes mapped to simple categories | Basis for the post-demo `OpenMeteoWeatherAdapter`; check which timestamps it exposes for freshness |
| `dbstorage_sqlstore` | Thread-safe SQLite store | Storage under `DataRepository` |
| `web_ui` | Web server, REST and WebSocket | User interface |
| `cloud_llm` | OpenAI/Anthropic/Gemini clients | Disclosed off-board contingency only |
| `sound_generator` | Tones and melodies through `Speaker` | Optional chime; the Buzzer remains an MCU output |
| `tps` | Wi-Fi geolocation (cloud credentials) | Not used |

### Modulino Pixels and Buzzer (Arduino_Modulino 0.10.0)

- `ModulinoPixels`: 8 LEDs; `set(index, color, brightness 0-100)` maps
  brightness to 5 bits; `clear()`; `show()` writes all 8 LEDs. I2C address
  `0x36`.
- `ModulinoBuzzer`: `tone(frequency_hz, duration_ms)` and `noTone()`. The
  Buzzer module's own microcontroller times the tone. I2C address `0x1E`.
- `Module::begin()` caches the address after the first successful discovery,
  and `write()` ignores I2C errors. The library therefore cannot detect a
  disconnected output; ContextCue must probe the address itself, as it does
  for Distance.

## Findings and fixes (8 November 2026)

After live time and weather (M3/M4) went onto the board, five issues were
investigated. The proposal for issue 4 is in
[AI_EXPANSION_PLAN.md](AI_EXPANSION_PLAN.md).

1. **Demo calendar empty in the evening (fixed).** The fixtures were not
   overwritten (no diff in `fixtures/`). On the device clock they were moved
   onto today with their daytime local times (09:00-16:00), so after 16:00
   every event had ended and Glance showed none. The briefing still said
   "4 events today". Now a scenario **replays from the moment it is chosen**:
   its start time maps to the current minute (`demo_shift`). Choosing a
   scenario again restarts it, and switching a source does not. Limitation:
   events that would fall after local midnight count as tomorrow. "Demo Mode"
   is still only a presentation toggle (UI plan rule 8); the data sources are
   chosen under Calendar & Weather.
2. **Briefing at night (fixed).** The text that looked invented was the
   labelled *factual fallback*, shown after the model hit its 45 s deadline.
   Its facts were real but misleading: the whole-day count of demo events
   that had all ended, and "sunny" at 21:00. Fallback and model view now
   count only events still ahead ("Demo calendar: No more events today"),
   say "clear" at night (`night` flag on forecast evidence, same 18:00-06:00
   rule as the UI), and no longer show the model the current period's start
   time (the model quoted it as "the current time is 21:00").
   **Timeout:** the model reads about 7-11 tokens/s and writes 2-4 tokens/s
   on the CPU. Replies took 32-45 s because the JSON schema made the model
   echo the category and every evidence ID (about 25 extra output tokens).
   The schema now holds only `text`, and code fills in the category and the
   evidence shown. Numbers are still checked against that evidence.
   `max_tokens` dropped from 160 to 100. On the board, four requests in a
   row took 12-19 s, all generated, none timed out. CPU contention from the
   kiosk costs about 10-25% (measured by pausing Chromium).
3. **City entry needed a physical keyboard (fixed).** The on-screen keyboard
   only opened from a separate "Keyboard" button, and a settings re-render
   could clear the typed text (the input had no value binding). Tapping the
   field now opens the keyboard. The text survives re-renders. The keyboard
   has a Search key and `- ' . ,` keys, and results appear after a pause in
   typing. No system keyboard is needed: Chromium has none here, and an
   in-page keyboard also works in kiosk mode.
4. **AI role:** see [AI_EXPANSION_PLAN.md](AI_EXPANSION_PLAN.md).
5. **Hardware brightness (tested: not supported on this unit).** Details below.

6. **Live calendar (M5, implemented).** `calendar_ics.IcsCalendarAdapter`
   reads a subscription link with `icalendar` 7.3.0 and
   `recurring-ical-events` 3.8.2 (in the app's `requirements.txt`). It
   handles recurrence rules, EXDATE, moved occurrences, time zones, floating
   times, all-day and cancelled events, for today and tomorrow (local). It
   refreshes in the background every 10 minutes, is stale after 2 hours
   without a refresh, and re-expands from the cached file at midnight. The
   provider (Google, Microsoft 365 or other) is detected from the link's
   host. Connect it from Settings (pasted through the SSH tunnel) or with
   `scripts/contextcue-app.sh calendar-link`. On the board (8 November), a
   public Google calendar connected in 1.2-1.5 s and showed as "Google
   Calendar, real, valid"; a wrong link was refused with `http_404`.

### Display backlight

| Path | Result on this board |
|---|---|
| `/sys/class/backlight` | None (an HDMI panel behind a USB-C dock) |
| DDC/CI (`ddcutil`) | Not available. The (C) panel does not implement DDC/CI |
| `xrandr --brightness` | Gamma only: the same as the CSS overlay, and the backlight stays at full power |
| **USB HID report 4** | The descriptor has the vendor output report that Waveshare's brightness tools use. Tested 8 November: the panel (`/dev/hidraw3`) accepted the 64-byte reports for 20%, 10% and 50%, but the backlight did not change. This revision ignores the published (B) command |

**Decision:** keep the visual dimming overlay. The panel's backlight stays at
full power, so the overlay can only darken pixels. If a later panel revision
accepts the command, the integration is:

1. A udev rule, installed once:
   `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0eef", ATTRS{idProduct}=="0005", MODE="0660", GROUP="video"`.
2. A host-side helper, started by the same XDG autostart entry as the kiosk.
   The app container cannot open the device: hidraw has major number 509,
   which is not in App Lab's `device_cgroup_rules`. The app writes the target
   level (0-100) to `data/display.json`. The helper watches it and writes the
   HID report, sending only on a change and at most twice per second.
3. The existing `rules.brightness()` output (0.2-1.0) maps to the backlight
   level. The CSS overlay is kept only as a fallback when the helper is
   missing or reports an error. Health shows "Backlight: hardware" or
   "Backlight: visual dimming".
4. If the panel ignores the command (an older revision), the remaining
   hardware route is the PWM solder pads on the panel's driver board, driven
   from an MCU PWM pin. That needs soldering and is out of scope before demo
   day.

## Remaining work

### A. On-board LLM feasibility (G1) — first

Run the benchmark above before writing the RecommendationService. Record the
model, runtime, measured timeout and memory budget, and whether the on-board
goal is met. If it is not, select a smaller configuration or a disclosed
off-board contingency, as the baseline requires.

### B. Pixels and Buzzer (HW-03, HW-07)

1. **Bring-up gate.** Confirm both modules are detected at `0x36` and `0x1E`
   on the assembled Qwiic chain; confirm `show()` is required, `noTone()`
   stops a tone, and measure tone duration and Pixel update latency.
2. **MCU `OutputController`** (`sketch/src/outputs/`):
   - Quiet boot: Pixels cleared and `noTone()` in `setup()`.
   - Allowlist only: `pixels.set_pattern` (`ready`, `notice`, `fault`, `off`),
     `pixels.off`, `buzzer.short_tone`, `buzzer.stop`, `outputs.stop`.
   - MCU-enforced bounds: brightness 0-1, tone duration <=250 ms without
     looping, bounded frequency, remaining TTL > 0 (standard TTL 2 s),
     duplicate command IDs rejected within a session.
   - Output lease: `cc_lease(session)` renews it; after 3 s without renewal
     the MCU stops the Buzzer and turns Pixels off. A new session never
     replays old commands.
   - RPC callbacks only validate and queue commands; the main loop performs
     I2C writes. Results report `accepted`, `rejected`, `completed`,
     `failed` or `expired` with a reason; acceptance is not completion.
   - Disconnect detection by address probe; outputs report health like the
     sensors.
3. **Gateway:** `execute(command)` and `heartbeat(session_id)` with
   `DeviceCommand` and `CommandResult` contracts, strict validation on Linux
   as well as on the MCU, and capability reporting of patterns and limits.
4. **Tests:** firmware tests for bounds, TTL, duplicates and lease expiry;
   gateway fake-peer tests; physical lease-loss test (outputs off within 3 s).
5. **Bring-up finding (7 November):** a bus scan of `Wire1` found the Buzzer
   (`0x1E`), Distance (`0x29`), Thermo (`0x44`) and Light (`0x53`), but nothing
   from the Pixels at any address. With the Pixels as the only module on the
   bus, the firmware never finished `setup()`: the first I2C transfer blocked
   and the MCU idled in the Zephyr kernel. The Pixels module (or its
   connector) is faulty: it powers up but does not answer, and in a
   `Light -> Pixels -> Distance` chain the Distance sensor behind it also
   disappeared, so it no longer passes the bus through. Keep it off the bus
   (it can freeze every transfer) and replace it; no firmware or gateway
   change is needed for a working module.
6. **MCU halts in darkness (fixed in firmware 0.2.3, 8 November).** Root
   cause of the recurring `mcu_unresponsive` (every RPC timing out at once,
   41-140 s after start, also seen in the earlier mutex investigation):
   Arduino_LTR381RGB 1.0.1 computes `lux = ... * (1 - 0.033f*(ir/rawlux))`
   with integer `ir/rawlux`. In a dark room the raw ALS count reaches 0;
   Zephyr enables the Cortex-M33 divide-by-zero trap, so this raised a
   UsageFault and Zephyr halted the MCU (`arch_system_halt`), leaving the
   Bridge UART unserviced. A reset in the same darkness halted it again, so
   only a change of light (or luck) appeared to help. Found over SWD: IPSR 6,
   faulting `sdiv r3, r3, r1` with r1 = 0 in `LTR381RGBClass::getALS`.
   Fix: `sensors/DivideByZeroTrap` turns the trap off only around
   `ModulinoLight::update()` (Armv8-M then returns 0, so the library reports
   0 lux) and restores it; verified on the board for 5 minutes at 1 lux,
   with CCR showing the trap restored between reads. Host test fails without
   it. While the Light driver waits, the trap is also off for the other
   threads; a library fix upstream would remove that.
7. **Bounded I2C (firmware 0.2.1).** Found while chasing the halt, but not
   its cause: the STM32duino VL53L4CD/VL53L4ED drivers retry a register
   access with `while (endTransmission(false) != 0)` and no limit, so one
   NACKing Distance sensor would freeze `loop()`. `sensors/BusGuard` and
   `sensors/QwiicBus` (a `ZephyrI2C` on Wire1's device) stop that loop after
   3 failures (`distance_bus_error`) and pause sampling for 2 s when a
   transfer hits the 500 ms driver timeout (`i2c_bus_stalled`). Not yet
   seen on the board. Still open: GPIO bus recovery (SCL pulses on PD12) if a
   device is ever found holding SDA low.
8. **Gateway read rates.** `HardwareGateway` now reads each RPC at the rate
   the MCU samples it (Thermo 1 Hz, Light 5 Hz, Distance 10 Hz, outputs
   1 Hz), and the application renews the output lease once a second. This
   cuts Bridge traffic from about 40 to about 17 RPC/s at a 10 Hz poll; it
   was not the cause of the halts.

### C. Calibration and hardware manifest (HW-06)

1. **Manifest:** board variant, OS, core and library versions (pinned in
   `sketch/sketch.yaml`), module variants and I2C addresses, cable order and
   mounting positions. Wiring and mounting need the team's input.
2. **Sampling rates.** One 66-second run on 6 November reached Thermo
   about 1.0 Hz, Light about 4.8 Hz and **Distance about 5.8 Hz against its
   10 Hz target**. A likely cause is the Light driver's blocking waits in the
   shared loop. Measure over several minutes; if confirmed, restructure the
   loop or record a revised supported rate.
3. **Light:** dark (sensor covered) and bright (desk lamp) references in the
   mounted position; check lux plausibility against a reference meter if one
   is available.
4. **Distance:** readings against a ruler at about 200, 400, 500, 650, 800 and
   1000 mm in the mounted position; confirm the near (<=500 mm) and clear
   (>650 mm) thresholds; document the out-of-range behavior (currently
   reported as `distance_timeout`).
5. **Thermo:** compare with a reference thermometer/hygrometer for about
   10 minutes, then repeat with the LLM running to measure self-heating;
   record any offset in the manifest, not in policy code.
6. **Tooling:** a calibration command that collects samples for a set time
   through the real gateway and prints statistics and achieved rates, run on
   the board through `scripts/unoq.sh`.

### D. Display, touch and Bluetooth on the existing stack (HW-02, HW-05)

1. Pair the team's speaker from Blueman; confirm a PipeWire Bluetooth node
   appears and reconnects after a power cycle.
2. From an app container, play a WAV through `Speaker` to the
   `pipewire:NODE=...` Bluetooth node; measure start latency, stop behavior,
   and 20 playbacks (T06).
3. Choose the TTS engine (espeak-ng versus Piper) by speed, quality, size and
   licensing; settle how it is installed in the app container.
4. Show a `web_ui` page full screen on the Waveshare; confirm touch
   orientation; disable blanking and `light-locker`; implement dimming as an
   overlay.

### E. SWE foundations — unchanged

Phases 9 and 10 below are carried over from the previous plan without
change.

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

## Testing and acceptance gates

At each vertical slice run `git diff --check`, the test suite
(`python3 -m unittest discover -s tests -v`), the pinned Arduino CLI compile,
and a real-board check. A laptop-only pass is not board validation.

Do not advance a layer until its gate passes:

1. **Hardware gate:** the official Arduino example works on the exact
   assembled module, recorded with versions and wiring.
2. **Firmware gate:** acquisition and output work is non-blocking, bounded,
   sequenced, and recovers from disconnects.
3. **Protocol gate:** responses fit the measured limit and keep module
   failures independent.
4. **Gateway gate:** strict decoding, freshness, reboot, health and command
   outcomes pass automated tests.
5. **Output gate:** Pixels and Buzzer commands are bounded and lease-safe on
   real hardware.
6. **LLM gate:** the selected local model meets the recorded latency, memory,
   grounding and failure criteria on the UNO Q.
7. **Audio gate:** the selected speaker meets T06 through the app's playback
   path; stop cancels actual playback.
8. **Policy gate:** deterministic notification/audio scenarios pass with
   fixtures and simulated failures.
9. **Provider gate:** live adapters pass consent, expiry, outage, freshness
   and no-fallback tests; the demo still works offline.
10. **System gate:** two clean setup/recovery runs complete and all failures
    stay visible without unsafe or repeated output.

Every test asserts both the action and the observable reason or state. A
successful return is not proof that audio was heard or that a Pixel or the
Buzzer physically changed.

## Recommended order

1. LLM benchmark (A), started first because the model download and
   measurements take time.
2. Pixels and Buzzer (B).
3. Calibration and manifest (C), including the Distance rate check.
4. Speaker pairing, container playback and TTS choice (D).
5. Kiosk display shell (D).
6. SWE foundations (Phases 9 and 10).
7. Integration and acceptance testing.

## Unresolved questions

| Question | Blocks | How to resolve |
|---|---|---|
| Can a 0.8B/1B model meet T03 grounding and the latency budget? | APP-06 to APP-08 | Benchmark (A) |
| Does the llama.cpp runner accept JSON-schema constrained output? | Validator design | Resolved: yes. The schema now holds only `text` (8 November) |
| Does the UNO Q runner use the Hexagon DSP or only the CPU? | Latency expectations | Resolved: CPU only. `llamacpp-runner` 0.13.2 starts with `--device none`; there are no fastrpc device nodes |
| Does stopping a stream free the server immediately? | APP-07 cancellation | Test during (A) |
| Which TTS engine, and how is it installed in an app container? | APP-15 | Trial both engines (D) |
| Does an app container reach PipeWire and the Bluetooth node? | APP-15, T06 | Playback test (D) |
| Can an app container reach BlueZ over D-Bus for in-app pairing and status, or must pairing stay in Blueman? | APP-15 pair/select UI | Test (D) |
| How is speech volume set from the app (PipeWire node volume)? | Touch volume control | Test (D) |
| How does App Lab show a `web_ui` app on the board display, and can it start at boot in kiosk mode? | APP-02, APP-03 | Test (D) |
| ~~Does the panel accept the USB HID backlight command?~~ | Hardware brightness | Resolved 8 November: no, the backlight ignores it; the overlay stays |
| Does the Distance rate reach 10 Hz alongside Light? | Proximity timing | Measure (C) |
| Do all five Modulinos coexist on the Qwiic chain without address or power problems? | B, C | Bring-up gate (B) |
| Replacement Pixels module (the current one is faulty: no I2C response, no pass-through, hangs the bus alone) | Pixels output, HW-03, T02/T05 | Order and fit a replacement |
| Report the Arduino_LTR381RGB `ir/rawlux` divide-by-zero upstream? | Removes the trap workaround in 0.2.3 | Open an issue with the SWD evidence |
| Long-idle interval, unknown-Distance behavior, away-mode grace, return summary, important-repeat count | Notification policy | Product decision before Phase 10 |
| ~~Calendar connection method~~ | Live calendar | Resolved 8 November: subscription link (ICS) first, OAuth as stage 2 (CONFLICT_RESOLUTION_REVIEW 9a) |
| OAuth stage 2: Microsoft device-code flow, Google redirect flow, token storage | Fresher, revocable calendar access (post-demo) | Provider gate |

## Completed work record

Phases 0 to 5 of the 25 October plan are complete for the three sensors:

- **Contracts:** `contextcue_contracts` holds `SensorSample`,
  `HardwareCapabilities`, `HealthState` and `GatewayState`, with module IDs
  `thermo`, `light`, `distance` and quality values from the baseline.
- **MCU:** `SensorController` keeps independent state and schedules for each
  sensor (Thermo 1 Hz, Light 5 Hz, Distance 10 Hz target) with named
  failures. Hardware access stays in the main loop; RPC callbacks return
  cached state.
- **Protocol:** separate compact RPCs per sensor (`cc_open`, `cc_read`,
  `cc_read_light`, `cc_read_distance`) within the 224-byte limit, instead of
  one combined response.
- **Gateway:** strict per-module decoding, sequence/uptime/boot checks,
  freshness limits (Thermo 5 s, Light and Distance 2 s, Bridge 3 s) and
  separate health for Bridge, Thermo, Light and Distance.
- **Deviations from the old plan:** Light is reported as `ambient_light` in
  lux from the library's `getAL()` (the old plan proposed `ambient_level`);
  lux accuracy is still to be checked during calibration. Distance's
  `begin()` runs only at boot and after a disconnect, because the library
  allocates new driver objects on every call.
- **Diagnostics (6 November):** Bridge failures name the failing side and
  a requested stop ends with a `stopped` record; see the hardware README.
