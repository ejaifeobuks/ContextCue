#!/usr/bin/env bash
# Deploy and run the ContextCue hardware app on an Arduino UNO Q over SSH.
#
#   scripts/unoq.sh run      copy the app to the board, then build/flash/start it
#   scripts/unoq.sh deploy   copy only
#   scripts/unoq.sh stop     stop the app
#   scripts/unoq.sh logs     follow the Python logs and save them to logs/
#   scripts/unoq.sh status   list running apps
#   scripts/unoq.sh agent    start the SSH agent if needed and unlock the key
#
# Full ContextCue app (application + kiosk UI + real sensors), built from this
# working tree with scripts/contextcue-app.sh, uncommitted changes included:
#   scripts/unoq.sh ui-run     build, copy to the board, flash, start
#   scripts/unoq.sh ui-deploy  build and copy only
#   scripts/unoq.sh ui-open    SSH tunnel to the UI: open http://localhost:7300
#   scripts/unoq.sh ui-kiosk   show the UI full screen on the board's display
#   scripts/unoq.sh ui-logs    follow the app's Python logs (saved to logs/)
#   scripts/unoq.sh ui-stop    stop the app
#
# Needs an SSH host alias (default "unoq") that logs in without a password.
# Override with UNOQ_HOST=..., UNOQ_APP_DIR=... / UNOQ_UI_APP_DIR=... (relative
# to the board home) and UNOQ_UI_PORT=... (local tunnel port).
set -euo pipefail

HOST="${UNOQ_HOST:-unoq}"
REMOTE_APP="${UNOQ_APP_DIR:-ArduinoApps/contextcue-hardware}"
# Shown in App Lab; distinguishes this dev copy from App Lab-managed copies.
APP_NAME="${UNOQ_APP_NAME:-ContextCue Hardware (dev)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_APP="$ROOT/apps/contextcue-hardware"
SSH=(ssh -o BatchMode=yes "$HOST")
REMOTE_UI_APP="${UNOQ_UI_APP_DIR:-ArduinoApps/contextcue}"
UI_LOCAL_PORT="${UNOQ_UI_PORT:-7300}"

AGENT_SOCK="$HOME/.ssh/agent.sock"

# Start an agent on $AGENT_SOCK unless a live one already owns it. After a
# reboot or WSL restart the socket file outlives its agent, and `ssh-agent -a`
# refuses an existing path, so a stale socket is removed first.
ensure_agent() {
  local rc=0
  SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add -l >/dev/null 2>&1 || rc=$?
  # 0 = keys loaded, 1 = live agent without keys, 2 = no agent listening.
  if [ "$rc" -eq 2 ]; then
    rm -f "$AGENT_SOCK"
    ssh-agent -a "$AGENT_SOCK" >/dev/null
    echo "Started a new SSH agent on $AGENT_SOCK." >&2
  fi
}

check_connection() {
  if ! "${SSH[@]}" true 2>/dev/null; then
    ensure_agent
    if ! "${SSH[@]}" true 2>/dev/null; then
      echo "Cannot reach '$HOST' without a password." >&2
      if ! SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add -l >/dev/null 2>&1; then
        echo "The SSH agent has no key loaded. Unlock it once per session:" >&2
        echo "  scripts/unoq.sh agent" >&2
      else
        echo "The key is loaded; check that the board is on and reachable." >&2
      fi
      exit 1
    fi
  fi
}

deploy() {
  # Replace only the files we own; keep App Lab's reserved .cache/ and data/.
  tar -C "$LOCAL_APP" --exclude='__pycache__' --exclude='*.py[cod]' \
      -czf - app.yaml README.md python sketch |
    "${SSH[@]}" "set -e; mkdir -p ~/$REMOTE_APP; cd ~/$REMOTE_APP;
                 rm -rf app.yaml README.md python sketch; tar -xzf -;
                 sed -i 's/^name: .*/name: $APP_NAME/' app.yaml"
  echo "Deployed $(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet -- "$LOCAL_APP" || echo '+uncommitted') to $HOST:~/$REMOTE_APP"
}

