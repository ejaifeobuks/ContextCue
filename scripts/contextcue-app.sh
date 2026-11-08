#!/usr/bin/env bash
# Assemble and run the full ContextCue App Lab app from this repository.
#
#   scripts/contextcue-app.sh build [DIR]   assemble the app into DIR
#   scripts/contextcue-app.sh run           build into the app folder, then (re)start it
#   scripts/contextcue-app.sh start         start the already-built app
#   scripts/contextcue-app.sh stop          stop it
#   scripts/contextcue-app.sh status        list running App Lab apps
#   scripts/contextcue-app.sh logs          follow the app's Python logs
#   scripts/contextcue-app.sh url           print the UI address (inside the board)
#   scripts/contextcue-app.sh kiosk         open the UI full screen on the board display
#
# App Lab mounts only an app's own folder into its container, so the code the
# app needs (services/, the hardware gateway, the firmware sketch) is copied
# from this repository into one folder. Run it on the UNO Q after `git pull`
# (every command), or on a laptop for `build` (scripts/unoq.sh ui-run uses it
# to deploy uncommitted work over SSH). The app folder defaults to
# ~/ArduinoApps/contextcue; override with CONTEXTCUE_APP_DIR. A build replaces
# only app.yaml, README.md, python/ and sketch/: data/ (settings, notices) and
# App Lab's .cache/ are kept.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/apps/contextcue"
APP_DIR="${CONTEXTCUE_APP_DIR:-$HOME/ArduinoApps/contextcue}"
UI_PORT=7000

die() { echo "contextcue-app: $*" >&2; exit 1; }

copy_tree() {  # copy_tree SRC DEST: copy a source tree without bytecode
  cp -R "$1" "$2"
  find "$2" -name __pycache__ -prune -exec rm -rf {} +
  find "$2" -name '*.py[cod]' -delete
}

build_info() {
  if ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "commit unknown built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    return
  fi
  local commit dirty=""
  commit="$(git -C "$ROOT" rev-parse --short HEAD)"
  if [ -n "$(git -C "$ROOT" status --porcelain -- apps services)" ]; then
    dirty="+uncommitted"
  fi
  echo "commit ${commit}${dirty} ($(git -C "$ROOT" rev-parse --abbrev-ref HEAD)) built $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

build() {
  local dest="${1:-$APP_DIR}"
  [ -n "$dest" ] || die "no destination folder"
  mkdir -p "$dest"
  dest="$(cd "$dest" && pwd)"
  case "$dest" in
    / | "$HOME" | "$ROOT" | "$ROOT"/*) die "refusing to build into $dest" ;;
  esac
  # Only replace an earlier ContextCue build or an empty folder.
  if [ -n "$(ls -A "$dest")" ] && ! grep -qx "name: ContextCue" "$dest/app.yaml" 2>/dev/null; then
    die "$dest exists and is not a ContextCue app; choose another folder"
  fi

  rm -rf "$dest/app.yaml" "$dest/README.md" "$dest/python" "$dest/sketch"
  cp "$TEMPLATE/app.yaml" "$TEMPLATE/README.md" "$dest/"
  mkdir -p "$dest/python/apps/contextcue-hardware/python" \
           "$dest/python/services/application" "$dest/python/services/ui"
  cp "$TEMPLATE/python/main.py" "$TEMPLATE/python/requirements.txt" "$dest/python/"
  copy_tree "$ROOT/apps/contextcue-hardware/sketch" "$dest/sketch"
  for package in contextcue_contracts contextcue_hardware; do
    copy_tree "$ROOT/apps/contextcue-hardware/python/$package" \
              "$dest/python/apps/contextcue-hardware/python/$package"
  done
  copy_tree "$ROOT/services/application/contextcue_app" "$dest/python/services/application/contextcue_app"
  copy_tree "$ROOT/services/application/fixtures" "$dest/python/services/application/fixtures"
  cp "$ROOT/services/ui/main.py" "$dest/python/services/ui/"
  copy_tree "$ROOT/services/ui/contextcue_ui" "$dest/python/services/ui/contextcue_ui"
  copy_tree "$ROOT/services/ui/assets" "$dest/python/services/ui/assets"
  build_info > "$dest/python/build_info.txt"
  echo "Built ContextCue ($(cat "$dest/python/build_info.txt")) in $dest"
}

need_app_cli() {
  command -v arduino-app-cli >/dev/null || die "arduino-app-cli not found: run this on the UNO Q"
}

ui_address() {  # IP of the running app container; the port is not published on the board
  local id
  id="$(docker ps -q --filter label=cc.arduino.app.main=true \
                     --filter "label=cc.arduino.app.path=$APP_DIR" | head -n 1)"
  [ -n "$id" ] || die "ContextCue is not running (scripts/contextcue-app.sh run)"
  docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$id"
}

case "${1:-}" in
  build)
    build "${2:-}"
    ;;
  run)
    need_app_cli
    build "$APP_DIR"
    # Stop, then start: `app restart` can leave the app marked failed.
    arduino-app-cli app stop "$APP_DIR" >/dev/null 2>&1 || true
    arduino-app-cli app start "$APP_DIR"
    ;;
  start)
    need_app_cli
    arduino-app-cli app start "$APP_DIR"
    ;;
  stop)
    need_app_cli
    arduino-app-cli app stop "$APP_DIR"
    ;;
  status)
    need_app_cli
    arduino-app-cli app ps
    ;;
  logs)
    need_app_cli
    arduino-app-cli app logs "$APP_DIR" --follow
    ;;
  url)
    echo "http://$(ui_address):$UI_PORT"
    ;;
  kiosk)
    url="http://$(ui_address):$UI_PORT"
    DISPLAY="${DISPLAY:-:0}" nohup chromium --kiosk --noerrdialogs --disable-infobars "$url" \
      >/dev/null 2>&1 &
    echo "Opened $url on display ${DISPLAY:-:0}"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
