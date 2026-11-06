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
# Needs an SSH host alias (default "unoq") that logs in without a password.
# Override with UNOQ_HOST=... and UNOQ_APP_DIR=... (relative to the board home).
set -euo pipefail

HOST="${UNOQ_HOST:-unoq}"
REMOTE_APP="${UNOQ_APP_DIR:-ArduinoApps/contextcue-hardware}"
# Shown in App Lab; distinguishes this dev copy from App Lab-managed copies.
APP_NAME="${UNOQ_APP_NAME:-ContextCue Hardware (dev)}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_APP="$ROOT/apps/contextcue-hardware"
SSH=(ssh -o BatchMode=yes "$HOST")

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

case "${1:-}" in
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
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
