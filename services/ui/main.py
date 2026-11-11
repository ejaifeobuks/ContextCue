"""Run the ContextCue application backend and kiosk UI.

  python3 services/ui/main.py --simulated          laptop: simulated gateway
  python3 services/ui/main.py                      UNO Q: real HardwareGateway

Then open http://127.0.0.1:7000 (on the board: Chromium in kiosk mode).
The device clock is used unless --scenario-clock is given; fixture calendar
and weather data then move onto today's date.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "apps/contextcue-hardware/python", ROOT / "services/application",
              ROOT / "services/ui"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from contextcue_app import ContextCueApp  # noqa: E402
from contextcue_app.clock import ScenarioClock, SystemClock  # noqa: E402
from contextcue_app.recommendation import LlamaCppModel  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.sources import load_scenarios  # noqa: E402
from contextcue_ui import UIServer  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    scenarios = load_scenarios()
    if args.scenario not in scenarios:
        print(f"Unknown scenario {args.scenario}; choose from {', '.join(scenarios)}", file=sys.stderr)
        return 2
    # Device clock by default: the fictional calendar and forecast move onto
    # today's date. --scenario-clock keeps the dated demo times and time jumps.
    clock = ScenarioClock(scenarios[args.scenario].start) if args.scenario_clock else SystemClock()
    if args.simulated:
        from contextcue_app.dev_gateway import SimulatedGateway
        gateway = SimulatedGateway(monotonic=clock.monotonic, utcnow=SystemClock().now)
        values = scenarios[args.scenario].sensors
        for metric, key in (("temperature", "temperature"), ("humidity", "humidity"),
                            ("ambient_light", "lux"), ("distance", "distance_mm")):
            if key in values:
                gateway.set_value(metric, values[key])
        mode = "simulated"
    else:
        from contextcue_hardware.gateway import HardwareGateway
        from contextcue_hardware.transport import ArduinoBridgeTransport
        gateway = HardwareGateway(ArduinoBridgeTransport(args.router))
        mode = "hardware"
    model = LlamaCppModel(args.model_url, args.model_name) if args.model_url else None
    repo = DataRepository(args.db)
    app = ContextCueApp(gateway=gateway, clock=clock, repository=repo, gateway_mode=mode,
                        scenario_id=args.scenario, model=model, scenarios=scenarios)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await app.start()
    server = UIServer(app, loop, args.host, args.port)
    server.start()
    host, port = server.address
    print(f"ContextCue UI on http://{host}:{port} ({mode} gateway, scenario {args.scenario})",
          flush=True)
    try:
        await stop.wait()
    finally:
        await asyncio.to_thread(server.stop)
        await app.stop()
        close = getattr(gateway, "close", None)
        if close:
            await close()
        repo.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ContextCue application and kiosk UI")
    parser.add_argument("--simulated", action="store_true",
                        help="Use the labeled simulated gateway instead of real hardware")
    parser.add_argument("--scenario", default="morning", help="Fixture scenario to start with")
    parser.add_argument("--scenario-clock", action="store_true",
                        help="Run on the labeled scenario clock (dated demo times, time jumps) "
                             "instead of the device clock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--db", default=str(Path.home() / ".contextcue" / "contextcue.db"),
                        help="SQLite file (default ~/.contextcue/contextcue.db)")
    parser.add_argument("--router", help="Override the Arduino Router socket (hardware mode)")
    parser.add_argument("--model-url", help="OpenAI-compatible endpoint, e.g. http://127.0.0.1:9999/v1")
    parser.add_argument("--model-name", help="Model name for the endpoint, if it needs one")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
