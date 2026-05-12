<p align="center">
  <img src="ui/static/og-image.png" alt="astrolab" width="720" />
</p>

<p align="center">
  <em>Iterate-and-compare astrophotography stacking. Tweak a slider, see the result, undo, branch, publish.</em>
</p>

---

## What it is

astrolab is a local-first workbench that wraps Siril, GraXpert, and StarNet++ behind a typed pipeline graph and a content-addressed cache. Each "project" is a live edit of a stack: changing a knob in the UI submits a fresh job, but every upstream node it didn't touch (calibrate, register, stack) is a free cache hit, so re-renders feel cheap. Every revision shows up in a history strip; promote keepers to a public gallery.

It is built around the Dwarf 3 capture layout for now, but nothing in the pipeline is Dwarf-specific. Want your scope supported? See the [Adding a scope](CONTRIBUTING.md#adding-a-scope) section in CONTRIBUTING.md; it starts with running a small collector script, not uploading raw files.

## What it does

<!-- Screenshot: Library page with at least two targets in the list, one expanded to show multiple sessions. Dark mode, sessions sorted by name. -->
<img src="docs/screenshots/library.png" alt="Library page showing targets and sessions" width="720" />

- Wraps **Siril 1.4** for calibration, registration, stacking, and pixel ops.
- Drives **GraXpert** for background extraction and ML denoise.
- Drives **StarNet++ v2** for star removal, replacement, and recombination.
- Caches every node's output by `(inputs + params + node version)` so editing a downstream knob never re-runs an upstream node.
- Tracks a per-project edit history (revert, branch, compare, publish).
- Surfaces a **Tonight** planner with altitude/visibility for the targets you've already captured, so you know what's worth re-imaging.
- Gallery view surfaces the renders you opt in to publish; everything else stays in your private history.

<!-- Screenshot: Project page open on a finished stack, history strip at the bottom with at least two revisions visible. This is the iterate-and-compare core. -->
<img src="docs/screenshots/project.png" alt="Project page with history strip showing multiple revisions" width="720" />

<!-- Screenshot: Compare view with two revisions side by side or using the before/after slider. -->
<img src="docs/screenshots/compare.png" alt="Compare view with two revisions" width="720" />

<!-- Screenshot: Tonight planner with targets queued and rise/set bars visible. -->
<img src="docs/screenshots/tonight.png" alt="Tonight planner with targets queued" width="720" />

## Quick start

### Docker (recommended)

No local install needed. The image bundles Siril 1.4 and GraXpert 3.0.2.

**CPU-only (most users):**

```bash
docker run -d \
  --name astrolab \
  -p 8000:8000 \
  -v ~/Pictures/Siril:/captures:ro \
  -v astrolab-data:/data \
  ghcr.io/bscholer/astrolab:latest
```

Open `http://localhost:8000`. The container auto-scans `/captures` on first run, so your sessions should appear within a few seconds.

**NVIDIA GPU (for GraXpert and StarNet++ AI nodes):**

You need the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) installed on the host first.

```bash
docker run -d \
  --name astrolab \
  --gpus all \
  -p 8000:8000 \
  -v ~/Pictures/Siril:/captures:ro \
  -v astrolab-data:/data \
  -e ASTROLAB_ENABLE_STARNET=1 \
  ghcr.io/bscholer/astrolab:cuda
```

`ASTROLAB_ENABLE_STARNET=1` tells the entrypoint to download StarNet++ v2 on first run and store it in the persistent `/data` volume. Omit it if you don't need star removal.

**Mount points:**

| Path | Purpose |
|------|---------|
| `/captures` | Your raw capture tree (read-only is fine; astrolab never writes here) |
| `/data` | Persistent state: cache, projects, settings, optional StarNet++ binary |

---

### From source (dev / no Docker)

```bash
git clone https://github.com/bscholer/astrolab.git
cd astrolab

# python deps + venv (uv reads pyproject.toml)
uv sync

# UI deps
cd ui && npm install && cd ..

# run the API on :8000
.venv/bin/uvicorn server.api:app --reload --host 0.0.0.0 --port 8000

# in another terminal: vite dev server on :5173 (proxies /api to :8000)
cd ui && npm run dev
```

