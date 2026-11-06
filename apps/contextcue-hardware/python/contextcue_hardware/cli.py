"""Console bring-up on UNO Q Linux. stdout is JSON Lines, diagnostics stderr."""

import argparse
import asyncio
import json
import logging
import math
import signal
import sys
from dataclasses import asdict

from .gateway import HardwareGateway
from .transport import ArduinoBridgeTransport


async def _run(args: argparse.Namespace) -> int:
    gateway = HardwareGateway(ArduinoBridgeTransport(args.router))
    async with gateway:
        if args.capabilities:
            try:
                result = await gateway.get_capabilities()
            except Exception as exc:
                print(f"Hardware discovery failed: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(asdict(result), allow_nan=False), flush=True)
            return 0
        # App Lab / arduino-app-cli stop the app with SIGTERM. Record that as a
        # requested stop so it is never mistaken for an MCU or router failure.
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        stop_signal: list[str] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            def request_stop(sig=sig):
                stop_signal.append(sig.name)
                stop.set()
            loop.add_signal_handler(sig, request_stop)
        last_signature = None
        while not stop.is_set():
            started = loop.time()
            state = await gateway.read_state()
            if stop.is_set():
                # A stop tears the MCU side down first, so a poll in flight
                # when it arrives fails because of the stop; report the stop.
                break
            # Emit new samples and health changes without printing each 100 ms poll.
            # Heartbeat timestamps advance on every successful poll; they do not
            # constitute a change in the user-visible health state.
            signature = (
                state.boot_id, state.samples,
                tuple((h.component_id, h.state, h.reason) for h in state.health),
            )
            if signature != last_signature:
                print(json.dumps(state.to_dict(), allow_nan=False), flush=True)
                last_signature = signature
            if args.once:
                return 0 if all(h.state == "ready" for h in state.health) else 1
            try:
                await asyncio.wait_for(
                    stop.wait(), max(0, args.interval - (loop.time() - started)))
            except asyncio.TimeoutError:
                pass
        print(json.dumps(
            gateway.stopped_state(f"{stop_signal[0]} received").to_dict(),
            allow_nan=False), flush=True)
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Read the real ContextCue sensor gateway (Thermo, Light, Distance)")
    parser.add_argument("--once", action="store_true", help="Read once; exit 1 unless the Bridge and every sensor are ready")
    parser.add_argument("--capabilities", action="store_true", help="Print capabilities and exit")
    parser.add_argument("--interval", type=float, default=0.1, help="Poll period in seconds (>=0.1)")
    parser.add_argument("--router", help="Override APP_SOCKET / the board's Arduino Router socket")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 0.1:
        parser.error("--interval must be a finite number >=0.1")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    main()
