# ContextCue hardware bring-up

This application reads real Modulino Thermo, Light, and Distance modules through
the UNO Q's Qwiic connector, serves the latest readings over Arduino Bridge, and
prints typed, labeled sensor state on Linux. It implements the
SensorController/HardwareGateway sensing slice.

## Hardware and software

- Arduino UNO Q with a working Linux image and Arduino App Lab connection.
- Modulino Thermo, Light, and Distance connected by Qwiic to the UNO Q. Connect
  cables with power off. Any module may be absent; it is then reported unavailable.
- Arduino_Modulino 0.10.0 detects the supported HS300x (`0x44`) or SHT31 (`0x45`)
  Thermo variant. The sketch does not select or access those drivers directly.
- Use `Wire1` for the Qwiic bus. The UNO-style header bus is a different interface.
- Python 3.10+; `arduino-router-bridge==0.5.1` is listed in
  `python/requirements.txt` for the App Lab runtime.
- Core/libraries are pinned in `sketch/sketch.yaml`: Arduino Zephyr core 1.0.0,
  RouterBridge 0.4.4, RPClite 0.3.1 and Arduino_Modulino 0.10.0. The Modulino
  library's dependencies, including HS300x 1.0.1 and SHT31 0.5.3, are pinned
  there for reproducible compilation; the sketch calls only the Modulino API.
  `Config.h` reports these build pins, not runtime measurements of the board's OS.

## Code layout

| Path | Contents |
|---|---|
| `sketch/sketch.ino` | `setup()`/`loop()` only |
| `sketch/src/sensors/` | `SensorReadings.h` records; `SensorController.h` interface and `.cpp` implementation |
| `sketch/src/state/` | Mutex-protected `PublishedState` snapshot and boot identity |
| `sketch/src/rpc/` | `cc_open`/`cc_read*` Bridge handlers and JSON serialization |
| `sketch/src/Config.h`, `Uptime.*` | Firmware constants and the Zephyr uptime clock |
| `python/contextcue_hardware/` | `HardwareGateway`, Bridge transport and console CLI |
| `python/contextcue_contracts/` | Application-facing contracts and freshness limits (baseline §5) |

