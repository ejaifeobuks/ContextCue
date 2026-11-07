"""Kiosk UI server: static assets plus a small JSON API.

Standard library only, so it runs unchanged on a laptop and in the UNO Q app
container. The HTTP server runs in its own thread; every call into the
application is scheduled on the application's asyncio loop, so the UI never
blocks sensing, policy or generation (APP-07).

API
  GET  /api/state                      view model (polled by the page)
  POST /api/action  {"action", "payload"}  user or simulation action
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("contextcue.ui")

ASSETS = Path(__file__).resolve().parents[1] / "assets"
CALL_TIMEOUT_S = 2.0
MAX_BODY = 16 * 1024


class UIServer:
    def __init__(self, app, loop: asyncio.AbstractEventLoop, host: str = "127.0.0.1",
                 port: int = 7000, assets: Path = ASSETS):
        self.app = app
        self.loop = loop
        self.assets = assets.resolve()
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self.httpd.server_address[:2]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="contextcue-ui",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _call(self, coro_or_fn, *args):
        async def run():
            result = coro_or_fn(*args)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        future = asyncio.run_coroutine_threadsafe(run(), self.loop)
        return future.result(timeout=CALL_TIMEOUT_S)

    def _handler_class(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ContextCueUI/1.0"

            def log_message(self, fmt, *args):  # keep stdout for the app
                log.debug("%s %s", self.address_string(), fmt % args)

            def _json(self, status: int, data) -> None:
                body = json.dumps(data, default=str, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/state":
                    try:
                        self._json(200, server._call(server.app.view_model))
                    except FutureTimeout:
                        self._json(503, {"error": "application_busy"})
                    except Exception as exc:
                        log.exception("view model failed")
                        self._json(500, {"error": str(exc)})
                    return
                if path == "/":
                    path = "/index.html"
                target = (server.assets / path.lstrip("/")).resolve()
                if server.assets not in target.parents or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = target.read_bytes()
                kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path.split("?", 1)[0] != "/api/action":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_BODY:
                    self._json(400, {"error": "Request body required (max 16 KB)"})
                    return
                try:
                    data = json.loads(self.rfile.read(length))
                    name = data["action"]
                    payload = data.get("payload") or {}
                    if not isinstance(name, str) or not isinstance(payload, dict):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    self._json(400, {"error": "Expected {\"action\": str, \"payload\": object}"})
                    return
                try:
                    result = server._call(server.app.action, name, payload)
                    self._json(200, result)
                except FutureTimeout:
                    self._json(503, {"error": "application_busy"})
                except ValueError as exc:  # ActionError and validation errors
                    self._json(400, {"error": str(exc)})
                except Exception as exc:
                    log.exception("action %s failed", name)
                    self._json(500, {"error": str(exc)})

        return Handler
