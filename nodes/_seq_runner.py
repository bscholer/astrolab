"""Shared staging and Siril-run helpers for sequence-processing nodes.

Every node that pipes a SEQUENCE_FITS through Siril follows the same shape:

  1. Coerce out_dir to Path.
  2. Validate the input sequence dir exists.
  3. Symlink the input into a working dir (_stage_sequence).
  4. Build and run a Siril command list.
  5. Validate output landed where expected (FITSEQ or per-frame).
  6. Strip the staged symlinks so the cache entry holds only outputs.
  7. Return a Ref.

This module owns all of that, so individual nodes only declare the Siril
command(s) and the output-basename pattern.

Public API
----------
stage_sequence(seq_in, seq_out, basename, fitseq) -> list[Path]
    Symlink the input into seq_out. Same semantics as the old private
    _stage_sequence in calibrate.py; callers that want manual control can
    still use it directly.

quote(path) -> str
    Shell-quote a Path for inclusion in a Siril .ssf command.

run_siril_on_sequence(*, node_name, seq_in, seq_out, commands,
                       basename, fitseq, ctx, runtime) -> str
    Run `commands` via `runtime`, validate the expected output files
    appeared in seq_out, strip the staged symlinks, and return a
    human-readable "wrote N frames" summary. Raises RuntimeError on
    Siril failure or missing output.

seq_ref(path) -> Ref
    Build a SEQUENCE_FITS Ref whose node_hash is the empty-string
    placeholder that the runner patches in after run() returns.

image_ref(path) -> Ref
    Same for IMAGE_FITS.
"""

from __future__ import annotations

from pathlib import Path

from server.models import Ref
from server.ports import PortType

# ---------------------------------------------------------------------------
# Path quoting
# ---------------------------------------------------------------------------


def quote(path: Path) -> str:
    """Shell-quote a path for Siril .ssf scripts.

    Only wraps in double quotes when the path contains a space, tab, or
    literal double quote. The vast majority of paths are clean; avoiding
    unnecessary quoting keeps the ssf readable in debug logs.
    """
    s = str(path)
    if any(c in s for c in (" ", "\t", '"')):
        return '"' + s.replace('"', r"\"") + '"'
    return s


# ---------------------------------------------------------------------------
# Input staging
# ---------------------------------------------------------------------------


def stage_sequence(seq_in: Path, seq_out: Path, basename: str, fitseq: bool) -> list[Path]:
    """Symlink the input sequence into seq_out so Siril finds it in cwd.

    Returns the list of staged links so the caller can clean them up after
    the run (drop_staged does that). seq_out must already exist.
    """
    staged: list[Path] = []

    if fitseq:
        src = seq_in / f"{basename}.fit"
        if not src.exists():
            return []
        link = seq_out / src.name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src.resolve())
        staged.append(link)
        return staged

    for f in sorted(seq_in.iterdir()):
        if not (f.is_file() or f.is_symlink()):
            continue
        if not f.name.startswith(f"{basename}_"):
            continue
        # Stage matching frames AND the Siril .seq index so downstream
        # commands (stack, calibrate w/ existing alignment) can read sequence
        # metadata. .seq references frames by relative name; the symlinks in
        # the same dir make those references valid.
        if f.suffix not in (".fit", ".fits", ".seq"):
            continue
        link = seq_out / f.name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(f.resolve())
        staged.append(link)

    # Siril 1.4 writes <basename>_.seq but `stack` reads <basename>.seq (no
    # trailing underscore). Add a no-underscore alias so both naming
    # conventions resolve. Only relevant for non-fitseq mode.
    underscored = seq_out / f"{basename}_.seq"
    no_underscore = seq_out / f"{basename}.seq"
    if underscored.exists() and not no_underscore.exists():
        no_underscore.symlink_to(underscored.resolve())
        staged.append(no_underscore)

    return staged


def drop_staged(staged: list[Path]) -> None:
    """Remove the staged symlinks left by stage_sequence after a successful run."""
    for link in staged:
        if link.is_symlink() or link.exists():
            link.unlink()


