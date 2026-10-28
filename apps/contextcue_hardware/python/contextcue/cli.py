"""Console bring-up on UNO Q Linux. stdout is JSON Lines, diagnostics stderr."""

import argparse
import asyncio
import json
import logging
import math
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
        last_signature = None
        while True:
            started = asyncio.get_running_loop().time()
            state = await gateway.read_state()
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
            await asyncio.sleep(max(0, args.interval - (asyncio.get_running_loop().time() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read the real ContextCue Thermo gateway")
    parser.add_argument("--once", action="store_true", help="Read once; exit 1 if degraded")
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
