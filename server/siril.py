"""Siril runtime: locate the Siril 1.4 binary and run .ssf scripts headless.

Phase 2 starts here. We do not yet talk to sirilpy directly; this module just
emits a temporary `.ssf` script and runs it via `siril-cli -s`. That covers
the commands needed for ingest and stacking flows (convert, calibrate,
seqplatesolve, seqapplyreg, stack, save) which is what Phase 2 needs.
Pixel-level access via sirilpy lands in a later slice when a node actually
wants it; the contract here grows by adding a `pyscript` runner.

Per Spike 01, sirilpy / pipeline runs are Linux-only. On macOS this module
will raise if asked to run; tests on the Mac mock the runner.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("astrolab.siril")


# ---------------------------------------------------------------------------
# Version cache
# ---------------------------------------------------------------------------

_cached_siril_version: str | None = None
_siril_version_resolved: bool = False


def get_siril_version() -> str:
    """Return the detected Siril version as a dotted string, or '' on failure.

    Resolved once at first call and cached for the process lifetime. On macOS
    dev hosts (or any box without Siril) this returns '' rather than crashing
    -- Siril nodes don't actually execute there, but the hash machinery still
    needs a stable string.
    """
    global _cached_siril_version, _siril_version_resolved
    if _siril_version_resolved:
        return _cached_siril_version or ""
    _siril_version_resolved = True
    try:
        binary = find_siril()
        if binary.version is not None:
            _cached_siril_version = ".".join(str(x) for x in binary.version)
        else:
            # Source had no parseable version (eg $SIRIL_BIN override without filename).
            # Fall back to probing the binary directly, best-effort.
            _cached_siril_version = ""
    except SirilNotFound:
        _cached_siril_version = ""
    except Exception:
        log.debug("siril version probe failed; version string will be empty", exc_info=True)
        _cached_siril_version = ""
    return _cached_siril_version or ""


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------

# Siril emits lines like 'progress: Rejection stacking in progress..., 50.00%'
# while a long-running command runs. We forward those into ctx.progress so the
# UI bar moves continuously instead of jumping 20 -> 100.
_PROGRESS_RE = re.compile(r"^progress:\s*(.+?),\s*(\d+(?:\.\d+)?)\s*%\s*$")


def parse_progress(line: str) -> tuple[str, float] | None:
    """Pull (message, fraction in [0, 1]) from a Siril `progress:` line.

    Returns None for lines that don't match (the vast majority — most of
    Siril's stdout is `log:` chatter).
    """
    m = _PROGRESS_RE.match(line)
    if m is None:
        return None
    pct = float(m.group(2)) / 100.0
    return m.group(1).strip(), max(0.0, min(pct, 1.0))


def make_progress_handler(
    ctx,
    *,
    low: float = 0.2,
    high: float = 0.95,
    phases: int = 1,
    prefix: str = "",
):
    """Return an on_log handler that forwards Siril progress lines through
    `ctx.progress`, mapping 0..100% onto the [low, high] sub-range so the
    node's pre/post work still has room. Non-progress lines are sent to
    `ctx.log` at debug level.

    `phases`: how many sequential Siril commands the node runs. Each phase
    gets an equal slice of [low, high]; when we see the progress fraction
    drop sharply (the next sub-command's fresh 0% sweep starting), we
    advance to the next phase so the bar keeps moving forward instead of
    visually rewinding to 0. Pass `phases=2` for nodes that run, eg,
    seqplatesolve+seqapplyreg in one shot.

    `prefix` is prepended to every progress message — useful for noting
    which phase ('Plate solve / Applying registration') in addition to
    Siril's own message text.
    """

    state: dict[str, float | int] = {"last": 0.0, "phase": 0, "last_emitted": low}
    phase_size = (high - low) / max(phases, 1)
    # Threshold for 'this is a regression, not just noise' - protects against
    # Siril emitting 99 -> 0 between sub-commands without misreading a small
    # 0.5 -> 0.4 jitter as a phase change.
    phase_reset_drop = 0.5

    def handler(line: str) -> None:
        ctx.log.debug("siril: %s", line)
        parsed = parse_progress(line)
        if parsed is None:
            return
        msg, frac = parsed
        if frac + phase_reset_drop < state["last"]:
            state["phase"] = min(int(state["phase"]) + 1, phases - 1)
        state["last"] = frac
        scaled = low + state["phase"] * phase_size + frac * phase_size
        # Clamp to high so a stray >1.0 never escapes the band.
        scaled = min(scaled, high)
        # Monotonic clamp: never emit a fraction lower than the last one. The
        # phase counter saturates at phases-1 when the node passes a count
        # that's smaller than the actual number of progress-emitting Siril
        # sub-commands; without this clamp the bar would rewind into the
        # previous slice every time a later sub-command opens at 0%.
        # make_line_progress_handler in server/subproc.py applies the same
        # clamp; keeping the two handlers consistent.
        scaled = max(scaled, state["last_emitted"])
        state["last_emitted"] = scaled
        ctx.progress(scaled, f"{prefix}{msg}" if prefix else msg)

    return handler

# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

# Order: explicit env override -> AppImage in ~/Downloads -> system siril-cli.
# AppImage is preferred even when system Siril exists, since distros frequently
# ship 1.2.x and we need 1.4+.
_APPIMAGE_NAME_RE = re.compile(r"Siril-(?P<ver>\d+\.\d+(?:\.\d+)?(?:[~-]\S+)?)-x86_64.*\.AppImage$")
"""Matches Siril-1.4.3-x86_64.AppImage and friends."""

MIN_VERSION: tuple[int, int] = (1, 4)


class SirilNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class SirilBinary:
    path: Path
    version: tuple[int, int, int] | None = None  # parsed from filename / --version
    source: str = "unknown"  # 'env' | 'appdir' | 'appimage' | 'system'
    args_prefix: tuple[str, ...] = ()
    """Args to insert before our own. Used for AppRun, which dispatches by
    its first positional arg ('siril-cli' for the headless CLI mode)."""


def _parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _candidate_appimages() -> list[Path]:
    """List Siril AppImages under common locations, newest version first."""
    homes = [Path.home() / "Downloads", Path.home() / "Applications"]
    out: list[tuple[tuple[int, int, int], Path]] = []
    for home in homes:
        if not home.exists():
            continue
        for child in home.iterdir():
            if not child.is_file():
                continue
            m = _APPIMAGE_NAME_RE.match(child.name)
            if not m:
                continue
            ver = _parse_version(m.group("ver")) or (0, 0, 0)
            out.append((ver, child))
    out.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in out]


def _candidate_appdirs() -> list[tuple[tuple[int, int, int], Path]]:
    """List extracted Siril AppDirs (AppImages unpacked via --appimage-extract
    or unsquashfs). Returned as (version, AppRun_path) tuples, newest first.

    Preferred over packed AppImages because FUSE mounting frequently fails on
    locked-down hosts (AppArmor, unprivileged user namespaces, broken
    squashfuse) and an extracted tree always works.
    """
    homes = [Path.home() / "Applications", Path.home() / ".local" / "share"]
    out: list[tuple[tuple[int, int, int], Path]] = []
    for home in homes:
        if not home.exists():
            continue
        for child in home.iterdir():
            if not child.is_dir():
                continue
            apprun = child / "AppRun"
            if not apprun.exists():
                continue
            ver = _parse_version(child.name) or (0, 0, 0)
            out.append((ver, apprun))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def find_siril() -> SirilBinary:
    """Locate a usable Siril 1.4+ executable, raising SirilNotFound otherwise.

    Search order:
      1. $SIRIL_BIN env var (path; if it ends in 'AppRun' we add 'siril-cli'
         to the args prefix automatically).
      2. Extracted AppDirs at ~/Applications/*.AppDir/AppRun (newest first).
      3. Packed AppImages at ~/Downloads or ~/Applications (newest first).
      4. System siril-cli / siril on PATH (must be 1.4+).
    """
    override = os.environ.get("SIRIL_BIN")
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise SirilNotFound(f"$SIRIL_BIN points at {path} which does not exist")
        prefix: tuple[str, ...] = ("siril-cli",) if path.name == "AppRun" else ()
        return SirilBinary(path=path, source="env", args_prefix=prefix)

    for ver, apprun in _candidate_appdirs():
        if ver[:2] >= MIN_VERSION:
            return SirilBinary(
                path=apprun, version=ver, source="appdir", args_prefix=("siril-cli",)
            )

    for app in _candidate_appimages():
        m = _APPIMAGE_NAME_RE.match(app.name)
        ver = _parse_version(m.group("ver")) if m else None
        if ver is not None and ver[:2] >= MIN_VERSION:
            # The Siril AppImage's AppRun dispatches on its first positional
            # arg (`siril` for the GUI, `siril-cli` for headless). Without
            # the prefix the AppImage opens the GTK window and fails on a
            # headless box with `cannot open display`.
            return SirilBinary(
                path=app, version=ver, source="appimage", args_prefix=("siril-cli",)
            )

    sys = shutil.which("siril-cli") or shutil.which("siril")
    if sys is not None:
        path = Path(sys)
        # Cheap version probe; if it fails we still trust the binary but log it.
        try:
            res = subprocess.run(
                [str(path), "--version"], capture_output=True, text=True, timeout=5
            )
            ver = _parse_version(res.stdout) or _parse_version(res.stderr)
            if ver is not None and ver[:2] < MIN_VERSION:
                log.warning(
                    "system siril at %s is %s, below %d.%d minimum; trying AppImages first",
                    path,
                    ".".join(str(x) for x in ver),
                    *MIN_VERSION,
                )
                # fall through to error below, since we already exhausted AppImages
                raise SirilNotFound(
                    f"system siril is {ver[0]}.{ver[1]}.{ver[2]}; need >= "
                    f"{MIN_VERSION[0]}.{MIN_VERSION[1]}"
                )
            return SirilBinary(path=path, version=ver, source="system")
        except subprocess.SubprocessError as exc:
            raise SirilNotFound(f"could not probe {path}: {exc}") from exc

    raise SirilNotFound(
        "no Siril 1.4+ found. Set $SIRIL_BIN, place an AppImage under "
        "~/Downloads or ~/Applications, or install siril-cli."
    )


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------


@dataclass
class SirilResult:
    returncode: int
    stdout: str
    stderr: str
    ssf: str
    """The exact .ssf payload that was executed; useful for debug + logging."""


LogFn = Callable[[str], None]


class SirilRuntime:
    """Wraps the Siril binary as a 'run a .ssf and tell me what happened' API.

    Each .run() call spawns a fresh subprocess. We do not yet pool a long-lived
    Siril; per-node startup overhead is on the order of seconds, dwarfed by
    actual work like stacking. Pooling can come later if the iteration loop
    gets dominated by spawn time.
    """

    def __init__(self, binary: SirilBinary | None = None) -> None:
        self.binary = binary if binary is not None else find_siril()

    def run(
        self,
        commands: Sequence[str],
        *,
        working_dir: Path | None = None,
        on_log: LogFn | None = None,
        timeout: float | None = None,
        require_version: str = "1.4.0",
        cancel: threading.Event | None = None,
    ) -> SirilResult:
        """Run a list of Siril commands as one .ssf script.

        `commands` is the body of the script; we always prepend `requires
        <require_version>` so a script written against 1.4 fails fast on an
        older binary. Lines that already start with `requires` or `#` are
        passed through.

        `cancel` is an optional cooperative cancellation token. When set, a
        watchdog thread terminates the Siril subprocess and the call raises
        JobCancelled. Used to interrupt long stack/register operations when
        the rendering they belong to has been superseded by a fresh edit.
        """
        from .runtime import JobCancelled  # local to dodge import cycle

        if cancel is not None and cancel.is_set():
            raise JobCancelled()

        ssf_text = self._compose_ssf(commands, require_version=require_version)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ssf",
            prefix="astrolab-",
            dir=str(working_dir) if working_dir is not None else None,
            delete=False,
        ) as tf:
            tf.write(ssf_text)
            ssf_path = Path(tf.name)

        argv = [
            str(self.binary.path),
            *self.binary.args_prefix,
            "-s",
            str(ssf_path),
        ]

        watchdog_stop = threading.Event()
        watchdog: threading.Thread | None = None

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(working_dir) if working_dir is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if cancel is not None:
                # Background poll: when cancel fires, terminate the Siril
                # subprocess (its stdout closes, the read loop ends, we
                # detect cancellation post-loop).
                def _watch() -> None:
                    while not watchdog_stop.is_set():
                        if cancel.wait(timeout=0.5):
                            log.info("siril: cancel event set, terminating pid %d", proc.pid)
                            with contextlib.suppress(ProcessLookupError):
                                proc.terminate()
                            try:
                                proc.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                with contextlib.suppress(ProcessLookupError):
                                    proc.kill()
                            return

                watchdog = threading.Thread(target=_watch, daemon=True)
                watchdog.start()

            stdout_chunks: list[str] = []
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                stdout_chunks.append(line)
                if on_log is not None:
                    on_log(line)
            proc.wait(timeout=timeout)
            stderr = proc.stderr.read() if proc.stderr is not None else ""

            if cancel is not None and cancel.is_set():
                # Subprocess was killed by the watchdog. Surface as cancellation
                # so JobManager can mark the run 'interrupted', not 'failed'.
                raise JobCancelled()

            return SirilResult(
                returncode=proc.returncode,
                stdout="\n".join(stdout_chunks),
                stderr=stderr,
                ssf=ssf_text,
            )
        finally:
            watchdog_stop.set()
            if watchdog is not None:
                watchdog.join(timeout=1.0)
            with contextlib.suppress(FileNotFoundError):
                ssf_path.unlink()

    @staticmethod
    def _compose_ssf(commands: Sequence[str], *, require_version: str) -> str:
        """Render a .ssf script body, injecting `requires <ver>` at the top."""
        lines: list[str] = []
        already_required = any(c.lstrip().startswith("requires ") for c in commands)
        if not already_required:
            lines.append(f"requires {require_version}")
        lines.extend(commands)
        return "\n".join(lines) + "\n"
