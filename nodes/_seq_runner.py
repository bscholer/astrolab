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

    Siril 1.4.x segfaults on process teardown after a successful sequence
    operation (returncode -11 / SIGSEGV). When stdout contains the literal
    "Sequence processing succeeded." message AND all expected outputs are
    present, the segfault is treated as success with a warning log entry.
    Real failures (bad returncode + no success message, or missing outputs)
    still raise RuntimeError.

seq_ref(path) -> Ref
    Build a SEQUENCE_FITS Ref whose node_hash is the empty-string
    placeholder that the runner patches in after run() returns.

image_ref(path) -> Ref
    Same for IMAGE_FITS.
"""

from __future__ import annotations

import logging
from pathlib import Path

from server.models import Ref
from server.ports import PortType

_log = logging.getLogger(__name__)

# Siril 1.4.x prints this exact line on a clean sequence run, before the
# shutdown teardown that sometimes segfaults.
_SIRIL_SEQ_SUCCESS_MARKER = "Sequence processing succeeded."

# Siril 1.4.x prints this exact line when a `stack` command succeeds, before
# the same shutdown teardown that sometimes segfaults.  The marker is specific
# to the `stack` command and does NOT appear in sequence-processing nodes
# (register, bg_extract, calibrate).  Verified against Siril 1.4.3 stdout:
#   log: Stacked sequence successfully.
_SIRIL_STACK_SUCCESS_MARKER = "Stacked sequence successfully."

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


def _outputs_present_fitseq(seq_out: Path, out_basename: str) -> tuple[bool, str]:
    """Return (present, description) for a FITSEQ output check."""
    expected = seq_out / f"{out_basename}.fit"
    if expected.exists():
        return True, expected.name
    return False, ""


def _outputs_present_perframe(
    seq_out: Path, out_basename: str, min_count: int
) -> tuple[bool, str]:
    """Return (present, description) for a per-frame output check.

    present is True when at least `min_count` matching frames exist.
    """
    frames = sorted(
        p
        for p in seq_out.iterdir()
        if p.name.startswith(f"{out_basename}_") and p.suffix in (".fit", ".fits")
    )
    if len(frames) >= max(1, min_count):
        return True, f"{len(frames)} frames"
    return False, ""


def _check_siril_seq_result(
    result,
    *,
    node_name: str,
    seq_out: Path,
    out_basename: str,
    fitseq: bool,
    min_count: int,
) -> str:
    """Validate a Siril sequence result, tolerating the Siril 1.4 shutdown segfault.

    Siril 1.4.x sometimes exits with returncode -11 (SIGSEGV) during process
    teardown after a sequence operation completes successfully. Every output file
    and the .seq index are already on disk before the crash; only the exit code
    is wrong.

    Success criteria:
      1. returncode is 0, OR (returncode != 0 AND stdout contains the exact
         "Sequence processing succeeded." marker AND all outputs are present)
      2. The expected output file(s) exist in seq_out.

    When the segfault path is taken a warning is logged so it is visible in the
    server log without surfacing as a user-facing error.

    Returns a human-readable "wrote ..." summary.
    Raises RuntimeError on real failures (bad exit code + no success marker,
    bad exit code + outputs missing, or zero returncode + outputs missing).
    """
    if result.returncode != 0:
        # Check for the known Siril 1.4 shutdown-segfault pattern.
        success_marker_present = _SIRIL_SEQ_SUCCESS_MARKER in result.stdout

        if fitseq:
            outputs_ok, wrote = _outputs_present_fitseq(seq_out, out_basename)
        else:
            outputs_ok, wrote = _outputs_present_perframe(seq_out, out_basename, min_count)

        if success_marker_present and outputs_ok:
            _log.warning(
                "%s: siril exited %d after success message; treating as success "
                "because outputs are present (Siril 1.4 shutdown segfault)",
                node_name,
                result.returncode,
            )
            return wrote

        # Real failure: bad exit code and either no success message or outputs missing.
        raise RuntimeError(
            f"{node_name}: siril exited {result.returncode}\n"
            f"--- ssf ---\n{result.ssf}\n"
            f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    # returncode == 0: validate outputs are present.
    if fitseq:
        outputs_ok, wrote = _outputs_present_fitseq(seq_out, out_basename)
        if not outputs_ok:
            raise RuntimeError(
                f"{node_name}: siril returned 0 but FITSEQ container "
                f"{seq_out / f'{out_basename}.fit'} is missing.\n"
                f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
            )
    else:
        outputs_ok, wrote = _outputs_present_perframe(seq_out, out_basename, min_count)
        if not outputs_ok:
            raise RuntimeError(
                f"{node_name}: siril returned 0 but no {out_basename}_*.fit* "
                f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                f"{result.stdout[-2000:]}"
            )

    return wrote


def _check_siril_stack_result(
    result,
    *,
    node_name: str,
    out_image: Path,
) -> None:
    """Validate a Siril `stack` command result, tolerating the Siril 1.4 shutdown segfault.

    Stack produces exactly ONE output file.  The success path is simpler than
    _check_siril_seq_result (no fitseq/per-frame branching needed).

    Success criteria:
      1. returncode is 0, OR (returncode != 0 AND stdout contains the exact
         "Stacked sequence successfully." marker AND out_image exists and is
         non-empty).
      2. The output image file exists and is non-empty.

    Raises RuntimeError on real failures:
      - bad exit code and no success marker
      - bad exit code and output missing / empty
      - exit code 0 but output missing / empty
    """
    def _output_ok() -> bool:
        return out_image.exists() and out_image.stat().st_size > 0

    if result.returncode != 0:
        success_marker_present = _SIRIL_STACK_SUCCESS_MARKER in result.stdout

        if success_marker_present and _output_ok():
            _log.warning(
                "%s: siril exited %d after stack success message; treating as success "
                "because output is present (Siril 1.4 shutdown segfault)",
                node_name,
                result.returncode,
            )
            return

        raise RuntimeError(
            f"{node_name}: siril exited {result.returncode}\n"
            f"--- ssf ---\n{result.ssf}\n"
            f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    # returncode == 0: output must still exist.
    if not _output_ok():
        raise RuntimeError(
            f"{node_name}: siril returned 0 but output {out_image} is missing or empty.\n"
            f"--- stdout (tail) ---\n{result.stdout[-2000:]}"
        )


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
    min_count: int = 1,
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
    min_count:
        Minimum number of per-frame output files required for success (ignored for
        fitseq mode). Callers that know the exact expected frame count should pass
        it here so an unexpectedly short output is caught as a failure even when
        Siril exited 0.

    Returns a human-readable summary such as "pp_light.fit" or "3 frames".
    Raises RuntimeError on Siril failure or missing output.

    Tolerates returncode -11 (Siril 1.4 shutdown segfault) when stdout contains
    "Sequence processing succeeded." and all expected outputs are present.

    For stack operations use _check_siril_stack_result, which matches the
    "Stacked sequence successfully." marker that the `stack` command emits
    instead of "Sequence processing succeeded."
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

    wrote = _check_siril_seq_result(
        result,
        node_name=node_name,
        seq_out=seq_out,
        out_basename=out_basename,
        fitseq=fitseq,
        min_count=min_count,
    )

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
