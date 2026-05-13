# syntax=docker/dockerfile:1
# Multi-stage Dockerfile for astrolab.
#
# Build args
#   BASE  debian:bookworm-slim          → CPU image (default, tagged :latest/:cpu)
#         nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04  → CUDA image (tagged :cuda)
#
# Build examples
#   docker build --build-arg BASE=debian:bookworm-slim -t astrolab:cpu .
#   docker build --build-arg BASE=nvidia/cuda:12.4.1-runtime-ubuntu22.04 -t astrolab:cuda .

ARG BASE=debian:bookworm-slim


# ---------------------------------------------------------------------------
# Stage 1: build the SvelteKit UI
# ---------------------------------------------------------------------------
FROM node:20-slim AS ui-builder

WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
RUN npm run build
# SvelteKit adapter-static lands the production build at ui/build/


# ---------------------------------------------------------------------------
# Stage 2: fetch and extract third-party binaries
# ---------------------------------------------------------------------------
# Must run as linux/amd64 so the x86_64 Siril AppImage can self-extract
# (AppImages are ELF binaries; running them on the wrong arch fails).
# On Apple Silicon / arm64 Docker Desktop this triggers a QEMU emulation layer.
FROM --platform=linux/amd64 debian:bookworm-slim AS tools-fetcher

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        squashfs-tools \
        binutils \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch

# --- Siril 1.4 AppImage (extracted via unsquashfs; no FUSE, no runtime exec) -
# Using Siril 1.4.3 — latest stable in the 1.4 series (siril.py MIN_VERSION=(1,4)).
# 1.4.3 fixes the use-after-free + double-free in prepare_venv_environment
# (src/io/siril_pythonmodule.c, upstream commit a07edcd0) that corrupts the
# GLib slab allocator on Siril startup whenever the embedded sirilpy install
# fails. That was the root cause of every "siril exited -11" we saw on 1.4.2.
# Hosted on free-astro.org; the GitLab releases page does not provide AppImages.
# We use unsquashfs -offset to extract without running the AppImage ELF stub,
# which avoids FUSE dependency and cross-arch execution issues.
ARG SIRIL_VERSION=1.4.3
ARG SIRIL_URL=https://free-astro.org/download/Siril-1.4.3-x86_64.AppImage

RUN curl -fL --progress-bar -o Siril.AppImage "$SIRIL_URL" && \
    # AppImage Type 2: the squashfs superblock starts immediately after the ELF
    # binary (section header table end). Compute the byte offset from ELF headers
    # so we can pass -offset to unsquashfs (avoids running the ELF stub).
    printf '%s\n' \
      'import struct' \
      'f = open("Siril.AppImage","rb")' \
      'f.seek(40); shoff = struct.unpack("<Q", f.read(8))[0]' \
      'f.seek(58); shentsize, shnum = struct.unpack("<HH", f.read(4))' \
      'print(shoff + shentsize * shnum)' \
      > /fetch/offset.py && \
    OFFSET=$(python3 /fetch/offset.py) && \
    echo "squashfs offset: $OFFSET bytes" && \
    unsquashfs -offset "$OFFSET" -d /opt/siril Siril.AppImage && \
    rm Siril.AppImage /fetch/offset.py

# --- GraXpert 3.0.2 (same version and URL as install-tools-linux.sh) --------
ARG GRAXPERT_VERSION=3.0.2
ARG GRAXPERT_URL=https://github.com/Steffenhir/GraXpert/releases/download/3.0.2/graxpert-linux-amd64.zip

RUN curl -fL --progress-bar -o graxpert.zip "$GRAXPERT_URL" && \
    unzip -q graxpert.zip -d /opt/graxpert-raw && \
    rm graxpert.zip && \
    # Normalize: the zip may extract to GraXpert-linux/ with a 'GraXpert' binary.
    if [ -x /opt/graxpert-raw/GraXpert-linux/GraXpert ]; then \
        mv /opt/graxpert-raw/GraXpert-linux /opt/graxpert; \
        ln -sf GraXpert /opt/graxpert/graxpert; \
    elif [ -x /opt/graxpert-raw/GraXpert ]; then \
        mv /opt/graxpert-raw /opt/graxpert; \
        ln -sf GraXpert /opt/graxpert/graxpert; \
    else \
        mv /opt/graxpert-raw /opt/graxpert; \
    fi

# Fetch GraXpert license for attribution; Siril's is already in the AppDir.
# Pinned to the release tag we ship (3.0.2) so the build is reproducible.
RUN curl -fsSL -o /opt/graxpert-LICENSE \
        "https://raw.githubusercontent.com/Steffenhir/GraXpert/${GRAXPERT_VERSION}/License.md" \
    || curl -fsSL -o /opt/graxpert-LICENSE \
        https://raw.githubusercontent.com/Steffenhir/GraXpert/develop/License.md


