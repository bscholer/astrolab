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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("astrolab.siril")

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
            return SirilBinary(path=app, version=ver, source="appimage")

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
    ) -> SirilResult:
        """Run a list of Siril commands as one .ssf script.

        `commands` is the body of the script; we always prepend `requires
        <require_version>` so a script written against 1.4 fails fast on an
        older binary. Lines that already start with `requires` or `#` are
        passed through.
        """
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
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(working_dir) if working_dir is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout_chunks: list[str] = []
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                stdout_chunks.append(line)
                if on_log is not None:
                    on_log(line)
            proc.wait(timeout=timeout)
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            return SirilResult(
                returncode=proc.returncode,
                stdout="\n".join(stdout_chunks),
                stderr=stderr,
                ssf=ssf_text,
            )
        finally:
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
