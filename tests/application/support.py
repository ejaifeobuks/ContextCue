"""Shared helpers: deterministic clocks and fakes for application tests."""

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for extra in (ROOT / "apps/contextcue-hardware/python", ROOT / "services/application",
              ROOT / "services/ui"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from contextcue_app.clock import ScenarioClock  # noqa: E402


class ManualBase:
    mode = "real"

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t, timezone.utc)


def frozen_clock(start: datetime) -> ScenarioClock:
    """Scenario clock that only moves with advance()."""
    return ScenarioClock(start, base=ManualBase(), frozen=True)


class FakeSpeech:
    def __init__(self, outcomes=None, connected=True):
        self.outcomes = list(outcomes or [])
        self.played = []
        self.stopped = []
        self.connected = connected
        self.block = None  # an asyncio.Event to hold playback open

    def status(self):
        return {"engine": "fake", "endpoint": "fake-speaker", "paired": True,
                "connected": self.connected, "ready": self.connected, "reason": None}

    async def play(self, playback_id, text, volume):
        self.played.append((playback_id, text, volume))
        if self.block is not None:
            await self.block.wait()
        return self.outcomes.pop(0) if self.outcomes else "completed"

    async def stop(self, playback_id):
        self.stopped.append(playback_id)


def temp_dir(case) -> str:
    """Temporary directory removed after the test's other cleanups.

    Cleanups run last-in first-out, so repositories registered later are
    closed first. Windows cannot delete an SQLite file that is still open.
    """
    path = tempfile.mkdtemp(prefix="contextcue-test-")
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path
