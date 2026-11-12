# astrolab

Astrophotography workbench: a FastAPI server (`server/`) with a SvelteKit UI (`ui/`), a
pipeline runtime with nodes (`nodes/`), a capture catalog, and control of a DWARF 3 smart
telescope through a relay service. CONTRIBUTING.md covers setup, nodes, and style in depth.

## Commands

- Python is 3.12 (`.python-version`, same as the Docker image). `uv sync --extra dev` once.
- Tests: `uv run pytest -n 8` (about 90 s). Lint: `uv run ruff check server tests`.
- After any API change: `uv run python -m scripts.export_spec`, commit `docs/openapi.json`
  and `docs/mcp-tools.json`. A test fails when they are stale.
- UI: `cd ui && npm ci && npm run build`. Type check: `npx svelte-kit sync && npx svelte-check`.

## Shipping

- Push to `main` builds the image (CUDA, about 3.5 GB, 10+ minutes), then CI bumps the digest
  in `bscholer/homelab-config` and Argo CD rolls it out. The Deployment uses Recreate
  (one RWO volume, one GPU), so every rollout has a short outage, longer when the image
  must be pulled. Changes to the homelab chart roll the pod on their own, too.
- `scripts/dev-push.sh [--ui] [--worker]` copies the working tree into the running pod and
  uvicorn reloads in seconds (reload is on while `/data/dev-reload` exists). Not for
  dependency changes: the image lacks new packages and the API would fail to start.
  A dev push restarts the API, which drops in-memory scope state and interrupts the scheduler.
- Staging (`astrolab-staging` namespace) runs the same image against the fake scope below.
  Try risky scope changes there first.

## Reaching things from the claude-code pod

- `kubectl -n astrolab exec deploy/astrolab -- ...` works; the API is at the pod IP on 8000
  (`kubectl -n astrolab get pod -o jsonpath='{.items[0].status.podIP}'`). The Service name
  does not answer from this pod; use the pod IP.
- The relay source is `bscholer/dwarf3-control` (clone it to read the API). The relay runs
  in the `dwarf3-relay` namespace on the host network.
- Traces: OTLP to `otel-collector.tracing:4318`, Tempo behind it. Services `astrolab` (API)
  and `astrolab-worker` (pipeline jobs, a span per node).

## The scope, facts that cost time to learn

- The relay API is versioned (`/api/capabilities`, spec 0.1); astrolab routes operations
  by `operationId` from the relay's `/openapi.json` (`server/scope_service.py`).
- Pitch 0 is the closed barrel, 172.5 straight up. The real scope sits on a tilted
  (equatorial) mount on a fence, so motor angles do not map simply to alt/az.
- A frame the scope could not register stars in is saved as `failed_*.fits` and counted in
  the job's `failed`. The scheduler's rejected-frame streak and the sky map rely on it.
- Tele frames from the RTSP stream wait for a keyframe, about every 40 s, so `getFrame` on
  the tele can take over a minute. Wide frames are quick when the camera is streaming.
- FITS `DATE-OBS` on the scope is local time, not UTC.
- The wide camera's photos do not follow a simple pinhole model on this mount; projecting
  scan pixels onto the sky failed (see git history for the drawn-horizon attempt). Only a
  frame's middle lands reliably.

## Fake scope and simulated nights

`server/fakescope/` is a pretend DWARF 3 with a seeded synthetic yard (house, trees, fence)
and weather. It serves the relay's API (`python -m server.fakescope`, port 9902) and can
run astrolab's real scheduler for many nights in seconds (`server/fakescope/sim.py`):

```python
from server.fakescope.sim import run_nights
report = run_nights(Scene(seed=4), targets, start, nights=5)
report.nights, report.skymap_accuracy()
```

Use it to check scheduler, sky map, or capture changes before they meet the real scope.
`tests/test_fakescope.py` has examples. A night takes about 10 s at one-minute ticks.

## Working agreements

- Check ideas against real data before building UI: pull a sample with
  `kubectl exec ... tar` and look at it. The drawn-horizon detour came from skipping this.
- Commit messages: short imperative subject, plain-language body.
