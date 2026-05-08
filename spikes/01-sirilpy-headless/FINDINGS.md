# Spike 01: sirilpy headless lifecycle

**Date:** 2026-05-07
**Status:** Done. Findings load-bearing for the runtime design.

## Question
Can the astrolab pipeline runtime drive Siril 1.4 via `siril-cli + pyscript + sirilpy` from out-of-process Python, without a GUI session?

## Answer

| Platform | Verdict | Why |
|---|---|---|
| Linux Mint | Likely yes (verify on hardware) | No structural blocker; the AMFI mechanism that breaks macOS does not exist on Linux. |
| macOS | **No** | AMFI launch constraints in Siril's bundled Python prevent any third-party parent (terminal, IDE, daemon) from spawning the interpreter. By design; not bypassable without re-signing. |

## How the architecture is supposed to work

1. `siril-cli -s some.ssf` runs a Siril script file (or `-` for stdin).
2. Inside the SSF, `pyscript path/to/script.py [args...]` tells Siril to spawn its managed Python venv interpreter, set the `MY_SOCKET` env var to a Unix domain socket Siril is listening on, and exec the script.
3. The script imports `sirilpy`, calls `sirilpy.SirilInterface().connect()`, which reads `MY_SOCKET` and connects back. From there it can run any Siril command via `cmd(...)` and read pixel data.

Siril manages its own venv at `~/Library/Application Support/org.siril.Siril/siril/venv/` (macOS) and pre-provisions `sirilpy` into it. Extra deps come via `sirilpy.ensure_installed("PyQt6", "numpy", "astropy")` from inside the script.

This **should** all work on Linux. We confirmed every piece up to the python spawn in macOS testing.

## What we confirmed on macOS (without ever running Python)

- `siril-cli -s file.ssf` launches headless cleanly, no GUI needed.
- The SSF dispatch is `requires <ver>` then `pyscript path.py`.
- Siril detects venv health, attempts auto-recreate when broken, logs structured progress.
- The connection contract is `MY_SOCKET` (Unix domain socket) on macOS/Linux, `MY_PIPE` (named pipe) on Windows.
- `SIRIL_PYTHON_CLI` env var marks CLI-mode runs vs GUI-mode runs.
- sirilpy is not pip-installable from outside; it ships inside Siril's bundle and Siril's internal venv setup is what wires it up. We don't need to install it ourselves.

## What blocks macOS

```
kernel[0] (AppleMobileFileIntegrity) AMFI: Launch Constraint Violation (enforcing),
error info: c[4]p[3]m[1]e[0], (Constraint not matched) launching proc:
  /Users/.../Siril.app/Contents/Frameworks/Python.framework/Versions/3.12/bin/python3.12,
failure proc: /Applications/cmux.app/Contents/MacOS/cmux
```

The bundled python3.12 has an AMFI launch constraint that requires its ancestor process to be Siril.app's main GUI binary. Any third-party terminal as the root ancestor fails the constraint and the Python process is killed before any code runs (SIGKILL, exit 137).

We tried:
- Launching python3.12 directly: blocked.
- Launching via `siril-cli` (which is also inside the bundle): blocked. AMFI walks up the *full* ancestor chain to the terminal.
- Stripping `com.apple.quarantine` recursively (`xattr -cr`): no effect; the constraint is in the code signature itself, not the quarantine flag.

Only an interactive Siril.app GUI session (root ancestor = Siril.app) satisfies the constraint. That's not useful for an out-of-process runtime.

Workarounds that exist but we are explicitly **not** taking:
- Re-signing the Python binary to remove the launch constraint. Invalidates the bundle signature; brittle; needs redoing on every Siril update.
- Patching AMFI (SIP-disable + custom kext). Way out of scope.

## Implications for astrolab design

The design always called for two runtime modes for nodes; the spike just promotes them from "primary + fallback" to "platform-determined":

1. **`pyscript` mode (Linux production)**: node implementation runs as a Python script invoked by Siril, talks to Siril via sirilpy. Rich: bidirectional, can read pixel data, structured progress.
2. **`ssf` mode (macOS dev, also a fallback)**: node generates a `.ssf` snippet, the runtime invokes `siril-cli -s -` with it, parses stdout/log. Coarser: no live pixel access, progress only via log scraping, but bulletproof and portable.

The node interface (`Node.run(inputs, params, ctx) -> outputs`) doesn't change. Only the `RunContext.siril()` helper differs: on Linux it wraps sirilpy, on macOS it accumulates a `.ssf` and flushes via subprocess. Node implementations that only need command dispatch (`cmd(...)`) work identically in both modes. Nodes that need pixel access (rare, mostly for ML/numpy ops) require sirilpy mode and will fail loudly on mac dev.

This is fine. mac dev was always second-class.

## Reproduction

The spike artifacts are at `/tmp/astrolab_spike/` (ephemeral; not committed):
- `spike.ssf`: `requires 1.4.0` + `pyscript /tmp/astrolab_spike/spike.py`
- `spike.py`: minimal sirilpy connect + `cmd("requires", "1.4.0")` + a few read-only `get_*` calls + disconnect.

Run via:
```
~/Applications/Siril.app/Contents/MacOS/siril-cli -s /tmp/astrolab_spike/spike.ssf
```

To capture the AMFI denial:
```
command log stream --style compact --predicate 'eventMessage CONTAINS "AMFI"' &
~/Applications/Siril.app/Contents/MacOS/siril-cli -s /tmp/astrolab_spike/spike.ssf
```

## Follow-ups

- [ ] Verify pyscript mode actually works end-to-end on Linux Mint when that machine is set up. Re-run the same spike there.
- [ ] Define the `RunContext.siril()` interface so the same node code works against either mode.
- [ ] Decide which (if any) nodes need to be Linux-only because they require pixel access.
