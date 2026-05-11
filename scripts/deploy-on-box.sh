#!/usr/bin/env bash
# On-box deploy: pull main, sync deps, build the UI, restart the API.
#
# Runs entirely locally on the linux processing box. Two callers:
#   1. astrolab-deploy.service (oneshot triggered by the FastAPI webhook
#      at /api/_deploy after CI succeeds and pushes to main).
#   2. scripts/deploy-linux.sh (manual deploy from a dev machine, which
#      ssh's in and execs this script directly).
#
# Idempotent. Exits non-zero on any failure; the systemd unit logs go to
# /var/log/astrolab-deploy.log.

set -euo pipefail

REPO="${ASTROLAB_REPO:-/home/bscholer/projects/astrolab}"
PORT="${ASTROLAB_API_PORT:-8000}"

cd "$REPO"

echo "▸ git pull"
git fetch --quiet origin
git pull --ff-only origin "$(git rev-parse --abbrev-ref HEAD)"

# uv-managed venv. ~/.local/bin is dropped from non-interactive ssh
# shells (login configs run for interactive sessions only), so prepend
# it explicitly. Skipping uv sync once shipped a starnet_extract revision
# the venv couldn't import (missing tifffile), so this is non-optional.
export PATH="$HOME/.local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  echo "▸ uv sync"
  uv sync --frozen 2>/dev/null || uv sync
fi

# Rebuild the SvelteKit UI. astrolab-api serves it from ui/build/ on the
# same port, so backend + UI restart in lockstep.
if command -v npm >/dev/null 2>&1; then
  echo "▸ npm run build (ui)"
  cd ui
  npm install --silent
  npm run build --silent
  cd "$REPO"
fi

echo "▸ restarting astrolab-api via systemctl"
# astrolab-api.service runs uvicorn under systemd. We're invoked from the
# astrolab-deploy.service cgroup (or from an interactive ssh session),
# both of which are separate cgroups from astrolab-api, so restarting
# astrolab-api here doesn't kill our own process.
#
# We deliberately do NOT restart astrolab-worker here: the whole point of
# the API/worker split is that a deploy must not SIGTERM in-flight Siril
# subprocesses. To pick up new worker code, run one of these manually:
#   sudo systemctl reload  astrolab-worker   # drain + exit, restart on idle
#   sudo systemctl restart astrolab-worker   # drain + restart on new code
if systemctl list-unit-files astrolab-api.service >/dev/null 2>&1 \
     && systemctl is-enabled --quiet astrolab-api 2>/dev/null; then
  sudo systemctl restart astrolab-api
  if systemctl list-unit-files astrolab-ui.service >/dev/null 2>&1; then
    sudo systemctl disable --now astrolab-ui 2>/dev/null || true
  fi
else
  echo "  astrolab-api.service not installed; skipping restart"
  exit 1
fi

# Heads-up if the worker unit is installed but not running. We don't
# restart it (see comment above), but a deploy that lands schema or
# protocol changes the worker hasn't picked up yet is worth flagging.
if systemctl list-unit-files astrolab-worker.service >/dev/null 2>&1 \
     && ! systemctl is-active --quiet astrolab-worker 2>/dev/null; then
  echo "  ⚠ astrolab-worker.service exists but is not active; queued jobs won't run"
  echo "    sudo systemctl start astrolab-worker"
fi

echo "▸ waiting for :$PORT to come up"
for i in $(seq 1 20); do
  if curl -sS -o /dev/null -w "%{http_code}" --max-time 2 \
       "http://127.0.0.1:$PORT/api/templates" | grep -q '^200$'; then
    echo "  up after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "  port $PORT didn't come up" >&2
sudo journalctl -u astrolab-api --no-pager -n 40 >&2 || true
exit 1
