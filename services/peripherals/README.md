# Peripherals service — planned, not implemented

Owns **PeripheralAdapter** (hardware software, Linux): display and USB touch
initialization, brightness capability, and Bluetooth speaker discovery,
pairing, readiness, playback and stop (baseline HW-02, HW-05).

- Used by `HardwareGateway` (see [apps/contextcue-hardware](../../apps/contextcue-hardware/)); it does not
  interpret model advice or queue notice retries.
- Runs only on the UNO Q Linux side; it has no MCU firmware.

**Approach:** build on the board's existing desktop stack (the "Arduino GUI"
is standard Debian XFCE with Blueman and PipeWire) rather than a new bring-up.

## What the board already has (measured 6 November 2026)

**Display and touch**

| Item | Finding |
|---|---|
| Screen | Waveshare 7-inch, **1024x600 on DP-1**, through a StarTech USB-C multiport dock |
| Touch | USB `0eef:0005`, input device "WaveShare WS170120" |
| Desktop | LightDM, Xorg (`:0`), XFCE 4; `light-locker` screen locker; Chromium installed |
| Brightness | **No `/sys/class/backlight` device**: report `set_display_level` as unsupported and let the UI dim with an overlay (baseline §5) |
| Missing | `xinput`, on-screen keyboard |

**Bluetooth and audio**

| Item | Finding |
|---|---|
| Bluetooth stack | BlueZ 5.82; controller powered and pairable; advertises **Audio Source (A2DP source)** |
| Pairing UI | Blueman 2.4.4 (tray applet on the desktop) |
| Audio server | PipeWire 1.4.2, WirePlumber, `pipewire-pulse`, Bluetooth plugin `libspa-0.2-bluetooth` |
| Outputs today | Built-in line-out and headphone jack, headset microphone; no Bluetooth device paired yet |
| Container playback | Arduino `Speaker` peripheral (`arduino.app_peripherals.speaker`, ALSA via `pyalsaaudio`) lists USB/jack outputs and also opens an explicit `pipewire:NODE=<node.name>` device |

Useful commands on the board:

```bash
bluetoothctl show; bluetoothctl devices Paired
XDG_RUNTIME_DIR=/run/user/1000 wpctl status      # PipeWire sinks, incl. bluez_output.*
DISPLAY=:0 xrandr --query                        # display mode
```

## Implementation considerations

- **Pairing:** pair once from Blueman as an explicit, user-triggered setup
  step. In-app pairing/selection (APP-15) needs BlueZ over D-Bus from inside
  the app container, which is unverified; keep Blueman as the fallback.
- **Playback:** play synthesized WAV through `Speaker` to the speaker's
  `pipewire:NODE=bluez_output...` node. `audio_stop` must cancel real playback,
  not just a queue entry.
- **Readiness:** paired, connected (BlueZ `Connected`) and audio-ready (PipeWire
  node present and opened) are separate states. None proves a person heard it.
- **Wired fallback:** the built-in headphone/line-out jack is available if
  Bluetooth fails, but the baseline requires a Bluetooth endpoint for T06.
- **Speech engine:** Arduino's `arduino:tts` Brick is VENTUNO Q only; the TTS
  choice (espeak-ng versus Piper) is open (plan section D).
- **Kiosk:** disable screen blanking and `light-locker`; show the UI through
  App Lab's webview or Chromium in kiosk mode.

## Recommended starting point

Plan section D, in order:

1. Pair the team's speaker in Blueman; confirm a `bluez_output.*` PipeWire node
   appears and returns after a power cycle.
2. From an app container, play a WAV to that node with `Speaker`; measure
   start latency and stop, then 20 playbacks (T06).
3. Test whether the container can reach BlueZ (D-Bus) and set node volume
   (needed for touch volume control).
4. Show a `web_ui` page full screen; confirm touch orientation; disable
   blanking.

Open questions: container access to PipeWire and BlueZ, how speech volume is
set from the app, App Lab kiosk/autostart behaviour, and whether the Waveshare
panel offers any native brightness control (DDC/CI or its own buttons).

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md) section D and
its "Display" and "Bluetooth and audio" findings.
