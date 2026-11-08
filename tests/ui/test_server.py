"""UI server contract: state polling, actions, errors and static file safety."""

import asyncio
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
import support  # noqa: E402

from contextcue_app import ContextCueApp  # noqa: E402
from contextcue_app.dev_gateway import SimulatedGateway  # noqa: E402
from contextcue_app.repository import DataRepository  # noqa: E402
from contextcue_app.sources import load_scenarios  # noqa: E402
from contextcue_ui import UIServer  # noqa: E402


def request(url, body=None):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            return res.status, res.read(), res.headers.get("Content-Type")
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(), exc.headers.get("Content-Type")


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        scenarios = load_scenarios()
        self.clock = support.frozen_clock(scenarios["event_rain"].start)
        gw = SimulatedGateway(monotonic=self.clock.monotonic, utcnow=self.clock.now)
        self.repo = DataRepository()
        self.app = ContextCueApp(gateway=gw, clock=self.clock, repository=self.repo,
                                 gateway_mode="simulated", scenario_id="event_rain", scenarios=scenarios)
        await self.app._tick()
        self.server = UIServer(self.app, asyncio.get_running_loop(), "127.0.0.1", 0)
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.stop)
        await self.app.stop()
        self.repo.close()

    async def call(self, path, body=None):
        return await asyncio.to_thread(request, self.base + path, body)

    async def test_state_and_index(self):
        status, body, kind = await self.call("/api/state")
        self.assertEqual((status, kind), (200, "application/json"))
        state = json.loads(body)
        self.assertEqual(state["gateway_mode"], "simulated")
        self.assertEqual(state["clock"]["label"], "Scenario time (simulated)")
        status, body, kind = await self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ContextCue", body)
        for asset, kind in (("/app.js", "javascript"), ("/ui-logic.js", "javascript"), ("/styles.css", "text/css"),
                            ("/icons/sprite.svg", "image/svg+xml"), ("/scene.svg", "image/svg+xml")):
            status, _, served = await self.call(asset)
            self.assertEqual(status, 200, asset)
            self.assertIn(kind, served, asset)

    async def test_actions_and_errors(self):
        status, body, _ = await self.call("/api/action", {"action": "mute", "payload": {"muted": True}})
        self.assertEqual((status, json.loads(body)["status"]), (200, "muted"))
        self.assertTrue(self.app.notifications.muted)
        status, body, _ = await self.call("/api/action", {"action": "mute", "payload": {"muted": "yes"}})
        self.assertEqual(status, 400)
        status, body, _ = await self.call("/api/action", {"action": "format_disk"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "Unknown action: format_disk"))
        status, _, _ = await self.call("/api/action", b"{not json")
        self.assertEqual(status, 400)
        status, body, _ = await self.call("/api/action", {"action": "ask", "payload": {"question": "x"}})
        self.assertEqual(status, 400)

    async def test_static_files_cannot_escape_assets(self):
        for path in ("/../main.py", "/%2e%2e/main.py", "/../../services/ui/main.py", "/nope.js"):
            self.assertEqual((await self.call(path))[0], 404, path)


if __name__ == "__main__":
    unittest.main()
