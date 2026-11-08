# ContextCue (App Lab app)

The full ContextCue app on the UNO Q: application service, kiosk UI, the real
`HardwareGateway` on the Linux side, and the Modulino firmware on the MCU.

**This folder is a template, not a runnable app.** App Lab mounts only an
app's own folder into its container, and this app needs code from elsewhere in
the repository (`services/application`, `services/ui`, the hardware gateway
and contracts, and the firmware sketch from `apps/contextcue-hardware`).
`scripts/contextcue-app.sh build` assembles a self-contained copy.

## Run it

On the UNO Q, from your clone of the repository:

```bash
git pull
scripts/contextcue-app.sh run      # build ~/ArduinoApps/contextcue, flash, start
scripts/contextcue-app.sh url      # UI address inside the board
scripts/contextcue-app.sh kiosk    # show it full screen on the board's display
scripts/contextcue-app.sh logs     # follow the Python logs
scripts/contextcue-app.sh stop
```

From a laptop, deploying the current working tree (uncommitted changes
included) over SSH:

```bash
scripts/unoq.sh ui-run     # build locally, copy to the board, flash, start
scripts/unoq.sh ui-open    # SSH tunnel; then open http://localhost:7300
scripts/unoq.sh ui-logs
scripts/unoq.sh ui-stop
```

Only one App Lab app runs at a time: starting this one stops
`contextcue-hardware` and the reverse.

## What a build contains

```text
~/ArduinoApps/contextcue/
  app.yaml  README.md            from this template
  sketch/                        apps/contextcue-hardware/sketch
  python/main.py                 entry point: services/ui/main.py on the real gateway
  python/requirements.txt
  python/build_info.txt          commit, branch, build time (printed at start)
  python/apps/contextcue-hardware/python/{contextcue_contracts,contextcue_hardware}
  python/services/application/{contextcue_app,fixtures}
  python/services/ui/{main.py,contextcue_ui,assets}
  data/contextcue.db             settings and notices; kept across builds
```

A build replaces only `app.yaml`, `README.md`, `python/` and `sketch/`, and
refuses to write into a folder that is not a ContextCue app.

The UI listens on port 7000 inside the app container. App Lab does not
publish that port on the board, so use `url` on the board or `ui-open` from a
laptop.
