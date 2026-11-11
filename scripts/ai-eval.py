"""Evaluate the app's real prompts against a live model server.

Builds the snapshots the application actually sends (every fixture scenario's
automatic notices, an explicit briefing and the four bounded questions) on a
simulated gateway, sends each through `LlamaCppModel`, validates the reply with
`validate_response` and prints one JSON line per request plus a summary.

On the UNO Q, inside the running ContextCue app (where the arduino:llm Brick
is reachable):

    docker exec contextcue-main-1 python /app/python/ai_eval.py

(scripts/contextcue-app.sh build copies this file to python/ai_eval.py.)
Laptop, against any OpenAI-compatible server:

    python3 scripts/ai-eval.py --model-url http://127.0.0.1:9999/v1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for root in (HERE, HERE.parent):  # assembled app (python/) or repository (scripts/..)
    for extra in ("apps/contextcue-hardware/python", "services/application"):
        if (root / extra).is_dir() and str(root / extra) not in sys.path:
            sys.path.insert(0, str(root / extra))

from contextcue_app import ContextCueApp  # noqa: E402
from contextcue_app.clock import ScenarioClock, SystemClock  # noqa: E402
from contextcue_app.dev_gateway import SimulatedGateway  # noqa: E402
from contextcue_app.recommendation import (SUPPORTED_QUESTIONS, LlamaCppModel,  # noqa: E402
                                           ModelUnavailable, validate_response)
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.sources import load_scenarios  # noqa: E402


class Capture:
    """Records the snapshots the app would send, without generating."""

    execution_location = "capture"
    model_version = None

    def __init__(self):
        self.snapshots = []

    async def complete(self, snapshot):
        self.snapshots.append(snapshot)
        raise ModelUnavailable("captured")


async def settle(app) -> None:
    for _ in range(200):
        await asyncio.sleep(0)
        if not app.recommender.busy and not app._pending:
            return


async def collect_snapshots() -> list:
    scenarios = load_scenarios()
    capture = Capture()
    for scenario_id, scenario in scenarios.items():
        clock = ScenarioClock(scenario.start, frozen=True)
        gateway = SimulatedGateway(monotonic=clock.monotonic, utcnow=SystemClock().now)
        for metric, key in (("temperature", "temperature"), ("humidity", "humidity"),
                            ("ambient_light", "lux"), ("distance", "distance_mm")):
            if key in scenario.sensors:
                gateway.set_value(metric, scenario.sensors[key])
        repo = DataRepository()
        app = ContextCueApp(gateway=gateway, clock=clock, repository=repo, gateway_mode="simulated",
                            scenario_id=scenario_id, scenarios=scenarios, model=capture)
        await app._tick()
        for _ in range(3):  # comfort needs its persistence interval
            clock.advance(30)
            await app._tick()
        await settle(app)
        if scenario_id == "morning":
            await app.action("briefing", {})
            await settle(app)
            for question in SUPPORTED_QUESTIONS:
                # One at a time: the app coalesces requests that queue up.
                await app.action("ask", {"question": question})
                await settle(app)
        await app.stop()
        repo.close()
    return capture.snapshots


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-url", default="http://llamacpp-models-runner:9999/v1")
    parser.add_argument("--model-name", default=None,
                        help="model to use (default: the one declared in the app's app.yaml)")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json-object", action="store_true",
                        help="use plain JSON mode instead of the JSON schema")
    args = parser.parse_args()
    snapshots = await collect_snapshots()
    name = args.model_name
    if name is None:
        try:  # assembled app: python/ai_eval.py next to python/main.py and ../app.yaml
            sys.path.insert(0, str(HERE))
            from main import declared_model  # noqa: E402
            name = declared_model(HERE.parent / "app.yaml")
        except ImportError:
            name = None
    model = LlamaCppModel(args.model_url, name, timeout_s=args.timeout, structured=not args.json_object)
    await model.warm_up()
    times, valid = [], 0
    for snap in snapshots:
        started = time.monotonic()
        record = {"category": snap.category, "question": snap.question, "reason": snap.reason}
        try:
            response = await model.complete(snap)
            record["text"] = response.text
            record["evidence_ids"] = list(response.evidence_ids)
            record["rejected"] = validate_response(response, snap)
        except Exception as exc:  # noqa: BLE001 - every failure is a result
            record["error"] = f"{type(exc).__name__}: {exc}"
        record["seconds"] = round(time.monotonic() - started, 1)
        times.append(record["seconds"])
        valid += int("error" not in record and record["rejected"] is None)
        print(json.dumps(record), flush=True)
    times.sort()
    print(json.dumps({"summary": True, "model": model.model_version, "structured": model.structured,
                      "requests": len(snapshots), "valid": valid,
                      "p50_s": times[len(times) // 2] if times else None,
                      "max_s": times[-1] if times else None,
                      "mean_s": round(statistics.mean(times), 1) if times else None}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
