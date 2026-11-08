# UI service

**Status (7 November 2026): implemented and tested on a laptop against the
simulated gateway, redesigned to two tabs the same day; not yet shown on the
Waveshare panel.**

## Run it

```bash
# Laptop, no board needed (simulated gateway, labeled everywhere):
python3 services/ui/main.py --simulated --scenario morning
# open http://127.0.0.1:7000

# UNO Q Linux side, real HardwareGateway (needs the router and Bridge):
python3 services/ui/main.py --host 0.0.0.0
chromium --kiosk http://127.0.0.1:7000

# Options: --scenario morning|event_rain|comfort|quiet_hours, --port,
#          --db PATH (default ~/.contextcue/contextcue.db),
#          --model-url http://127.0.0.1:9999/v1 (arduino:llm endpoint)
```

Only the Python standard library is used (3.10+). `zoneinfo` needs the
`tzdata` package on Windows; WSL and the board already have zone data.

## What is implemented

- `main.py` starts `ContextCueApp` (services/application) on one asyncio loop
  and the HTTP server in a thread; every UI call is scheduled onto the app
  loop, so the UI never blocks sensing or generation (APP-07).
- `contextcue_ui/server.py`: static `assets/` plus `GET /api/state` (view
  model) and `POST /api/action` (`{"action", "payload"}`). Errors come back as
  400 with a readable message; path traversal is refused.
- `assets/`: plain HTML/CSS/JS, no build step, laid out for 1024x600 with
  touch targets of at least 48 px. The page polls every 500 ms and replaces a
  region's markup only when it changed.

### Layout (redesign of 7 November 2026)

Two tabs only, **Glance** and **Settings**, following
`ContextCue_UI_Implementation_Plan.md`. The former Ask, Why, Audio, Health and
Simulation tabs became contextual parts of these two.

| Where | Content |
|---|---|
| Header (always) | Sensor source badge (Real sensors / Simulated sensors), Demo Mode badge, Focus and Mute buttons, device-status button (opens Device Status), clock labeled *Scenario time* or *Device time* |
| Glance | Greeting and date on a time-of-day sky; **Ask** (sheet with the four bounded questions) and **Brief me**. Hero: the one highest-priority notice (category, headline from its evidence, text, origin and source labels, Why this?, Play, Done, "N more" sheet) or, with no notice, the next event. Today strip, Room comfort, Weather |
| Why this? | Expands inside the notice: one plain-language line per evidence item with source, real/simulated and freshness, how the text was written (AI or factual fallback), and technical details on request |
| Demo Mode | Display-only toggle in Developer & Demo, remembered in this browser only. Adds the live sensor panel (temperature, humidity, light, distance, comfort, brightness response, proximity) and changes no data |
| Settings | General (units, time zone, brightness, clear data) · Calendar & Weather (sources, status, forecast location) · Sound & Notifications (speaker paired/connected/ready, mute, volume, policy, Buzzer, away, stop, quiet hours) · Focus & Comfort · Device Status (problems first, friendly hints, raw reasons under technical details) · Developer & Demo (Demo Mode, sensor source, scenarios, scenario clock, simulated sliders and faults only with `--simulated`, diagnostics log) |

Every state is written as text (Simulated, Stale, Unavailable, Factual
fallback, Outside your range · checking ...), so nothing depends on color or
icons alone. Dimming is a CSS overlay because the panel has no Linux backlight
control.

Presentation rules (weather icon map, health labels, comfort wording, notice
headlines, Why lines) are pure functions in `assets/ui-logic.js`. Weather
icons come only from `forecast.condition` (`sunny`, `cloudy`, `rainy`,
`snowy`, `foggy`); missing, stale or unknown forecasts get a neutral
icon and say why.

For screenshots or a fixed kiosk start, the page reads its initial view from
the URL: `?tab=settings&section=developer`, `?demo=1`, `?why=1`,
`?details=1`, `?sheet=ask`. These set display state only; a `demo=` value
given here is not saved.

### Assets

| File | What |
|---|---|
| `assets/icons/sprite.svg` | 48 Lucide icons (lucide-static 0.460.0, ISC; `assets/icons/LICENSE`), as `<symbol>`s used with `<use href="icons/sprite.svg#name">` |
| `assets/scene.svg` | Header hills (original); sky and sun are CSS |

Nothing is loaded from the network: no CDN, web fonts or remote images.

### Tests

- `tests/ui/test_server.py`: API, actions, errors, static files and content types.
- `tests/ui/test_ui_assets.py`: no network resources, every referenced icon
  is in the sprite, the weather map matches the contract conditions, exactly
  two tabs, and every action the page sends exists on `ContextCueApp`.
- `tests/ui/test_ui_logic.test.js`: the presentation rules (`node --test
  tests/ui/test_ui_logic.test.js`); run from `test_ui_assets.py` when Node.js is installed.

