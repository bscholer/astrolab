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
# Delegates the actual work to scripts/deploy-on-box.sh, which is the same
# script the auto-deploy webhook (astrolab-deploy.service) runs. Keeping
# both paths through one script means manual and automated deploys can't
# drift.
step "ssh $HOST: scripts/deploy-on-box.sh"
ssh -o ConnectTimeout=10 "$HOST" \
  ASTROLAB_REPO="$REMOTE_REPO" \
  ASTROLAB_API_PORT="$PORT" \
  bash "$REMOTE_REPO/scripts/deploy-on-box.sh"

ok "deployed — http://$HOST:$PORT/api/templates"
