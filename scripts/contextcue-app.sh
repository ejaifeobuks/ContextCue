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
#   scripts/contextcue-app.sh kiosk [--wait SECONDS]
#                                           open the UI full screen on the board display
#   scripts/contextcue-app.sh autostart on|off|status
#                                           start the app and the kiosk at every boot
#
# App Lab mounts only an app's own folder into its container, so the code the
# app needs (services/, the hardware gateway, the firmware sketch) is copied
# from this repository into one folder. Run it on the UNO Q after `git pull`
# (every command), or on a laptop for `build` (scripts/unoq.sh ui-run uses it
# to deploy uncommitted work over SSH). The app folder defaults to
# ~/ArduinoApps/contextcue; override with CONTEXTCUE_APP_DIR. A build replaces
# only app.yaml, README.md, python/ and sketch/: data/ (settings, notices) and
# App Lab's .cache/ are kept. Keep the clone outside ~/ArduinoApps, or App Lab
# lists the apps inside it as extra apps.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/apps/contextcue"
APP_DIR="${CONTEXTCUE_APP_DIR:-$HOME/ArduinoApps/contextcue}"
UI_PORT=7000
KIOSK_PROFILE="$HOME/.config/contextcue-kiosk"
AUTOSTART_DIR="$HOME/.config/autostart"

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
  # Prompt evaluation against the app's own model server (docker exec ... ai_eval.py).
  cp "$ROOT/scripts/ai-eval.py" "$dest/python/ai_eval.py"
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

app_url() {  # the running app container's UI; App Lab does not publish the port
  local id address
  id="$(docker ps -q --filter label=cc.arduino.app.main=true \
                     --filter "label=cc.arduino.app.path=$APP_DIR" | head -n 1)"
  [ -n "$id" ] || return 1
  address="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$id")"
  [ -n "$address" ] || return 1
  echo "http://$address:$UI_PORT"
}

kiosk() {
  local wait_s=0 url="" deadline prefs
  if [ "${1:-}" = "--wait" ]; then wait_s="${2:-600}"; fi
  export DISPLAY="${DISPLAY:-:0}"
  # A kiosk must not blank or lock: unlocking needs a keyboard. Do this first,
  # so the screen stays unlocked even if the app never answers.
  xset s off s noblank -dpms 2>/dev/null || true
  pkill -x light-locker 2>/dev/null || true
  deadline=$((SECONDS + wait_s))
  # At boot App Lab starts the app after the desktop is up: wait until it answers.
  until url="$(app_url)" && curl -sf -o /dev/null --max-time 2 "$url/api/state"; do
    [ "$SECONDS" -lt "$deadline" ] || die "ContextCue is not running (scripts/contextcue-app.sh run)"
    sleep 3
  done
  # Replace an earlier kiosk window. Its profile is only ever closed by killing
  # Chromium, so mark it cleanly closed; otherwise Chromium opens with a
  # "restore pages" bubble over the UI.
  if pkill -f -- "--user-data-dir=$KIOSK_PROFILE" 2>/dev/null; then sleep 1; fi
  prefs="$KIOSK_PROFILE/Default/Preferences"
  if [ -f "$prefs" ]; then
    sed -i 's/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/; s/"exited_cleanly":false/"exited_cleanly":true/' "$prefs"
  fi
  nohup chromium --user-data-dir="$KIOSK_PROFILE" --kiosk --noerrdialogs --disable-infobars \
    --no-first-run --disable-session-crashed-bubble --disable-features=Translate \
    --password-store=basic --check-for-update-interval=31536000 \
    --overscroll-history-navigation=0 "$url" >/dev/null 2>&1 &
  echo "Opened $url on display $DISPLAY"
}

write_desktop_entry() {  # write_desktop_entry FILE LINE...: a minimal XDG autostart entry
  local file="$1"
  shift
  { echo "[Desktop Entry]"; echo "Type=Application"; printf '%s\n' "$@"; } > "$file"
}

autostart() {
  local entry="$AUTOSTART_DIR/contextcue-kiosk.desktop"
  local locker="$AUTOSTART_DIR/light-locker.desktop"
  need_app_cli
  case "${1:-status}" in
    on)
      [ -f "$APP_DIR/app.yaml" ] || die "build the app first: scripts/contextcue-app.sh run"
      # App Lab's default app starts at every boot.
      arduino-app-cli properties set default "$APP_DIR"
      mkdir -p "$AUTOSTART_DIR"
      write_desktop_entry "$entry" "Name=ContextCue kiosk" \
        "Comment=Show ContextCue full screen once the app answers (scripts/contextcue-app.sh)" \
        "Exec=$ROOT/scripts/contextcue-app.sh kiosk --wait 600"
      # Hide the system lock screen for this user: on a touchscreen kiosk it
      # would ask for a password nobody can type.
      write_desktop_entry "$locker" "Name=Light Locker" "Hidden=true"
      echo "At boot: App Lab starts $APP_DIR; the desktop opens the kiosk when it answers."
      ;;
    off)
      arduino-app-cli properties set default none
      rm -f "$entry" "$locker"
      echo "Autostart off: no default app, no kiosk at login, lock screen restored."
      ;;
    status)
      arduino-app-cli properties get default
      if [ -f "$entry" ]; then echo "Kiosk at login: on ($entry)"; else echo "Kiosk at login: off"; fi
      if [ -f "$locker" ]; then echo "Lock screen at login: off"; else echo "Lock screen at login: on"; fi
      if pgrep -x light-locker >/dev/null; then echo "Lock screen running now: yes"; else echo "Lock screen running now: no"; fi
      DISPLAY="${DISPLAY:-:0}" xset q 2>/dev/null | grep -E "timeout:|DPMS is" | sed 's/^ */Screen blanking: /' || true
      ;;
    *)
      die "usage: scripts/contextcue-app.sh autostart on|off|status"
      ;;
  esac
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
    app_url || die "ContextCue is not running (scripts/contextcue-app.sh run)"
    ;;
  kiosk)
    kiosk "${2:-}" "${3:-}"
    ;;
  autostart)
    autostart "${2:-status}"
    ;;
  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