The folder follows the [App Lab app specification](https://github.com/arduino/arduino-app-cli/blob/main/docs/app-specification.md)
and is self-contained, like the official
[app-bricks-examples](https://github.com/arduino/app-bricks-examples). `sketch/src/`
is permitted by the Arduino sketch specification and is compiled recursively.

## Load with Arduino App Lab

1. Connect the UNO Q to App Lab and create a new app named **ContextCue Hardware**.
2. Open that app's folder. Copy the contents of this `apps/contextcue-hardware`
   folder into it, preserving both Python packages
   (`python/contextcue_hardware` and `python/contextcue_contracts`) and the
   whole `sketch/src/` tree. The resulting app root must directly contain
   `app.yaml`, `python/` and `sketch/`. Copying just `main.py` and
   `sketch.ino` is insufficient. Do not add `data/` or `.cache/` folders here;
   App Lab reserves those names.
3. Run the app. App Lab builds/uploads the MCU sketch, installs the Python
   dependency and launches `python/main.py` on the UNO Q's Linux processor.
4. Open the **Main / Python** console. It prints JSON Lines with four sensor
   samples (`temperature` in `degC`, `humidity` in `%RH`, `ambient_light` in
   `lux`, `distance` in `mm`), plus separate Bridge, Thermo, Light, and Distance
   health. Values depend on the actual hardware.

The MCU attempts Thermo acquisition at 1 Hz, Light acquisition at 5 Hz, and
Distance acquisition at 10 Hz. The sensor controller keeps independent Thermo,
Light, and Distance reading state. Distance uses the pinned Modulino
`begin()`, `available()`, and `get()` API and reports millimeters; a scheduled
poll without a fresh measurement is reported as `distance_timeout` rather than
reusing an older value.
The Light driver uses the pinned Modulino `begin()`, `update()`, and `getAL()`
API and stores verified ambient values as lux. Linux exposes separate compact
RPC responses for Thermo, Light, and Distance. A valid zero remains zero. A
failed initialization, non-finite reading, or out-of-range reading has null
values and a reason. A connected Bridge can coexist with a failed sensor.

The app can start without Thermo: expect `thermo_not_found`, null values and
unavailable sensor health. It retries discovery on the next acquisition interval.

## Compile from VS Code or the terminal

Install the official Arduino CLI. From the repository root:

```bash
arduino-cli core update-index
arduino-cli compile --profile unoq apps/contextcue-hardware/sketch
```

The profile downloads the exact board and library dependencies into Arduino CLI's
managed storage. Arduino_Modulino includes headers for other modules, so its
transitive libraries are declared for reproducible compilation.
Compilation does not upload anything. App Lab is the recommended way to deploy
the paired MCU and Linux parts together; uploading only the sketch does not start
the Python gateway.

If the board's existing image requires another core release, record its version
and deliberately update the profile and corresponding Config.h build label,
then recompile and repeat the board checks. The board's installed OS/core pairing
has not been inspected from this workspace.

## Run manually on the UNO Q Linux side

If deploying outside App Lab, first load the sketch, copy the project to the board
and ensure its Arduino Router service is running. Create a virtual environment
and install the board extra from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e '.[board]'
contextcue-hardware --capabilities
contextcue-hardware --once
contextcue-hardware
```

Alternatively, after installing `python/requirements.txt` in a board environment:

```bash
python3 apps/contextcue-hardware/python/main.py --once
```

`--once` exits 0 only when the Bridge and every sensor (Thermo, Light, and
Distance) are ready; any missing or failed module makes it exit 1. `--capabilities`
exits 0 when the capabilities exchange succeeds; inspect `available_modules` to
check sensor availability. Ctrl+C stops polling and disconnects the Linux client.

The transport honors App Lab's `APP_SOCKET` when present; otherwise it uses
`unix:///var/run/arduino-router.sock`. `--router` overrides that address. Do not
open `/dev/ttyHS1` or `Serial1` directly; the Arduino Router owns the MCU link.

## Protocol and recovery

The application-facing objects use schema `1.0` from the software baseline.
Bridge transfers compact JSON strings, at most 224 bytes each, to leave room for
MessagePack framing within the MCU transport limit. The sensor RPCs are kept
separate so each module retains independent sequence, timing, quality, and
error state. These ContextCue RPC names are implemented by this sketch, not
built-in Arduino functions:

| RPC | Arguments | Response |
|---|---|---|
| `cc_open` | Proposed 32-character lowercase hexadecimal boot token | Schema, assigned boot ID, firmware/build versions, library identity, and Thermo (`ok`), `light`, and `distance` availability |
| `cc_read` | None | Schema, boot ID, sequence, current/observation uptime, validity, temperature, humidity, library identity and error |
| `cc_read_light` | None | Schema, boot ID, sequence, current/observation uptime, validity, lux, and error |
| `cc_read_distance` | None | Schema, boot ID, sequence, current/observation uptime, validity, millimeters, and error |

The compact read keys map as follows:

| Wire key | Meaning |
|---|---|
| `v`, `boot`, `seq` | Schema version, MCU boot epoch and acquisition sequence |
| `up`, `at` | Current and observation uptime in milliseconds |
| `q`, `err` | Validity and acquisition error; failure values are JSON null |
| `t`, `h`, `d` | Temperature, humidity and `modulino` library identity (`none` if unavailable) |

The Linux gateway expands these into SensorSample objects with source mode
`real`, stable event IDs, observation/receipt times and clock quality. Each
acquisition attempt gets a new sequence, including failures. Temperature/humidity
are one acquisition group. The group timestamp is conservatively taken before
the library calls start.

The MCU's SensorController follows the library's Thermo example: it includes
`<Arduino_Modulino.h>`, calls `Modulino.begin(Wire1)` and
`ModulinoThermo::begin()`, then calls `getTemperature()` and `getHumidity()`.
It repeats `begin()` before each acquisition to check that the module still
responds. The library handles variant detection and underlying I2C operations.
Its public Thermo API does not report which physical variant was selected or
whether an internal SHT31 read succeeded. That variant can return a cached value
after an internal read failure while the module still responds to initialization;
this milestone cannot certify every SHT31 sample as freshly acquired. Verify
freshness and failure behavior with the actual module before relying on comfort
triggers. [Library source](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/src/Modulino.h)

The MCU adopts a UUID supplied at its first handshake and retains it in RAM.
Reconnects preserve that boot epoch; a firmware restart clears it and the next
handshake assigns a new UUID. The Linux gateway discards old-boot state before
accepting new samples. The token is an identity marker, not authentication.

Observation UTC is estimated from MCU observation age and the RPC midpoint;
`clock_uncertainty_ms` reports half the round-trip time. This is not a precision
clock synchronization guarantee. Monotonic Linux time determines freshness, so
wall-clock changes cannot refresh a sample. Duplicate polls retain the original
sample timestamps. MCU uptime uses Zephyr's thread-safe 64-bit kernel clock.

Thermo data expires after five seconds, and Light and Distance data after two
seconds, even if the Bridge still answers. Health also expires after three
seconds without a successful state poll. A failed RPC
immediately makes exposed cached values unavailable. The next poll rediscovers
the peer; it never substitutes simulated values. Every RPC has a 750 ms deadline
and executes in a worker thread so it does not block the application's event loop.

MCU callbacks use `Bridge.provide_safe()`, but the pinned RouterBridge/RPClite
versions can also dispatch them on the background RPC worker. Only setup/loop
accesses SensorController. After acquisition finishes, the sketch publishes the
complete reading and driver identity under a mutex. Callbacks copy that snapshot
and the boot token under the same mutex, then serialize outside the lock. Polling
during acquisition returns the previous completed sequence with its original
timestamp; a completed failure publishes a new sequence with null values and a
reason. No lock is held during I2C acquisition. Serialization also rejects
non-finite values instead of emitting invalid JSON `nan` or `inf` tokens.
Driver-level I2C blocking on a faulted bus still needs measurement on the physical
board; the Linux RPC deadline does not reset a hung MCU or guarantee bus recovery.

## Board verification to record

1. Start with Thermo attached. Verify plausible temperature/humidity, `real`
   labeling, increasing sequences and both health states ready.
2. Start with Thermo absent. Confirm null values and a named sensor fault while
   Bridge health remains ready. Power down before changing connections, then
   reconnect and start again. A live disconnect/reconnect test can be performed
   separately if your hardware setup supports it.
3. Restart the MCU while leaving the gateway running. Verify a new boot ID and
   recovery to fresh readings. A full App Lab restart also restarts Python; test
   these separately if using manual deployment.
4. Interrupt the Bridge/router connection. Verify unavailable Bridge health and
   null exposed values, followed by rediscovery after communication is restored.
5. Record the board RAM variant, OS/core versions, sensor variant, actual sampling
   interval, observed values, reset/reconnect results and any acquisition delays.

Local tests exercise malformed data, stale observations, duplicate/changed
sequences, version mismatches, transport loss, reboot identity and recovery. They
do not validate physical measurements, I2C electrical behavior or audible output.
The firmware regression tests require a host `g++` with C++17 support and compile
the actual sketch with stubbed hardware. Run them from the repository root:

```bash
python3 -m unittest discover -s tests -p test_firmware.py -v
```

They poll from another thread during sensor acquisition, verify completed
failures/recovery and boot identity, reject non-finite JSON values, and stress
concurrent publication across the 32-bit uptime boundary.

## Scope of this milestone

Thermo, Light, and Distance are configured and independently advertised through
the current gateway protocol. The gateway polls each module separately and
preserves valid modules when another module is unavailable, malformed, stale,
or disconnected. Pixels,
Buzzer, display/touch, Bluetooth, output leases, control-event retention,
ContextEngine, persistence, notices, model generation and the UI are subsequent
implementation work. There are no output commands or audio actions in this app.
The gateway does not yet satisfy the baseline's complete INT-01/INT-02 or HW-01
through HW-07 requirements. Test-only peers are confined to `tests/hardware/`.

## API references

- [UNO Q manual: Bridge and Qwiic interfaces](https://docs.arduino.cc/tutorials/uno-q/user-manual/)
- [Arduino RouterBridge and safe callbacks](https://github.com/arduino-libraries/Arduino_RouterBridge)
- [Official Python Router Bridge client](https://github.com/arduino/arduino-router-bridge-py)
- [Arduino App folder specification](https://github.com/arduino/arduino-app-cli/blob/main/docs/app-specification.md)
- [Arduino_Modulino public header and Thermo implementation](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/src/Modulino.h)
- [Official Thermo basic example](https://github.com/arduino-libraries/Arduino_Modulino/blob/main/examples/Modulino_Thermo/Thermo_Basic/Thermo_Basic.ino)