# ---------------------------------------------------------------------------
# Stage 3: runtime image
# ---------------------------------------------------------------------------
FROM ${BASE} AS runtime

LABEL org.opencontainers.image.title="astrolab" \
      org.opencontainers.image.description="Local-first astrophotography workbench wrapping Siril, GraXpert, and StarNet++ behind a typed pipeline graph and a content-addressed cache." \
      org.opencontainers.image.source="https://github.com/bscholer/astrolab" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.bundled-software="Siril 1.4 (GPLv3, https://siril.org); GraXpert 3.0.2 (GPLv3, https://www.graxpert.com); StarNet++ v2 optional at runtime (proprietary freeware, https://www.starnetastro.com)"

# Runtime libs Siril's AppDir needs. Determined from ldd on the extracted
# AppDir's siril-cli binary; only packages not already bundled in the AppDir
# are listed here.
#
# Package soname suffixes differ between Debian 12 (bookworm) and the CUDA
# image's Ubuntu 22.04 (jammy) base. Dispatch on /etc/os-release.
RUN set -eu; \
    apt-get update -qq; \
    . /etc/os-release; \
    # Shared across both bases.
    common="libc6 libgcc-s1 libstdc++6 libgsl27 libfftw3-double3 \
            libfftw3-single3 libgomp1 libexiv2-27 libheif1 libraw20 libwcs7 \
            libglib2.0-0 libgl1 libxrender1 libxext6 libxft2 libfontconfig1 \
            libsm6 libcurl4 ca-certificates curl unzip"; \
    case "${ID}-${VERSION_CODENAME}" in \
        debian-bookworm) extra="libcfitsio10 libopencv-core406" ;; \
        ubuntu-jammy)    extra="libcfitsio9  libopencv-core4.5d" ;; \
        *) echo "unsupported base ${ID}-${VERSION_CODENAME}" >&2; exit 1 ;; \
    esac; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        ${common} ${extra}; \
    rm -rf /var/lib/apt/lists/*

# CUDA-only: install cuDNN 8 from NVIDIA's archive. GraXpert and StarNet++
# ship onnxruntime / TensorFlow built against cuDNN 8.x; the cudnn-runtime
# CUDA base ships cuDNN 9, so we use the plain `runtime` base (~3 GB smaller)
# and install cuDNN 8 directly. Skip on the Debian base (no GPU, no need).
RUN set -eu; \
    . /etc/os-release; \
    if [ "${ID}-${VERSION_CODENAME}" = "ubuntu-jammy" ]; then \
        echo "installing cuDNN 8 for GraXpert / StarNet onnxruntime"; \
        tmp="$(mktemp /tmp/cudnn8.XXXXXX.deb)"; \
        curl -fL --silent --show-error -o "$tmp" \
            "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/libcudnn8_8.9.7.29-1+cuda12.2_amd64.deb"; \
        dpkg -i "$tmp"; \
        rm -f "$tmp"; \
    else \
        echo "skipping cuDNN 8 install (not on CUDA base)"; \
    fi

# Install uv via the standalone binary download (no installer script HOME issues)
ARG UV_VERSION=0.5.26
RUN curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz --strip-components=1 -C /usr/local/bin uv-x86_64-unknown-linux-musl/uv

# Python deps via uv (uv manages its own Python; frozen, no dev extras)
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --python 3.12

# Third-party tools from the fetcher stage
COPY --from=tools-fetcher /opt/siril /opt/siril
COPY --from=tools-fetcher /opt/graxpert /opt/graxpert

# License files
RUN mkdir -p /opt/licenses/siril /opt/licenses/graxpert
# Siril's license lives inside the AppDir at usr/share/doc/siril/LICENSE.md
COPY --from=tools-fetcher /opt/siril/usr/share/doc/siril/LICENSE.md /opt/licenses/siril/LICENSE.md
COPY --from=tools-fetcher /opt/graxpert-LICENSE /opt/licenses/graxpert/LICENSE
COPY LICENSES/ /opt/licenses/astrolab/

# Application sources
COPY server/ ./server/
COPY nodes/ ./nodes/
COPY templates/ ./templates/
COPY profiles/ ./profiles/
COPY catalogs/ ./catalogs/

# Pre-built UI
COPY --from=ui-builder /ui/build ./ui/build/

# Entrypoint
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runtime environment
# ASTROLAB_CAPTURE_ROOT_DEFAULT seeds the capture_root setting on first run
# if it isn't set yet, and triggers an initial library scan. Matches the
# captures mount point in the README quick start so onboarding is one command.
ENV SIRIL_BIN=/opt/siril/AppRun \
    ASTROLAB_GRAXPERT_BIN=/opt/graxpert/graxpert \
    ASTROLAB_STARNET_BIN=/data/tools/starnet/starnet++ \
    ASTROLAB_HOME=/data \
    ASTROLAB_CAPTURE_ROOT_DEFAULT=/captures \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "server.api:app", "--host", "0.0.0.0", "--port", "8000"]
