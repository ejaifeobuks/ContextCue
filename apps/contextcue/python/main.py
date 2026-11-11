"""App Lab entry point for ContextCue: application, kiosk UI and real hardware.

App Lab mounts only this app's folder into the container, so
`scripts/contextcue-app.sh build` copies the repository code this needs
(services/application, services/ui, the hardware gateway and contracts, and
the firmware sketch) next to this file. Do not run this template directly.

Settings, notices and logs persist in the app's data/ folder, which builds
and redeploys leave untouched.
"""

import re
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
ENTRY = HERE / "services" / "ui" / "main.py"
MODEL_URL = "http://llamacpp-models-runner:9999/v1"


def declared_model(app_yaml: Path) -> str | None:
    """The model the arduino:llm Brick was given in app.yaml. The server lists
    every downloaded model, so the app must ask for this one by name."""
    try:
        match = re.search(r"^\s*model:\s*llamacpp:(\S+)\s*$", app_yaml.read_text(encoding="utf-8"), re.M)
    except OSError:
        return None
    return match.group(1) if match else None


def main() -> None:
    info = HERE / "build_info.txt"
    if info.exists():
        print(f"ContextCue build: {info.read_text(encoding='utf-8').strip()}", flush=True)
    if not ENTRY.exists():
        raise SystemExit("This is the unassembled template; build it with scripts/contextcue-app.sh")
    DATA.mkdir(exist_ok=True)
    # Real HardwareGateway (no --simulated). Listen on all container interfaces
    # so the UI is reachable from the board and through an SSH tunnel.
    sys.argv = [str(ENTRY), "--host", "0.0.0.0", "--port", "7000",
                "--db", str(DATA / "contextcue.db"),
                # The arduino:llm Brick declared in app.yaml (on-board, CPU).
                "--model-url", MODEL_URL]
    model = declared_model(HERE.parent / "app.yaml")
    if model:
        sys.argv += ["--model-name", model]
    runpy.run_path(str(ENTRY), run_name="__main__")


if __name__ == "__main__":
    main()
