#!/usr/bin/env bash
# astrolab container entrypoint.
#
# Responsibilities:
#   1. Ensure /data subdirs exist and are writable.
#   2. Optionally fetch StarNet++ v2 if ASTROLAB_ENABLE_STARNET=1 and the
#      binary is not already present. Failure is non-fatal; the app starts
#      regardless, and star-removal nodes will surface a clear error if used.
#   3. exec into uvicorn (or whatever CMD was passed).

set -euo pipefail

step() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. Runtime directories under /data
# ---------------------------------------------------------------------------
step "ensuring /data layout"
mkdir -p /data/cache /data/projects /data/tools
ok "/data ready"

# ---------------------------------------------------------------------------
# 2. Optional StarNet++ download
# ---------------------------------------------------------------------------
STARNET_BIN="${ASTROLAB_STARNET_BIN:-/data/tools/starnet/starnet++}"
STARNET_DIR="$(dirname "$STARNET_BIN")"
STARNET_ZIP_URL="https://starnetastro.com/wp-content/uploads/2022/03/StarNetv2CLI_linux.zip"

if [[ "${ASTROLAB_ENABLE_STARNET:-0}" == "1" ]]; then
    if [[ -x "$STARNET_BIN" ]]; then
        ok "StarNet++ already present at $STARNET_BIN"
    else
        step "downloading StarNet++ v2 (ASTROLAB_ENABLE_STARNET=1)"
        mkdir -p "$STARNET_DIR"
        tmpzip="$(mktemp /tmp/starnet.XXXXXX.zip)"
        if curl -fL --progress-bar -o "$tmpzip" "$STARNET_ZIP_URL"; then
            unzip -q -o "$tmpzip" -d "$STARNET_DIR"
            rm -f "$tmpzip"
            # Some zips nest under StarNetv2CLI_linux/, others extract flat. Flatten.
            if [[ -d "$STARNET_DIR/StarNetv2CLI_linux" ]]; then
                mv "$STARNET_DIR/StarNetv2CLI_linux/"* "$STARNET_DIR/"
                rmdir "$STARNET_DIR/StarNetv2CLI_linux"
            fi
            chmod +x "$STARNET_BIN" 2>/dev/null || true
            if [[ -x "$STARNET_BIN" ]]; then
                ok "StarNet++ installed at $STARNET_BIN"
            else
                err "StarNet++ extracted but no binary at $STARNET_BIN — star removal nodes will fail"
            fi
        else
            rm -f "$tmpzip"
            warn "could not fetch StarNet++ from $STARNET_ZIP_URL"
            warn "star removal nodes will fail until the binary is placed at $STARNET_BIN"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. Hand off to the process
# ---------------------------------------------------------------------------
exec "$@"
