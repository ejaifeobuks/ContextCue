# UI service — planned, not implemented

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
