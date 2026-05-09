#!/usr/bin/env bash
# Push local commits to origin, pull on the Linux processing box, and
# restart the FastAPI server so it picks up backend changes (the catalog
# loader, new endpoints, etc.). Vite HMR on the Linux box catches UI
# edits automatically — no UI restart needed.
#
# Usage: scripts/deploy-linux.sh [--no-push]
#
# Idempotent. Safe to run from a clean working tree.

set -euo pipefail

HOST="${ASTROLAB_LINUX_HOST:-192.168.1.254}"
REMOTE_REPO="${ASTROLAB_LINUX_REPO:-/home/bscholer/projects/astrolab}"
REMOTE_VENV="${ASTROLAB_LINUX_VENV:-.venv}"
PORT="${ASTROLAB_API_PORT:-8000}"
LOG_PATH="${ASTROLAB_LOG_PATH:-/tmp/astrolab-server.log}"
# Cache + DB live on the big scratch partition (2TB ext4). Override with
# ASTROLAB_LINUX_HOME if the box's layout differs.
REMOTE_HOME="${ASTROLAB_LINUX_HOME:-/scratch/astrolab}"

skip_push=0
for arg in "$@"; do
  case "$arg" in
    --no-push) skip_push=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }

# --- 1. Push local commits -------------------------------------------------
if [[ $skip_push -eq 0 ]]; then
  step "git push origin"
  git push origin
else
  warn "skipping local push (--no-push)"
fi

# --- 2. Pull + restart on the Linux box ------------------------------------
# Single SSH invocation: less authentication overhead, and any failure aborts
# the whole sequence cleanly via `set -e` inside the remote shell.
step "ssh $HOST: pull + restart uvicorn"
ssh -o ConnectTimeout=10 "$HOST" \
  REMOTE_REPO="$REMOTE_REPO" \
  REMOTE_VENV="$REMOTE_VENV" \
  PORT="$PORT" \
  LOG_PATH="$LOG_PATH" \
  REMOTE_HOME="$REMOTE_HOME" \
  bash -s <<'REMOTE'
set -euo pipefail
cd "$REMOTE_REPO"

echo "▸ git pull"
git fetch --quiet origin
git pull --ff-only origin "$(git rev-parse --abbrev-ref HEAD)"

# uv-managed venv? Run a quick sync so dependency changes show up. We
# add ~/.local/bin to PATH before checking because non-interactive ssh
# shells often drop it (login configs run for interactive sessions only)
# and silently skipping the sync is exactly how we ended up shipping a
# starnet_extract revision the venv couldn't import (missing tifffile).
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  echo "▸ uv sync"
  uv sync --frozen 2>/dev/null || uv sync
fi

echo "▸ restarting astrolab-api via systemctl"
# astrolab-api.service runs uvicorn under systemd, auto-starts on boot,
# and reads ASTROLAB_HOME from its unit file (not from this script anymore).
# If the unit isn't installed yet, fall back to the legacy nohup runner
# so a fresh box still works until scripts/install-systemd-units.sh has
# been run.
if systemctl list-unit-files astrolab-api.service >/dev/null 2>&1 \
     && systemctl is-enabled --quiet astrolab-api 2>/dev/null; then
  sudo systemctl restart astrolab-api
  # Vite HMR picks up FE changes without restart, but if the user touched
  # package.json we'd want to bounce the UI too. Cheap to restart either way.
  sudo systemctl restart astrolab-ui
else
  echo "  astrolab-api.service not installed — running legacy nohup uvicorn"
  pkill -f "uvicorn server.api:app" || true
  for _ in 1 2 3 4 5; do
    if ss -ltnp 2>/dev/null | grep -q ":$PORT "; then sleep 1; else break; fi
  done
  mkdir -p "$REMOTE_HOME"
  ASTROLAB_HOME="$REMOTE_HOME" nohup "$REMOTE_VENV/bin/uvicorn" server.api:app \
    --host 0.0.0.0 --port "$PORT" --log-level info \
    > "$LOG_PATH" 2>&1 &
  disown
fi

# Wait for the port to start accepting connections so the script doesn't
# return success before the server is actually ready.
echo "▸ waiting for :$PORT to come up"
for i in $(seq 1 20); do
  if curl -sS -o /dev/null -w "%{http_code}" --max-time 2 \
       "http://127.0.0.1:$PORT/api/templates" | grep -q '^200$'; then
    echo "  up after ${i}s"
    break
  fi
  sleep 1
  if [[ "$i" == "20" ]]; then
    echo "  port $PORT didn't come up — last 40 lines of journal:" >&2
    sudo journalctl -u astrolab-api --no-pager -n 40 >&2 || tail -n 40 "$LOG_PATH" >&2 || true
    exit 1
  fi
done
REMOTE

ok "deployed — http://$HOST:$PORT/api/templates"
