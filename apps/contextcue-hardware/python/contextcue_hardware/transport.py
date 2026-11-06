"""Own the official Arduino Router client; never open the MCU serial port."""

import os
from typing import Protocol


class BridgeTransport(Protocol):
    def connect(self, timeout: float) -> None: ...

    def call(self, method: str, *args: object, timeout: float) -> object: ...

    def router_version(self, timeout: float) -> str: ...

    def close(self) -> None: ...


class ArduinoBridgeTransport:
    def __init__(self, address: str | None = None):
        self.address = address or os.environ.get(
            "APP_SOCKET", "unix:///var/run/arduino-router.sock"
        )
        self._client = None

    def connect(self, timeout: float) -> None:
        if self._client is None:
            try:
                from arduino.router_bridge import Bridge
            except ImportError as exc:
                raise RuntimeError(
                    "Arduino Router Bridge is missing. Run this app in App Lab, "
                    "or install the project's [board] extra on the UNO Q Linux side."
                ) from exc
            self._client = Bridge(self.address)
        if not self._client.connect(timeout=timeout):
            raise ConnectionError(f"Arduino Router unavailable at {self.address}")

    def call(self, method: str, *args: object, timeout: float) -> object:
        if self._client is None:
            raise ConnectionError("Bridge has not been connected")
        return self._client.call(method, *args, timeout=timeout)

    def router_version(self, timeout: float) -> str:
        # The router answers $/version itself without forwarding to the MCU,
        # so it separates a dead router from an unresponsive MCU.
        return str(self.call("$/version", timeout=timeout))

    def close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client = None
