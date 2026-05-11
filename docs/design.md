# astrolab design doc

A local-first astrophotography processing workbench. Wraps Siril (and friends) behind a typed DAG, a content-addressed cache, and a phone-friendly web UI so the loop "tweak a knob, see what happens" is fast enough to actually iterate on.

This is the working design doc; for an introduction and install path see the [README](../README.md).

### Credit

astrolab is an orchestration layer, not an image processor. The actual number-crunching is done by three tools that each deserve direct credit:

- **[Siril](https://siril.org)** (GPLv3) by Cyril Richard and the Free Astronomy team. Calibration, registration, stacking, plate-solving.
- **[GraXpert](https://www.graxpert.com)** (GPLv3) by Steffen Hirtle. ML background extraction and denoise.
- **[StarNet++ v2](https://www.starnetastro.com)** (proprietary freeware) by Nikita Misiura. Star-nebula separation.

Full attribution and license notes are in [LICENSES/THIRD_PARTY.md](../LICENSES/THIRD_PARTY.md).

---

## Vision

Today the workflow is: capture frames, copy them somewhere, open Siril, click through a script, wait, eyeball, tweak, click through again. Stacking is slow and expensive; post-processing tweaks are cheap but locked behind the slow part. Calibration frame matching is ad-hoc. Experiments are not reproducible; the params that produced any given image live in your head.

astrolab inverts this:

- A **library** that knows about every frame on the NAS and matches calibration to lights automatically.
- A **pipeline** modeled as a typed DAG of cacheable nodes. Stacking is just one (expensive) node; stretches, palette recombines, denoise, etc. are cheap downstream nodes.
- A **content-addressed cache** keyed by `(inputs + params + node version)`. Re-running with a tweaked stretch never re-stacks.
- A **web UI** you reach from any device on the LAN. Phone-tweak from the couch.
- **Renders** as first-class artifacts: a saved render is a (template, params, input refs) tuple. Reproducible, comparable, shareable.

---

## Goals (and non-goals)

### Goals
- Single-user, local-first, no cloud.
- Multiple smart-telescope flows out of the box (Seestar S30/S50, Dwarf 2/3, Celestron Origin), all on the same engine.
- Support OSC broadband, OSC narrowband (Ha/OIII split → recombine), and mosaic flows.
- Fast iteration on **post-stacking** params (palette, stretch, sat, starnet on/off) with sub-second preview.
- Reproducible: any saved render can be re-derived from its inputs + recipe.
- Phone-usable for the iteration loop.

### Non-goals (for now)
- Not a Siril replacement. Siril does the heavy lifting; we orchestrate.
- No multi-user, no auth beyond LAN/Tailscale-trust.
- No real-time during-capture processing (live stacking is out of scope; we work on completed sessions).
- No fancy ML pipelines beyond shelling out to Starnet++ / similar.
- No mobile app; responsive web is enough.

---

## Core concepts

### 1. Pipeline = typed DAG of nodes

A node is a pure-ish function: `(input_refs, params) → output_refs`. "Pure-ish" because it may shell out to Siril/Starnet, but for the same `(inputs, params, node_version)` it must produce equivalent outputs.

Each node declares:
- **Input ports**: typed (e.g. `Sequence[FITS]`, `Image[FITS]`, `Master[FITS]`, `ChannelTriple`).
- **Output ports**: typed.
- **Params**: a Pydantic schema with defaults.
- **Version**: bumped on behavior change → invalidates cache.
- **Cost class**: `cheap` (pixel ops, ms to seconds), `medium` (ML inference, seconds to minutes), `expensive` (stack, drizzle, minutes to hours). Drives UI affordances (auto-rerun on cheap, button on expensive).

Edges connect output ports to compatible input ports. The DAG runner topologically sorts, checks the cache, runs only dirty nodes.

### 2. Polymorphic nodes (variant slots)

Some "nodes" are really a slot accepting one of N implementations. Example: `Stretch` slot accepts `AutoStretch | GHS | Asinh | HistEq`. Each variant has its own param schema and version.

In templates, this looks like:

```yaml
- id: stretch_main
  kind: stretch
  variant: ghs
  params: { D: 1.5, b: 0.25, SP: 0.0 }
```

In the UI, the variant is a dropdown; the params panel re-renders for the chosen variant. This handles the "I want to try a different op here" instinct without graph editing.

### 3. Templates (pipelines as files)

A pipeline is a YAML file: nodes + edges + default params + scope-profile reference. Forking = copying the file. One template ships with MVP (the existing Naztronomy flow); more come later.

Templates are source-controlled artifacts, not DB rows. Hand-editable. The UI loads them from a directory.

### 4. Scope profiles

Scope-specific knowledge lives in profiles, not in nodes. A profile has two sections: pipeline-side defaults and library-side ingest rules.

**Pipeline defaults:**
- Default param overrides for nodes (`-oscfilter=...`, focal length, pixel size, narrowband wavelengths for dual-band filters like Dwarf 3's).
- Allowed filter list.
- Default templates / template recommendations.
- Quirks/workarounds (e.g. Seestar drizzle+BG-extract debayer bug).

**Ingest rules** (consumed by the catalog scope adapter):
- File path globs per frame type (`captures/<target>/lights/`, etc.); each scope's layout is different.
- Filename pattern → metadata mapping when FITS headers don't carry it.
- Default behavior when calibration frames don't exist for this scope (skip, fall back to library masters, or refuse).

Profiles are YAML. Adding a new scope = adding a profile (and possibly a small adapter module if path/filename rules need code).

### 5. Content-addressed cache

Every node output is written to `cache/<hash>/...` where hash = `sha256(node_id + version + sorted(input_hashes) + canonical(params))`. The cache is the DB of intermediate state. A "render" is just a pinned cache entry plus metadata.

Eviction: LRU by access time, with a `pinned` flag for things we never want to lose (stacked masters, named renders). Off by default; turn on later when disk pressure shows up.

### 6. Cost-aware execution

The graph runner classifies each dirty node by cost. Cheap nodes auto-run on param change (debounced). Expensive nodes require explicit confirmation ("Re-stack?"). This is what makes the slider-tweak loop feel instant: editing a stretch param re-runs only `stretch → palette_combine → preview`, not the upstream stack.

### 7. Preview vs final

A `preview_master` (e.g. 1024px long edge) is generated once after stacking and stored alongside the full master. Cheap downstream nodes run against both: preview for the live UI loop, full-res on demand for "render". A few ops are flagged `preview_approximate` (Starnet++, ML denoise, deconvolution); the UI surfaces this so you know to verify at full res.

---

## Architecture

Four logical subsystems, **one engine**.

```
┌──────────────────────────────────────────────────────┐
│                       Web UI                         │
│       (SvelteKit; library / templates / edit)        │
└────────────────┬───────────────────┬─────────────────┘
                 │ HTTP / WS         │
┌────────────────▼───────────────────▼─────────────────┐
│                    FastAPI server                    │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐  │
│  │  Catalog   │  │  Pipeline  │  │   Renders &    │  │
│  │  service   │  │  runtime   │  │   experiments  │  │
│  └─────┬──────┘  └─────┬──────┘  └────────┬───────┘  │
│        │               │                  │          │
│  ┌─────▼───────────────▼──────────────────▼───────┐  │
│  │  Storage: SQLite + content-addressed cache     │  │
│  │  + templates dir + profiles dir                │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
                    │           │
              ┌─────▼───┐  ┌────▼─────┐
              │   NAS   │  │  Local   │
              │ (frames)│  │   SSD    │
              └─────────┘  └──────────┘
```

### Catalog service
Indexes FITS frames on the NAS by reading headers, normalized through per-scope **ingest adapters**. Owns `frames`, `sessions`, `targets`, `masters`, `calibration_matches` tables. Periodic + on-demand scans.

Target display names come from two sources consulted in order: the OpenNGC `Common Name` column, then a curated fallback table at `server/catalog/data/common_names.json` (keyed by catalog id, e.g. `"NGC 7380": "Wizard Nebula"`). The curated file is the right place to add popular names that OpenNGC leaves blank. `server/catalog/common_names.py` exposes `lookup(name) -> str | None`; prefer it over direct JSON access.

Each smart telescope writes files into its own folder layout with its own naming conventions, and some don't save calibration frames at all. An ingest adapter handles that messiness so the core schema stays uniform. An adapter knows:

- **Path globs** for lights / darks / flats / biases (Dwarf 3 vs Seestar vs Celestron Origin all differ).
- **Filename → metadata** mapping when FITS headers are incomplete or non-standard. Path-derived target/session/filter is sometimes the only signal.
- **Frame-type taxonomy** for that scope. Some smart scopes never produce darks; "no calibration available" is a valid baseline, not an error.
- **Per-scope quirks**: pre-debayered files, non-standard `IMAGETYP` values, sub-session splits, multi-night session boundaries that don't match folder boundaries, etc.

Adding a new scope is "drop in an adapter + a profile YAML." No core-engine changes. Adapters live in `nodes/ingest/<scope_id>.py` (or as pure-data rules inside the profile when no code is needed).

### Pipeline runtime
Loads templates, builds a `Job` (template + param overrides + input refs), validates, hashes, runs the dirty subgraph through node implementations, writes outputs to cache. Streams progress over WebSocket.

### Renders & experiments
A render = `(template_id, full param tree, input refs, output cache hash, thumbnail, notes, tags)`. Stored in SQLite. Compare-side-by-side, fork-from-render, etc.

### Web UI
Views: **Library** (targets/sessions/calibration coverage, sortable by name/integration/recency), **Projects** (project detail with session list and freeform notes), **Tonight** (site-aware planner over OpenNGC, altitude sparklines), **Gallery**, **Compare**, **Settings**. Mobile-first responsive.

---

## Contracts

These are the load-bearing interfaces. Get these right, the rest is mechanical.

### Node interface (Python)

```python
class Node[ParamsT]:
    id: str                       # stable, used in cache key
    version: int                  # bump to invalidate cache
    cost: Literal["cheap", "medium", "expensive"]
    preview_approximate: bool = False

    inputs: dict[str, PortType]   # name -> type
    outputs: dict[str, PortType]  # name -> type
    params_schema: type[ParamsT]  # Pydantic model

    def run(
        self,
        inputs: dict[str, Ref],
        params: ParamsT,
        ctx: RunContext,
    ) -> dict[str, Ref]: ...
```

`Ref` is an opaque handle to a cache entry (a path under `cache/<hash>/`). `RunContext` provides:
- A scoped temp dir.
- A `siril()` callable for issuing Siril commands. Wraps a live sirilpy session inside a Siril subprocess; supports `cmd(...)`, pixel reads/writes, and structured progress. Linux-only at runtime (see Spike 01).
- A `progress(fraction, message)` callback.
- Logger.

Polymorphic nodes are a `Node` subclass `VariantNode` whose `params_schema` is a discriminated union over variants. The runtime selects the variant by the `variant` field.

### Template schema (YAML)

```yaml
# templates/hoo_dwarf3_dualband.yaml
id: hoo_dwarf3_dualband
version: 1
description: HOO recombine for Dwarf 3 with dual-band (Ha/OIII) filter
profile: dwarf3

nodes:
  - id: convert
    kind: convert_lights
  - id: calibrate
    kind: calibrate
    params: { use_darks: true, use_flats: true, use_biases: true }
  - id: bg_extract
    kind: bg_extract
    params: { samples: 10 }
  - id: platesolve
    kind: seq_platesolve
  - id: register
    kind: seq_register
    params: { drizzle: true, scale: 2.0, pixfrac: 1.0, kernel: square }
  - id: extract
    kind: extract_ha_oiii
  - id: stack_ha
    kind: seq_stack
    params: { rejection: "rej 3 3", norm: addscale, feather: 5 }
    inputs: { sequence: extract.ha }
  - id: stack_oiii
    kind: seq_stack
    params: { rejection: "rej 3 3", norm: addscale, feather: 5 }
    inputs: { sequence: extract.oiii }
  - id: align_results
    kind: align_pair
    inputs: { a: stack_ha.master, b: stack_oiii.master }
  - id: stretch
    kind: stretch
    variant: ghs
    params: { D: 1.5, b: 0.25 }
    inputs: { image: align_results.a }
  - id: palette
    kind: palette_combine
    params: { palette: HOO }
    inputs:
      ha: stretch.image
      oiii: align_results.b
  - id: preview
    kind: preview_png
    inputs: { image: palette.image }

outputs:
  final: palette.image
  preview: preview.png
```

Edge syntax: `inputs.<port>: <node_id>.<output_port>`. Implicit chain when not specified (each node's first input wires to the previous node's primary output); sugar to keep simple flows tidy.

### Job spec

```python
class Job(BaseModel):
    template_id: str
    template_version: int
    profile_id: str
    target_id: str
    session_ids: list[str]
    calibration: CalibrationSpec  # auto / explicit / none
    param_overrides: dict[str, dict]  # node_id -> partial params
    preview_only: bool = False
```

The runtime resolves the template + overrides + profile defaults into a fully-baked DAG, computes hashes per node, and executes the dirty subgraph.

### Cache key

```
node_hash = sha256(
    node.id || node.version ||
    sorted(input_refs_by_port) ||
    canonical_json(params)
)
```

Canonical-JSON params: keys sorted, floats rounded to a fixed precision per param schema (some params are float-noisy; pin tolerance in the schema). Outputs go to `cache/<node_hash>/<port_name>.<ext>`. Reverse index in SQLite: `(node_id, params_hash) -> node_hash` for lookup.

### Catalog schema (SQLite, sketch)

Authoritative definition: `server/catalog/db.py` migrations. Current schema version: 11.

```sql
-- indexes: object, image_type, session_key, (inode, mtime)
CREATE TABLE frames (
    id              INTEGER PRIMARY KEY,
    file_hash       TEXT,                    -- xxhash of contents
    path            TEXT NOT NULL UNIQUE,
    inode           INTEGER,
    mtime           REAL,
    size            INTEGER,
    image_type      TEXT,                    -- LIGHT / DARK / FLAT / BIAS
    quality         TEXT,
    object          TEXT,
    instrument      TEXT,
    camera          TEXT,
    filter          TEXT,
    exptime         REAL,
    gain            INTEGER,
    binning         INTEGER,
    ccd_temp        REAL,
    date_obs        TEXT,                    -- ISO 8601
    ra              REAL,
    dec             REAL,
    scope_id        TEXT,
    session_key     TEXT,
    fits_headers    BLOB,                    -- JSON of full header
    scanned_at      REAL
);

CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY,
    scope_id        TEXT,
    session_key     TEXT NOT NULL UNIQUE,
    target_id       INTEGER REFERENCES targets(id),
    instrument      TEXT,
    camera          TEXT,
    filter          TEXT,
    exptime         REAL,
    gain            INTEGER,
    binning         INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    frame_count     INTEGER,
    failed_count    INTEGER,
    notes           TEXT,
    description     TEXT       -- freeform; NULL = no note
);

CREATE TABLE session_frames (
    session_id      INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    frame_id        INTEGER REFERENCES frames(id) ON DELETE CASCADE,
    PRIMARY KEY (session_id, frame_id)
);

-- resolved_* columns are scanner-managed; source tags which path produced the match
CREATE TABLE targets (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,    -- normalized (e.g. "M31")
    aliases         TEXT,                    -- JSON array
    ra              REAL,
    dec             REAL,
    resolved_canonical          TEXT,
    resolved_separation_arcmin  REAL,
    resolved_at                 TEXT,
    resolved_source             TEXT
);

-- indexes: kind; (kind, instrument, camera, gain, exptime, binning, ccd_temp)
CREATE TABLE masters (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,           -- 'dark' | 'flat' | 'bias'
    scope_id        TEXT,
    source          TEXT,                    -- 'factory' | 'user' | 'astrolab'
    instrument      TEXT,
    camera          TEXT,
    filter          TEXT,
    exptime         REAL,
    gain            INTEGER,
    binning         INTEGER,
    ccd_temp        REAL,
    stack_count     INTEGER,
    file_hash       TEXT,
    path            TEXT NOT NULL UNIQUE,
    inode           INTEGER,
    mtime           REAL,
    size            INTEGER,
    date_built      TEXT,
    cache_ref       TEXT,                    -- content-cache pointer or external path
    source_frame_ids TEXT,                   -- JSON array of frames.id; null for factory
    scanned_at      REAL
);

CREATE TABLE calibration_matches (
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,           -- 'dark' | 'flat' | 'bias'
    master_id       INTEGER REFERENCES masters(id) ON DELETE SET NULL,
    match_quality   TEXT NOT NULL,           -- 'exact' | 'approx' | 'none'
    details         TEXT,                    -- JSON: deltas, candidate count, etc.
    overridden      INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL,
    PRIMARY KEY (session_id, kind)
);

-- indexes: status; submitted_at DESC
CREATE TABLE jobs (
    id                TEXT PRIMARY KEY,
    status            TEXT NOT NULL,
    template_id       TEXT NOT NULL,
    template_version  INTEGER NOT NULL,
    template_json     TEXT NOT NULL,
    job_json          TEXT NOT NULL,
    outputs_json      TEXT,
    error             TEXT,
    submitted_at      TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    node_hashes_json  TEXT
);

-- index: (job_id, seq)
CREATE TABLE job_events (
    id              INTEGER PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    type            TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    node_id         TEXT,
    fraction        REAL,
    message         TEXT,
    error           TEXT,
    extra_json      TEXT
);

-- index: updated_at DESC
-- formerly "renderings" (renamed in migration 6)
CREATE TABLE projects (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    template_id           TEXT NOT NULL,
    template_version      INTEGER NOT NULL,
    template_json         TEXT NOT NULL,
    base_job_json         TEXT NOT NULL,
    current_seq           INTEGER NOT NULL,
    draft_mode            INTEGER NOT NULL DEFAULT 0,
    source_session_ids    TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    cover_seq             INTEGER,           -- null = auto-pick latest with outputs
    description           TEXT
);

-- index: (project_id, seq)
-- formerly "rendering_history" (renamed in migration 6)
CREATE TABLE project_history (
    id              INTEGER PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    job_id          TEXT NOT NULL,
    overrides_json  TEXT NOT NULL,
    label           TEXT,
    created_at      TEXT NOT NULL,
    published       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, seq)
);

CREATE TABLE settings (
    key             TEXT PRIMARY KEY,
    value_json      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

### Calibration matching rules (initial)

Tunable per-camera; defaults shown:

| Frame | Match by                                 | Tolerance                  |
|-------|-------------------------------------------|----------------------------|
| Dark  | instrument, gain, exptime, ccd_temp, bin  | temp ±3°C, exptime exact   |
| Flat  | instrument, filter, gain, bin             | date proximity preferred   |
| Bias  | instrument, gain, bin                     | exact                      |

Match quality:
- `exact`: all keys match within zero tolerance.
- `approx`: within tolerance windows.
- `none`: UI surfaces it loud, user picks: skip cal / pick manually / cancel.

User overrides are sticky: overriding for one session optionally applies to "all sessions of this target" or "all sessions on this scope".

### HTTP API (sketch)

```
GET   /api/targets                              → list + counts
GET   /api/targets/{id}                         → sessions, calibration coverage
GET   /api/sessions/{id}                        → session detail
PATCH /api/sessions/{id}                        → reassign target, set description
POST  /api/scan                                 → trigger NAS rescan
GET   /api/templates                            → available templates
POST  /api/jobs                                 → submit Job; returns job_id
GET   /api/jobs/{id}                            → status
WS    /api/jobs/{id}/events                     → progress events
POST  /api/preview                              → submit preview-only Job; returns hash
GET   /api/preview/{node_hash}/{port}           → content-addressed preview image
POST  /api/projects                             → create project from job
POST  /api/projects/from_session                → create project from one session
POST  /api/projects/from_sessions               → create project from multiple sessions
GET   /api/projects                             → list projects
GET   /api/projects/{id}                        → project detail
PATCH /api/projects/{id}                        → apply param overrides or set description
PUT   /api/projects/{id}/cover                  → pin a history seq as cover image
PUT   /api/projects/{id}/history/{seq}/published → toggle gallery visibility
POST  /api/projects/{id}/revert/{seq}           → revert to a prior history entry
DELETE /api/projects/{id}                       → delete project
DELETE /api/projects/{id}/cache                 → evict project cache entries
GET   /api/gallery                              → published renders
GET   /api/tonight                              → visible DSOs for configured site
GET   /api/masters                              → master calibration frames
GET   /api/settings                             → current settings KV
PATCH /api/settings                             → update settings
```

### WebSocket event shape

```json
{ "type": "node_start",    "node_id": "register",        "ts": 1730000000.0 }
{ "type": "node_progress", "node_id": "register", "fraction": 0.42, "msg": "frame 84/200" }
{ "type": "node_done",     "node_id": "register", "cache_hash": "..." }
{ "type": "job_done",      "outputs": { "preview": "deadbeef..." } }
{ "type": "job_failed",    "node_id": "stack_ha", "error": "..." }
```

---

## Storage layout

```
~/projects/personal/astrolab/
├── docs/design.md             # this doc
├── server/                    # FastAPI app
├── ui/                        # SvelteKit app
├── templates/                 # *.yaml pipelines (versioned in git)
├── profiles/                  # *.yaml scope profiles (versioned in git)
└── nodes/                     # node implementations (Python)

# runtime data lives outside the repo:
~/Library/Application Support/astrolab/   (or $ASTROLAB_HOME)
├── astrolab.sqlite            # catalog + renders
├── cache/                     # content-addressed cache (the thrash dir)
│   └── <hash>/...
├── masters/                   # built calibration masters (pinned)
└── logs/

# NAS mounts (frames are read-only; archive is read/write):
/mnt/nas/astro/captures/<scope>/<target>/...   # source of truth; one subdir per scope
/mnt/nas/astro/astrolab-archive/                # archived renders + masters

# Each scope's ingest adapter knows how to walk its own subtree.
# The /<scope>/<target>/ layout above is a convention; adapters can be configured for any root.
```

Local SSD path is for the working cache (expected to thrash; size scales with concurrent jobs and drizzle factor). NAS path is for archival of named renders + master library. All paths are configurable via env var.

---

## Tech stack

- **Backend**: Python 3.12, FastAPI, Uvicorn, Pydantic v2, SQLite (via `sqlite3` + light query layer; no ORM unless we feel the need).
- **Astro**: `sirilpy`, `astropy`, `numpy`. Siril 1.4+ for Python API.
- **Workers**: in-process async tasks for cheap nodes; subprocess pool for expensive (one Siril per job). No Celery/Redis.
- **Frontend**: SvelteKit + TypeScript + Vite. Mobile-first CSS, no UI kit lock-in (probably plain CSS or PicoCSS / open-props).
- **Transport**: HTTP for control plane, WebSocket for job streams, content-addressed PNG URLs for previews.
- **Process mgmt**: a single `astrolab` binary script that runs the FastAPI server; SvelteKit served either via Vite dev server (development) or static-built and served by FastAPI (production).
- **Platforms**: Linux is where the server runs (Linux Mint as the reference). macOS works fine for non-Siril dev (catalog, UI, schema, template authoring); for end-to-end pipeline runs, work over SSH or directly on the Linux box.
- **Optional later**: Tailscale for remote access; Starnet++ wrapper; ASTAP integration.

### Why not React/Next
Svelte's reactivity model fits the slider→preview loop tightly without the bundle weight or boilerplate. Smaller mobile payload matters here.

### Why not htmx
Considered seriously. Lost on: (1) sub-debounce slider feedback wants real client state, (2) eventual graph visualization wants client-side SVG state, (3) compare-two-renders side-by-side wipe wants client interactivity.

### Why not Celery / Redis
Single-user. SQLite + an asyncio task queue + a subprocess pool is plenty. Adding Celery is the kind of premature complexity that'll bite us before it pays off.

---

## Phases

Each phase ends with something demoably useful, not "infrastructure done."

### Phase 0: DAG + first template (the foundation)
**Demo**: run the existing Naztronomy flow end-to-end via CLI from a Job spec, get a stacked + recombined image identical to the current script's output.
- Node interface + RunContext + Siril subprocess wrapper.
- Cache (filesystem + SQLite reverse index).
- Template loader + Job resolver (params merging, default injection).
- Port the existing script's stages into ~10 nodes (`convert_lights`, `calibrate`, `bg_extract`, `seq_platesolve`, `seq_register`, `extract_ha_oiii`, `seq_stack`, `align_pair`, `palette_combine`, `preview_png`).
- Stretch as a polymorphic node (start with `autostretch` only; variants come later).
- Scope profile + ingest adapter for **Dwarf 3** (initial target scope).
- One template: `hoo_dwarf3_dualband.yaml` (Ha/OIII dual-band → HOO recombine).
- CLI: `astrolab run <template> --target ... --sessions ...`.

### Phase 1: Catalog
**Demo**: open the web UI, see a list of targets with frame counts and calibration coverage.
- FITS-header scanner (incremental, resumable, by inode+mtime).
- Session clustering (target + same-night + same-instrument).
- Calibration matching rules + master library.
- Master-build job (uses Phase 0 nodes).
- FastAPI: read-only endpoints for targets/sessions/coverage.
- SvelteKit: Library view (mobile-friendly).

### Phase 2: Submit jobs from UI + live progress
**Demo**: pick a target, pick a template, hit go, watch the graph light up node-by-node, get a preview PNG when it lands.
- Job submission endpoint.
- WebSocket progress stream.
- Read-only graph visualization (SVG) showing node state (idle/running/cached/done/failed).
- Preview view: latest preview PNG, basic metadata.
- Cache hits surface as instant "node skipped".

### Phase 3: Edit loop (the payoff)
**Demo**: tweak GHS sliders on a phone, see the preview update in <1s. Switch palette HOO → HSO. Save a render. Compare two renders.
- Param panels driven by node `params_schema` (form-from-schema).
- Polymorphic node UI (variant dropdown + per-variant controls).
- Debounced auto-rerun of cheap subgraphs.
- Preview-master generation post-stack.
- Renders: save / list / open / compare (slider wipe).

### Phase 4: Variants + experimental flows
- Stretch variants: GHS, asinh, hist.
- Starnet++ node (mark `preview_approximate`).
- Palette set expanded (HSO, OOH, custom RGB mixer).
- Second template: Dwarf 3 broadband (UV/IR filter); third: HSO; fourth: mosaic.
- Profiles + adapters for Seestar S30/S50, Celestron Origin, Dwarf 2.

### Phase 5: Quality of life
- Template forking from UI (copies file).
- Render tags + search.
- Cache eviction policies (LRU, size cap).
- Background pre-stacking when a new session lands.
- Tailscale-ready (already works; just document).

### Future / maybe
- Visual graph editor (the "Node-RED moment"); only if topology editing becomes common.
- Multi-session integration ("combine these 3 nights").
- Plate solving against external services (astrometry.net fallback).
- Auto-tag bad subs by metric (FWHM, eccentricity, background).
- Cloud cover + seeing forecast overlay in the Tonight view (Met.no / pyastroweatherio).

---

## Risks & open questions

- **sirilpy headless lifecycle.** Resolved by Spike 01 (see `spikes/01-sirilpy-headless/FINDINGS.md`). Confirmed working on Linux Mint; macOS structurally blocked by AMFI launch constraints. Sirilpy is the only Siril runtime; macOS is for non-Siril dev (catalog, UI, schema, template authoring). End-to-end pipeline runs happen over SSH or on the Linux box directly.
- **Cache hash stability across Siril versions.** A Siril upgrade can change pixel output of an "identical" stack. Add `siril_version` into node hashes for any node that calls Siril, so upgrades invalidate cleanly.
- **Float param canonicalization.** Two GHS configs that look the same can hash differently due to float noise. Mitigation: per-param rounding precision in the schema.
- **NAS read perf during stacking.** SMB latency over hundreds of frames is a killer. Plan: sync session locally before expensive stages; the catalog scanner stays remote (reads only FITS headers).
- **`preview_approximate` ops.** Starnet at 1024px does not look like Starnet at full res. Acceptable for slider-tweaking, but UI must surface "preview-approximate" loudly.
- **Disk thrash.** Drizzle-2x intermediates are huge. Cache eviction policy lands in Phase 5; until then, monitor.
- **Calibration "no match" UX.** Easy to silently fall back to no calibration. Make it an explicit, blocking decision.

---

## Decision log

Capture decisions with the *why*; future-us will want this.

| # | Decision | Why |
|---|----------|-----|
| 1 | Typed DAG, not hardcoded pipeline | Want multiple flows + future reordering without code changes. |
| 2 | Content-addressed cache | Iterate on post-stack params without re-stacking. The single highest-leverage design choice. |
| 3 | Templates as files (YAML), not DB rows | Hand-editable, version-controlled, fork = copy. |
| 4 | Polymorphic nodes for "swap an op type" | 80% of "I want a different op here" cases without needing graph editing. |
| 5 | Scope profiles separate from nodes; per-scope ingest adapters in the catalog | Adding a new scope = data + a small adapter, not core-engine changes. Each smart scope writes a different file layout, so the messiness has to live somewhere; this contains it. |
| 6 | Preview master as a sibling artifact, same DAG runs against it | "Turbo" preview is just the same graph with a smaller input. |
| 7 | SvelteKit + FastAPI | Slider→preview loop wants real client reactivity; Svelte's reactivity model fits without React's overhead; Python backend keeps us in sirilpy/astropy land. |
| 8 | No Celery/Redis | Single user; in-process async + subprocess pool is enough. |
| 9 | Read-only graph viz in MVP, editor later (maybe) | Authoring is rare; tweaking is constant. Don't conflate them. |
| 10 | Cache lives on local SSD; NAS for archive only | Disk perf during expensive nodes is the bottleneck. |
| 11 | Sirilpy is the only Siril runtime; Linux-only for live pipelines | Spike 01 found macOS AMFI launch constraints block the bundled Python from third-party parents. Considered a parallel `.ssf`-shellout mode for macOS dev but rejected: a less-capable second backend doubles surface area for a use case (running pipelines on Mac) that's structurally limited regardless. macOS is for non-Siril dev; Siril runs happen over SSH or on Linux. |

---

## What's next

Phase 0 starts with two questions to spike before committing:

1. ~~**`sirilpy` headless lifecycle**~~. Done; see `spikes/01-sirilpy-headless/FINDINGS.md`. Confirmed working on Linux. macOS structurally blocked by AMFI; sirilpy is the only Siril runtime.
2. **Cache key canonicalization**: write a small property test that proves equivalent params hash the same and unequal params don't. Bake it into CI early.

After (2) passes, port the existing Naztronomy script onto the node framework, one stage at a time, with a snapshot test against the current script's output to make sure we haven't drifted.
