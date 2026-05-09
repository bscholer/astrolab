#!/usr/bin/env bash
# Idempotent installer for the AI processing tools astrolab shells out to:
#   - GraXpert (open source, AppImage-style zip on GitHub releases)
#   - StarNet++ (closed-source binary, manual download required)
#
# Layout:
#   $ASTROLAB_TOOLS_DIR (default $HOME/tools)
#     graxpert/
#       graxpert            # binary or symlink (the CLI entry point)
#     starnet/
#       starnet++           # the StarNet binary
#       *.pb                # the AI model files starnet++ needs at runtime
#
# After a successful run, the corresponding node will find the binary either
# via $ASTROLAB_GRAXPERT_BIN / $ASTROLAB_STARNET_BIN (explicit override) or
# via the layout above.
#
# Usage:  scripts/install-tools-linux.sh
#         scripts/install-tools-linux.sh --remote     # ssh + run on Linux box

set -euo pipefail

step() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

# Run remotely if requested; otherwise execute locally.
if [[ "${1:-}" == "--remote" ]]; then
  HOST="${ASTROLAB_LINUX_HOST:-192.168.1.254}"
  REMOTE_REPO="${ASTROLAB_LINUX_REPO:-/home/bscholer/projects/astrolab}"
  step "ssh $HOST: running installer in $REMOTE_REPO"
  exec ssh -t "$HOST" "cd $REMOTE_REPO && bash scripts/install-tools-linux.sh"
fi

TOOLS_DIR="${ASTROLAB_TOOLS_DIR:-$HOME/tools}"
mkdir -p "$TOOLS_DIR"

# ---------- GraXpert ---------------------------------------------------------
GRAXPERT_VERSION="${GRAXPERT_VERSION:-3.0.2}"
GRAXPERT_ZIP_URL="https://github.com/Steffenhir/GraXpert/releases/download/${GRAXPERT_VERSION}/graxpert-linux-amd64.zip"
GRAXPERT_DIR="$TOOLS_DIR/graxpert"
GRAXPERT_BIN="$GRAXPERT_DIR/graxpert"

if [[ -x "$GRAXPERT_BIN" ]]; then
  ok "GraXpert already at $GRAXPERT_BIN"
else
  step "downloading GraXpert ${GRAXPERT_VERSION}"
  mkdir -p "$GRAXPERT_DIR"
  tmpzip="$(mktemp --tmpdir graxpert.XXXXXX.zip)"
  trap 'rm -f "$tmpzip"' EXIT
  curl -fL --progress-bar -o "$tmpzip" "$GRAXPERT_ZIP_URL"
  step "extracting into $GRAXPERT_DIR"
  unzip -q -o "$tmpzip" -d "$GRAXPERT_DIR"
  rm -f "$tmpzip"
  trap - EXIT

  # The release zip extracts to GraXpert-linux/ (or sometimes flat). The
  # binary inside is 'GraXpert' (capital G). Normalize the entry point to
  # a stable lowercase name 'graxpert' the node looks for.
  if [[ -x "$GRAXPERT_DIR/GraXpert-linux/GraXpert" ]]; then
    ln -sf GraXpert-linux/GraXpert "$GRAXPERT_BIN"
  elif [[ -x "$GRAXPERT_DIR/GraXpert" ]]; then
    ln -sf GraXpert "$GRAXPERT_BIN"
  elif [[ -x "$GRAXPERT_DIR/graxpert" ]]; then
    : # already named graxpert, nothing to do
  else
    err "GraXpert zip didn't contain an executable I recognize. Contents:"
    ls -la "$GRAXPERT_DIR" >&2
    exit 1
  fi
  ok "GraXpert at $GRAXPERT_BIN -> $(readlink -f "$GRAXPERT_BIN")"
fi

# ---------- StarNet++ --------------------------------------------------------
# StarNet++ is closed-source and the author distributes via a download form
# on https://www.starnetastro.com/. We can't fetch automatically. The node
# only needs the binary on $PATH or at the configured location, so we just
# verify and print install instructions if it's missing.
STARNET_DIR="$TOOLS_DIR/starnet"
STARNET_BIN="$STARNET_DIR/starnet++"

if [[ -x "$STARNET_BIN" ]]; then
  ok "StarNet++ already at $STARNET_BIN"
else
  warn "StarNet++ is not installed. Manual steps:"
  cat <<EOF
    1. Open https://www.starnetastro.com/ and download the Linux package.
    2. Extract the contents into $STARNET_DIR/
       - You should end up with $STARNET_BIN (the binary) plus the .pb
         model files in the same directory.
    3. chmod +x $STARNET_BIN

    The starnet_extract node will fall back to a pass-through (and log
    a clear error) until this finishes.
EOF
fi

ok "done. Set ASTROLAB_GRAXPERT_BIN / ASTROLAB_STARNET_BIN to override paths."
