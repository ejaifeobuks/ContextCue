# ContextCue implementation

The first hardware milestone implements **Modulino Thermo → UNO Q MCU → Arduino
Bridge → Linux HardwareGateway → console**. The runtime uses real hardware.

Start with the [board setup and verification guide](apps/contextcue_hardware/README.md).
The complete `apps/contextcue_hardware` folder is an Arduino App Lab application.

## Source map

| Location | Responsibility |
|---|---|
| `apps/contextcue_hardware/sketch/SensorController.h` | Thermo acquisition and validation through Arduino_Modulino on the MCU |
| `apps/contextcue_hardware/sketch/sketch.ino` | Firmware startup, boot identity and compact Bridge RPC handlers |
| `apps/contextcue_hardware/sketch/sketch.yaml` | Pinned UNO Q core and Arduino libraries |
| `apps/contextcue_hardware/python/contextcue/contracts.py` | Application-facing sensor, capabilities and health contracts |
| `apps/contextcue_hardware/python/contextcue/transport.py` | Official Arduino Router Bridge connection |
| `apps/contextcue_hardware/python/contextcue/gateway.py` | HardwareGateway, asynchronous I/O, time mapping and recovery |
| `apps/contextcue_hardware/python/contextcue/cli.py` | Console startup, polling and shutdown |
| `tests/test_gateway.py` | Contract and fault tests using a test-only RPC peer |
| `background_Files/` | Existing requirements and design reference documents |

## Local development

Python 3.10 or newer is required. Gateway tests use only the standard library:

```bash
python3 -m unittest discover -s tests -v
python3 apps/contextcue_hardware/python/main.py --help
```

VS Code's **Run Task** menu includes test and firmware compile tasks. The compile
task requires `arduino-cli` on PATH and downloads the pinned dependencies on its
first invocation. Neither task uploads firmware.

An optional editable install exposes `contextcue-hardware` as a console command:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --no-build-isolation -e .
contextcue-hardware --help
```

The console needs the real router and Python Bridge dependency on the board to
read hardware. Running it on an ordinary laptop reports unavailable hardware;
there is no automatic simulator or fabricated sensor fallback.

## Next implementation steps

1. Load this milestone and record actual readings, disconnect recovery and MCU
   reboot behavior using the board guide.
2. Extend SensorController with Light and Distance, including per-metric sample
   sequences, validity and supported sampling rates.
3. Implement OutputController with quiet initialization, bounded commands,
   duplicate rejection and the three-second output lease before enabling outputs.
4. Add InputController, retained control events and local buzzer cancellation.
5. Connect ContextEngine to the gateway, then add notice policy, persistence and UI.

LLM generation, Bluetooth speech, full simulation and acceptance testing remain
required by the software baseline. This milestone establishes the real sensor
boundary; it does not claim completion of the whole gateway or project.
