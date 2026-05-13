#!/usr/bin/env bash
# Idempotent install of astrolab's systemd units. Copies the .service files
# from scripts/systemd/ into /etc/systemd/system/, sets up log files writable
# by bscholer, and enables both units so they auto-start on boot.
#
# Usage:  scripts/install-systemd-units.sh
#         scripts/install-systemd-units.sh --remote   # ssh + run on Linux box

set -euo pipefail

step() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--remote" ]]; then
  HOST="${ASTROLAB_LINUX_HOST:-192.168.1.254}"
  REMOTE_REPO="${ASTROLAB_LINUX_REPO:-/home/bscholer/projects/astrolab}"
  step "ssh $HOST: running systemd-units installer in $REMOTE_REPO"
  exec ssh -t "$HOST" "cd $REMOTE_REPO && bash scripts/install-systemd-units.sh"
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
units_src="$repo_dir/scripts/systemd"

if [[ ! -d "$units_src" ]]; then
  echo "missing $units_src" >&2
  exit 1
fi

step "copying units into /etc/systemd/system/"
sudo cp "$units_src"/astrolab-api.service /etc/systemd/system/
sudo cp "$units_src"/astrolab-worker.service /etc/systemd/system/

step "preparing log files"
sudo touch /var/log/astrolab-api.log
sudo chown "$USER:$USER" /var/log/astrolab-api.log
sudo touch /var/log/astrolab-worker.log
sudo chown "$USER:$USER" /var/log/astrolab-worker.log

# Clean up the legacy vite-dev unit if it's still around. FastAPI now
# serves the prebuilt UI directly on :8000 — no separate node process.
if systemctl list-unit-files astrolab-ui.service >/dev/null 2>&1; then
  step "removing legacy astrolab-ui (vite dev) unit"
  sudo systemctl disable --now astrolab-ui 2>/dev/null || true
  sudo rm -f /etc/systemd/system/astrolab-ui.service
fi

step "daemon-reload + enable + start"
sudo systemctl daemon-reload
sudo systemctl enable --now astrolab-api
sudo systemctl enable --now astrolab-worker

sleep 3
echo
step "status:"
systemctl is-active astrolab-api
systemctl is-active astrolab-worker
echo
step "ports:"
ss -ltnp 2>/dev/null | grep -E ":8000 " || true

ok "installed. astrolab: http://$(hostname -I | awk '{print $1}'):8000/"