### Backend dependencies found during the redesign (not built)

- **Forecast location:** the UI shows the source's location; changing it
  needs a location-capable weather adapter, settings field and action.
- **Sensor source switch:** chosen at launch (`--simulated`); a runtime
  switch needs a gateway hot-swap design.
- **Demo Mode persistence:** browser-only; storing it in app settings needs a
  settings field.
- **Notice headline for answers:** the notice does not carry the question it
  answers, so the hero says "Your answer"; adding the question to the notice
  would let it show the question.
- **Volume:** saved but not applied until a speech adapter exists (the
  backend reports `applied: false`).

## Still open

- **App Lab packaging.** App Lab apps must be self-contained. To ship this
  as an app: create `apps/contextcue/` with `app.yaml`, copy or package
  `contextcue_app`, `contextcue_ui` and `contextcue_contracts`, and point
  `python/main.py` at `services/ui/main.py`'s `main()`. Declaring
  `arduino:web_ui` is optional: this server already serves port 7000, but
  App Lab's webview display needs the Brick, so check that on the board.
- Board checks from plan section D: full screen, touch orientation, kiosk
  autostart, screen blanking off.

---

*Original planning notes below.*


Owns **UserInterface** (SWE): the kiosk touch UI with Glance/Dashboard,
Briefing/Alert, Why, Settings/Focus, Audio, Health and Simulation views
(baseline APP-02, APP-03, APP-09).

- Sends questions and settings to the application service (ContextEngine).
- Sends acknowledge, mute and Focus actions to NotificationService; receives
  notices and status from it.
- Holds only view state (`currentView`, `selectedNotice`); it is not a
  notification authority and never talks to hardware directly.
- Uses the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

## Existing solution: Arduino `arduino:web_ui` Brick

Every Arduino reference app with a UI uses this Brick (installed on the board).

```python
from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App

ui = WebUI()                                   # serves /app/assets on port 7000
ui.expose_api("GET", "/health", lambda: {...})  # REST endpoint
ui.on_connect(lambda sid: ...)                 # Socket.IO connection
ui.on_message("ack", handler)                  # messages from the browser
ui.send_message("notice", payload)             # push to the browser
App.run()                                      # starts the server; blocks
```

- Static files come from the app's `assets/` folder; the baseline's
  React/TypeScript recommendation can be built into it.
- `send_message()` only works while the server runs (call it from callbacks
  or another loop, not before `App.run()`).
- The Brick declares `requires_display: webview`: App Lab shows the page on the
  board's display. Chromium is also installed for a kiosk window.

## References

- `/var/lib/arduino-app-cli/examples/inspirational/platform_unoq/edge-ai-assistant`
  on the board: `web_ui` + `llm`, streams model tokens over Socket.IO, with a
  Stop button (`assets/app.js`, `assets/libs/socket.io.min.js`, `assets/libs/arduino.js`)
- `.../common/home-climate-monitoring-and-storage` (`web_ui` + time-series
  storage) and `.../common/blink-with-ui` (minimal UI-to-board control)
- On the board: `arduino-app-cli brick details arduino:web_ui`

## Target display (measured 6 November 2026)

- Waveshare 7-inch, **1024x600 landscape**, USB touch.
- No Linux brightness control: implement dimming as a visual overlay
  (baseline §6: 20-100% screen, 60% on Light failure, never black out).
- No on-screen keyboard is installed; typed context questions need an
  in-page keyboard or a reduced set of question buttons.
- `light-locker` and screen blanking are active by default; disable them for
  kiosk use (peripherals service).

## Implementation considerations

- At most one highest-priority notice on Glance; indoor/outdoor, real/simulated
  and stale/unavailable must look different, and never by color alone
  (APP-02, APP-03).
- Why shows inputs, units, source IDs, times, freshness, simulation and
  generation status (APP-09). Model rationale is not evidence.
- Screen changes within 2 s of a validated trigger (T02), independent of LLM
  generation; show generating/fallback states explicitly.
- Health view: show the gateway's reasons as they are (`mcu_unresponsive`,
  `router_unavailable`, sensor errors such as `distance_timeout`) and treat
  `stopped` as a requested shutdown.
- Touch targets sized for a 7-inch screen; acknowledge, mute, Focus and volume
  (0-100%) are touch actions with visible results.

## Recommended starting point

1. Copy the `edge-ai-assistant` structure: `web_ui` app with a Socket.IO
   channel and static assets.
2. Build the Glance view against recorded `GatewayState` JSON (from
   `scripts/unoq.sh logs`) or the simulator, before the application service
   exists.
3. Confirm full-screen display and touch on the Waveshare (peripherals service,
   plan section D).

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md).
