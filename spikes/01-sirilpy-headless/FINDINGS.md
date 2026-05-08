# Spike 01: sirilpy headless lifecycle

**Date:** 2026-05-07
**Status:** Done.

## Question
Can the astrolab pipeline runtime drive Siril 1.4 via `siril-cli + pyscript + sirilpy` from out-of-process Python, without a GUI session?

## Outcome

| Platform | Verdict |
|---|---|
| Linux (Mint 22, x86_64, AppImage 1.4.0-beta4) | **Yes**. Confirmed end-to-end: siril-cli launches headless, auto-bootstraps managed venv at `~/.local/share/siril/venv`, spawns the python script via `pyscript`, sirilpy connects via `MY_SOCKET`. |
| macOS | **No**. Structurally blocked by AMFI launch constraints on the bundled Python (see below). |

**Decision:** sirilpy is the only Siril runtime. macOS is for non-Siril dev only (catalog, UI, schema, template authoring). End-to-end pipeline runs happen over SSH or directly on the Linux box.

## How it works (Linux)

1. `siril-cli -s some.ssf` runs a Siril script file (or `-` for stdin) headless.
2. Inside the SSF, `pyscript path/to/script.py [args...]` tells Siril to spawn its managed Python venv interpreter, set the `MY_SOCKET` env var to a Unix domain socket Siril is listening on, and exec the script.
3. The script imports `sirilpy`, calls `sirilpy.SirilInterface().connect()`, which reads `MY_SOCKET` and connects back. From there it can run any Siril command via `cmd(...)` and read pixel data via `get_pixeldata()` / `set_pixeldata()`.

Siril manages its own venv (Linux: `~/.local/share/siril/venv`, macOS: `~/Library/Application Support/org.siril.Siril/siril/venv/`) and pre-provisions `sirilpy` into it. Extra deps come via `sirilpy.ensure_installed(...)` from inside the script. We don't pip-install sirilpy ourselves.

Other contract bits: `SIRIL_PYTHON_CLI=1` env var marks CLI-mode runs; on Windows the connection is via `MY_PIPE` (named pipe) instead of `MY_SOCKET`.

## What blocks macOS

```
kernel[0] (AppleMobileFileIntegrity) AMFI: Launch Constraint Violation (enforcing),
error info: c[4]p[3]m[1]e[0], (Constraint not matched) launching proc:
  /.../Siril.app/Contents/Frameworks/Python.framework/Versions/3.12/bin/python3.12,
failure proc: /Applications/cmux.app/Contents/MacOS/cmux
```

The bundled python3.12 has an AMFI launch constraint that requires its root ancestor process to be Siril.app's main GUI binary. AMFI walks the *full* ancestor chain, so any third-party terminal as the root parent (cmux, iTerm, Terminal.app, sshd, etc.) fails the check and the Python process is SIGKILL'd before any code runs (exit 137).

We tried and ruled out: launching python3.12 directly, launching via `siril-cli`, stripping `com.apple.quarantine`, granting App Management permission to the terminal. None help; the constraint is baked into the code signature.

Only an interactive Siril.app GUI session satisfies the constraint, which isn't useful for an out-of-process runtime. Re-signing the binary or disabling SIP/AMFI are out of scope.

## Reproduction

Spike artifacts are committed alongside this doc:
- `spike.ssf`: `requires 1.4.0` + `pyscript /tmp/astrolab_spike/spike.py`
- `spike.py`: minimal sirilpy connect + a few read-only `get_*` calls + disconnect.

On Linux (working):
```
~/Downloads/Siril-1.4.x-x86_64.AppImage --appimage-extract  # if FUSE issues
./squashfs-root/AppRun siril-cli -s /tmp/astrolab_spike/spike.ssf
```

On macOS (for replicating the AMFI denial):
```
command log stream --style compact --predicate 'eventMessage CONTAINS "AMFI"' &
~/Applications/Siril.app/Contents/MacOS/siril-cli -s /tmp/astrolab_spike/spike.ssf
```