ui_deploy() {
  local stage
  stage="$(mktemp -d)"
  "$ROOT/scripts/contextcue-app.sh" build "$stage/contextcue" >/dev/null
  # Replace only the files a build owns; keep data/ (settings) and .cache/.
  tar -C "$stage/contextcue" -czf - app.yaml README.md python sketch |
    "${SSH[@]}" "set -e; mkdir -p ~/$REMOTE_UI_APP; cd ~/$REMOTE_UI_APP;
                 if [ -f app.yaml ] && ! grep -qx 'name: ContextCue' app.yaml; then
                   echo '~/$REMOTE_UI_APP is not a ContextCue app' >&2; exit 1; fi
                 rm -rf app.yaml README.md python sketch; tar -xzf -"
  echo "Deployed ContextCue ($(cat "$stage/contextcue/python/build_info.txt")) to $HOST:~/$REMOTE_UI_APP"
  rm -rf "$stage"
}

ui_address() {  # the app container's IP; App Lab does not publish its port
  "${SSH[@]}" "docker ps -q --filter label=cc.arduino.app.main=true \
      --filter label=cc.arduino.app.path=\$HOME/$REMOTE_UI_APP | head -n 1 |
    xargs -r docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'"
}

case "${1:-}" in
  ui-deploy)
    check_connection
    ui_deploy
    ;;
  ui-run)
    check_connection
    ui_deploy
    # Stop, then start: `app restart` can leave the app marked failed.
    "${SSH[@]}" "arduino-app-cli app stop ~/$REMOTE_UI_APP >/dev/null 2>&1 || true;
                 arduino-app-cli app start ~/$REMOTE_UI_APP"
    ;;
  ui-stop)
    check_connection
    "${SSH[@]}" "arduino-app-cli app stop ~/$REMOTE_UI_APP"
    ;;
  ui-logs)
    check_connection
    mkdir -p "$ROOT/logs"
    log="$ROOT/logs/contextcue-$(date -u +%Y%m%dT%H%M%SZ).log"
    echo "Saving to ${log#"$ROOT"/} (Ctrl+C to stop)" >&2
    "${SSH[@]}" "arduino-app-cli app logs ~/$REMOTE_UI_APP --follow" | tee "$log"
    ;;
  ui-open)
    check_connection
    address="$(ui_address)"
    [ -n "$address" ] || { echo "ContextCue is not running; start it with: scripts/unoq.sh ui-run" >&2; exit 1; }
    echo "ContextCue UI: http://localhost:$UI_LOCAL_PORT (Ctrl+C closes the tunnel)" >&2
    exec ssh -N -L "$UI_LOCAL_PORT:$address:7000" "$HOST"
    ;;
  ui-kiosk)
    check_connection
    address="$(ui_address)"
    [ -n "$address" ] || { echo "ContextCue is not running; start it with: scripts/unoq.sh ui-run" >&2; exit 1; }
    "${SSH[@]}" "DISPLAY=:0 nohup chromium --kiosk --noerrdialogs --disable-infobars \
                 http://$address:7000 >/dev/null 2>&1 &"
    echo "Opened http://$address:7000 on the board's display" >&2
    ;;
  run)
    check_connection
    deploy
    "${SSH[@]}" "arduino-app-cli app restart ~/$REMOTE_APP"
    ;;
  deploy)
    check_connection
    deploy
    ;;
  stop)
    check_connection
    "${SSH[@]}" "arduino-app-cli app stop ~/$REMOTE_APP"
    ;;
  logs)
    check_connection
    mkdir -p "$ROOT/logs"
    log="$ROOT/logs/unoq-$(date -u +%Y%m%dT%H%M%SZ).log"
    echo "Saving to ${log#"$ROOT"/} (Ctrl+C to stop)" >&2
    "${SSH[@]}" "arduino-app-cli app logs ~/$REMOTE_APP --follow" | tee "$log"
    ;;
  status)
    check_connection
    "${SSH[@]}" "arduino-app-cli app ps"
    ;;
  agent)
    ensure_agent
    SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add -l >/dev/null 2>&1 ||
      SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add "$HOME/.ssh/id_ed25519"
    SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add -l
    ;;
  *)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
