# Contributing to astrolab

Thanks for poking around. This doc covers what you need to get the dev loop
running, what tests/lints to run before opening a PR, and the load-bearing
shapes of the codebase so you don't have to reverse-engineer the whole
thing to add a node.

## Dev setup

You'll need:

- **Python 3.12+** and [uv](https://github.com/astral-sh/uv) for the backend
- **Node 20+** (for the SvelteKit UI)
- **Linux + NVIDIA GPU** if you want to run the actual stacking pipeline
  end-to-end. macOS works for everything except the Siril-using paths;
  tests on macOS mock the Siril subprocess.

```bash
# clone
git clone https://github.com/bscholer/astrolab.git
cd astrolab

# python deps + venv (uv reads pyproject.toml)
uv sync

# UI deps
cd ui && npm install && cd ..

# run the API on :8000
.venv/bin/uvicorn server.api:app --reload --host 0.0.0.0 --port 8000

# in another terminal: vite dev server on :5173 (proxies /api -> :8000)
cd ui && npm run dev
```

Open `http://localhost:5173`. The UI talks to `/api/...` which Vite
proxies through to FastAPI.

For a single-port setup that mirrors production, run `cd ui && npm run
build` once, then just hit `http://localhost:8000/` — FastAPI serves the
prebuilt UI under the same origin. Re-run `npm run build` after frontend
edits.

## Linux vs macOS

The pipeline shells out to three Linux-only tools: **Siril 1.4+**,
**GraXpert 3.x**, and **StarNet++ v2**. The runtime auto-detects them
(env override > `~/Applications` AppDir > `~/Downloads` AppImage > $PATH;
GraXpert/StarNet at `~/tools/{graxpert,starnet}/` per
`scripts/install-tools-linux.sh`).

On macOS you can:
- Edit code, run the API server, and hit non-Siril endpoints.
- Run the test suite — pipeline tests skip when Siril isn't found
  (`tests/test_siril_runtime.py::SKIPPED [1] no siril found on this
  machine`), and the rest mock the subprocess.
- Use the UI against a remote backend (point Vite's proxy at the Linux
  box's IP).

If you're working on a Siril-using node, do final validation on the
Linux box. The test fixtures cover the unit-level behavior, but the
real subprocess is the source of truth.

## Tests + lints

```bash
# backend tests (runs the full suite, skips Siril-dependent tests on macOS)
.venv/bin/pytest

# scoped: just the slice you're touching
.venv/bin/pytest tests/test_job_builder.py

# lint
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# UI type-check
cd ui && npm run check

# UI prod build (catches static-adapter problems)
cd ui && npm run build
```

PR gates: `pytest`, `ruff check`, and `npm run check` should all be
clean. Ruff config (line-length 100, py312 target, selected rules) lives
in `pyproject.toml` and is authoritative.

### Pre-commit hooks

Mirror the CI ruff job locally so failures show up before push, not in
GH Actions:

```bash
.venv/bin/pre-commit install
```

Runs `ruff --fix` plus a handful of cheap hygiene hooks (trailing
whitespace, EOF newline, YAML/TOML syntax, merge-conflict markers) on
every staged file at commit time. To run against the whole tree (e.g.
after a fresh clone): `.venv/bin/pre-commit run --all-files`.

The hook deliberately doesn't run `ruff-format` yet — the existing tree
wasn't formatted, and a sweeping format pass would muddy unrelated
diffs. Format-the-world is on the table for its own PR when we're
ready.

## Adding a node

A node is a Pydantic-typed `(inputs, params) -> outputs` function with a
version. Same `(inputs, params, version)` must produce equivalent
outputs — the cache relies on it.

Skeleton:

```python
# nodes/basic/my_node.py
from __future__ import annotations
from pathlib import Path

from pydantic import BaseModel, Field

from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register


class MyNodeParams(BaseModel):
    strength: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="What this knob does (shows up as a tooltip in the UI).",
        # Hash precision controls how many decimals the cache key keeps:
        # 0.500 and 0.5001 hash the same with hash_precision=2.
        json_schema_extra={"hash_precision": 2},
    )


@register("my_node")
class MyNode(Node[MyNodeParams]):
    id = "my_node"
    version = 1
    cost = "cheap"  # cheap | medium | expensive — drives UI affordances

    inputs = {"image": PortType.IMAGE_FITS}
    outputs = {"image": PortType.IMAGE_FITS}
    params_schema = MyNodeParams

    def run(
        self,
        inputs: dict[str, Ref],
        params: MyNodeParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        ctx.progress(0.0, "my_node: starting")
        # ... do the work ...
        out_path = out_dir_path / "image.fit"
        # write FITS to out_path
        ctx.progress(1.0, "my_node: done")
        return {
            "image": Ref(
                node_hash="",  # patched in by the runner
                port="image",
                path=out_path,
                type=PortType.IMAGE_FITS,
            )
        }
```

Then:

1. Import it from `nodes/basic/__init__.py` so the registry sees it at
   process start (every node decorator runs at import time).
2. Add a test under `tests/test_my_node.py` exercising the run path
   against a synthetic input. See `tests/test_downscale.py` for the
   simplest possible example.
3. If the node should appear in the canned pipeline, wire it into
   `templates/calibrate_register_stack.yaml` and bump the template's
   `version:` field.

### Field metadata that drives the UI

The UI auto-builds parameter forms from each node's Pydantic schema.
Useful `json_schema_extra` keys:

- `hash_precision: N` — round floats to N decimals before hashing.
  Without this, `0.5000001` busts the cache against `0.5`.
- `ui_hidden: True` — hide from the UI entirely (pipeline plumbing
  like `basename`, `fitseq`).
- `ui_section: "advanced"` — collapsed behind a disclosure in the
  form. Default is "basic" (always visible).
- `ui_when: {paramName: value}` — only show this field when another
  param has a specific value. List value = "any of these"; scalar =
  exact match. Useful for mode-conditional knobs.

### Cancellation

Long-running nodes should pass `ctx.cancel` (a `threading.Event`) to
their subprocess wrappers. `SirilRuntime.run()` and
`server.subproc.run_streamed()` already accept it. When set (e.g. the
user supersedes the job mid-run), the wrapper terminates the
subprocess and the runner translates that into a `JobCancelled` ->
`'interrupted'` job status, separate from `'failed'`.

## Adding a template

Templates live as YAML under `templates/<id>.yaml` and parse into the
`Template` model in `server/models.py`. Filename stem = template id.

When you change a template that's already in use:

- Bump `version:`. The cache hash includes template version, so a bump
  invalidates downstream entries cleanly.
- Add a sentence to the template's `description:` block explaining
  *why* the chain looks the way it does — future-you will read it.

## Style + commit conventions

- **Comments**: write WHY, not WHAT. The named identifier already
  names the what. If a comment would just describe what the next line
  does, delete it.
- **No AI attribution in commits or PRs.** No "Co-Authored-By:
  Claude", no "Generated with Claude Code". Commits should read as
  if a human wrote them — because functionally one did.
- **Imperative commit subjects**: "Add foo node" not "Added foo
  node". One logical change per commit; if you find yourself writing
  "X and Y" in the subject, consider splitting.
- **No em-dashes** (—) in committed text. Hyphens or commas instead.

## License

The code is MIT (see `LICENSE`). The vendored OpenNGC catalog under
`catalogs/openngc/` is CC BY-SA 4.0 by Mattia Verga; that carve-out is
preserved in `LICENSE`. By contributing you agree your contributions
are MIT-licensed.