# ---------------------------------------------------------------------------
# Siril execution + output validation
# ---------------------------------------------------------------------------


def run_siril_on_sequence(
    *,
    node_name: str,
    seq_in: Path,
    seq_out: Path,
    commands: list[str],
    basename: str,
    out_basename: str,
    fitseq: bool,
    ctx,
    runtime,
    phases: int = 1,
) -> str:
    """Stage, run, validate, and clean up a Siril sequence operation.

    Parameters
    ----------
    node_name:
        Used in error messages (e.g. "calibrate", "seq_bg_extract").
    seq_in:
        Source sequence directory (must exist).
    seq_out:
        Working directory where Siril runs (will be created if absent).
    commands:
        Full list of Siril .ssf commands including the leading `cd` line.
    basename:
        Input basename used to find staged files.
    out_basename:
        Expected output basename (e.g. "pp_light" for calibrate, "bkg_pp_light"
        for bg_extract). Used to find and count output frames.
    fitseq:
        True if input/output is a FITSEQ container (.fit); False for per-frame.
    ctx:
        RunContext; used for cancel propagation and make_progress_handler.
    runtime:
        A SirilRuntime (or compatible fake) instance.
    phases:
        Number of sequential Siril sub-commands; forwarded to make_progress_handler.

    Returns a human-readable summary such as "pp_light.fit" or "3 frames".
    Raises RuntimeError on Siril failure or missing output.
    """
    from server.siril import make_progress_handler

    seq_out.mkdir(parents=True, exist_ok=True)

    if not seq_in.exists():
        raise RuntimeError(f"{node_name}: input dir does not exist: {seq_in}")

    staged = stage_sequence(seq_in, seq_out, basename, fitseq)
    if not staged:
        raise RuntimeError(
            f"{node_name}: no input frames matching basename '{basename}' under {seq_in}"
        )

    result = runtime.run(
        commands,
        working_dir=seq_out,
        on_log=make_progress_handler(ctx, phases=phases),
        cancel=ctx.cancel,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{node_name}: siril exited {result.returncode}\n"
            f"--- ssf ---\n{result.ssf}\n"
            f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    # Validate expected output.
    if fitseq:
        expected = seq_out / f"{out_basename}.fit"
        if not expected.exists():
            raise RuntimeError(
                f"{node_name}: siril returned 0 but FITSEQ container "
                f"{expected} is missing.\n--- stdout (tail) ---\n"
                f"{result.stdout[-2000:]}"
            )
        wrote = expected.name
    else:
        frames = sorted(
            p
            for p in seq_out.iterdir()
            if p.name.startswith(f"{out_basename}_") and p.suffix in (".fit", ".fits")
        )
        if not frames:
            raise RuntimeError(
                f"{node_name}: siril returned 0 but no {out_basename}_*.fit* "
                f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                f"{result.stdout[-2000:]}"
            )
        wrote = f"{len(frames)} frames"

    drop_staged(staged)
    return wrote


# ---------------------------------------------------------------------------
# Ref factories
# ---------------------------------------------------------------------------
# The runtime patches node_hash into every Ref returned by run() (see
# server/runtime.py: the committed dict replaces ref.node_hash with h).
# Nodes therefore return Refs with an empty-string placeholder, which the
# runtime immediately overwrites. These factories make that pattern explicit
# and DRY -- no more scattered `node_hash=""` literals.


def seq_ref(path: Path) -> Ref:
    """SEQUENCE_FITS Ref with placeholder node_hash (patched by the runner)."""
    return Ref(node_hash="", port="sequence", path=path, type=PortType.SEQUENCE_FITS)


def image_ref(path: Path, *, display_ready: bool = False) -> Ref:
    """IMAGE_FITS Ref with placeholder node_hash (patched by the runner)."""
    return Ref(
        node_hash="",
        port="image",
        path=path,
        type=PortType.IMAGE_FITS,
        display_ready=display_ready,
    )
