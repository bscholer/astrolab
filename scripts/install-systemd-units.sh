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
sudo cp "$units_src"/astrolab-ui.service /etc/systemd/system/

step "preparing log files"
sudo touch /var/log/astrolab-api.log /var/log/astrolab-ui.log
sudo chown "$USER:$USER" /var/log/astrolab-api.log /var/log/astrolab-ui.log

step "daemon-reload + enable + start"
sudo systemctl daemon-reload
sudo systemctl enable --now astrolab-api astrolab-ui

sleep 4
echo
step "status:"
systemctl is-active astrolab-api astrolab-ui
echo
step "ports:"
ss -ltnp 2>/dev/null | grep -E ":(8000|5173) " || true

ok "installed. UI: http://$(hostname -I | awk '{print $1}'):5173/"