Open `http://localhost:5173`, point the captures path at your Siril folder in **Settings**, hit **Refresh**, and projects show up.

For a single-port setup mirroring production (`http://localhost:8000`), `cd ui && npm run build` once instead.

## Hardware + OS support

The Docker image is the supported runtime. It's amd64-only and runs anywhere Docker does (Linux, macOS via Docker Desktop, Windows via WSL2).

- **CPU only** (the `:latest` and `:cpu` tags) works on any amd64 box. GraXpert and StarNet++ fall back to CPU; stacking is fine, AI nodes are noticeably slower.
- **NVIDIA GPU** (the `:cuda` tag, plus `--gpus all` and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)) is recommended if you want GraXpert and StarNet++ to fly. Tested on Ampere and Ada.
- **From source** (Linux only, see Quick Start above) works for dev. macOS source installs run everything except the Siril paths; the test suite mocks the Siril subprocess so the dev loop stays full-featured, but final validation belongs on a Linux box.

### Apple Silicon caveat

The image is amd64; on M-series Macs it runs through Rosetta. Siril, the calibrate / register / stack / save_image pipeline all work fine under Rosetta. **GraXpert and StarNet++ do not** — their PyInstaller-bundled Python stack hits a Rosetta amd64 emulation bug and segfaults at startup. Astrolab's pipeline will therefore crash on those nodes when run on Apple Silicon via Docker Desktop. The Siril-only path through `stack` works end to end and produces a usable PNG. For full pipelines with GraXpert / StarNet, run on a Linux or amd64-native host.

## Status

Phase 0. Not stable, not packaged, schemas can change between commits. Useful enough to process my own captures every weekend; rough enough that I'd hate to ship it as v1.0.0 today. The [V1 milestone](https://github.com/bscholer/astrolab/milestone/1) tracks the punch list.

## Built on

The actual image processing is done by external tools that astrolab orchestrates:

- **[Siril](https://siril.org)** (GPLv3) - calibration, registration, stacking, plate-solving.
- **[GraXpert](https://www.graxpert.com)** (GPLv3) - background gradient extraction and ML denoise.
- **[StarNet++ v2](https://www.starnetastro.com)** (proprietary freeware) - star-nebula separation.

The `calibrate_register_stack` template follows the pipeline shape from [Naztronomy's smart-telescope script](https://github.com/naztronaut/siril-scripts/blob/main/Naztronomy-Smart_Telescope_PP.py). No code is copied; astrolab reimplements the same Siril command sequence as graph nodes so each step is cacheable. Stage order is his.

The multi-scope ingest classifier (NINA, ZWO ASIAIR, Seestar) and the cross-scope filter alias table were derived clean-room from public header dumps in [starbash](https://github.com/geeksville/starbash) (Kevin Hester, GPLv3). No starbash code is in astrolab; the mappings are factual observations about how each capture program writes its FITS headers.

Full notes in [LICENSES/THIRD_PARTY.md](LICENSES/THIRD_PARTY.md).

## Links

- [Design doc](docs/design.md), the original README. Pipeline contracts, cache shape, node authoring guide.
- [Contributing](CONTRIBUTING.md). Dev setup, test/lint commands, "add a node" walkthrough.
- [Issue tracker](https://github.com/bscholer/astrolab/issues). [V1 board](https://github.com/users/bscholer/projects).

## License

MIT, with a CC BY-SA 4.0 carve-out for the vendored OpenNGC catalog under `catalogs/openngc/` (Mattia Verga). See [LICENSE](LICENSE) for both.

The Docker image bundles Siril (GPLv3) and GraXpert (GPLv3) as separate processes; astrolab's MIT license applies only to astrolab's own source code. See [LICENSES/THIRD_PARTY.md](LICENSES/THIRD_PARTY.md) for details.
