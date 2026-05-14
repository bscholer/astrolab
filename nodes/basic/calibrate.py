"""calibrate: apply per-frame-matched master darks (+ optional master flat /
bias) to a sequence via Siril 1.4's `calibrate` command.

Input ports
- sequence (SEQUENCE_FITS, required)        prior step's sequence dir
- dark     (MASTER_FITS_LIST, optional)     master darks for each (exptime,
                                            temp) bin in the sequence; the
                                            node reads each light's headers
                                            and picks the closest dark from
                                            the pool. A single-dark list is
                                            equivalent to the old single-
                                            master behavior.
- flat     (MASTER_FITS,   optional)        master flat frame
- bias     (MASTER_FITS,   optional)        master bias frame

With an empty dark list, calibrate becomes a prefix-rename pass with
-cfa/-debayer applied; the stack is noisier but the pipeline still
completes. Same fallback the original single-dark node had.

Output port
- sequence (SEQUENCE_FITS): directory containing pp_<basename>_*.fit (or
                            pp_<basename>.fit when the input was a FITSEQ
                            AND only one dark was needed). When the bundle
                            spans multiple darks, output is always per-
                            frame because we run Siril once per dark group
                            and concatenate the results into a single
                            directory.

The node reads FITS headers (EXPTIME, GAIN, DET-TEMP / CCD-TEMP) from every
light frame and every candidate dark, groups lights by their best-matching
dark, and runs Siril calibrate once per group. Frame-count-weighted progress
is reported across the group calls.

When no dark in the pool has the right exptime/gain to serve a frame, that
frame is "uncalibratable". By default uncalibratable frames pass through
debayered-only (matching the no-dark fallback); enabling
exclude_uncalibratable drops them from the output instead, which is the
right call when the user has a mostly-complete dark library and would
rather lose a few subs than mix calibrated and uncalibrated frames in the
final stack.

TODO: Add a dark-scaling fallback (Siril's -dark_scaling=<f> flag) for
frames whose closest match is more than ~5C off. Scaling is cheap, but
estimating the right factor robustly (across uncooled-sensor variability)
adds complexity that isn't obviously worth the residual reduction; for
now, dithering + sigma clipping absorbs the error.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from astropy.io import fits
from pydantic import BaseModel, Field

from nodes._seq_runner import drop_staged, quote, run_siril_on_sequence, seq_ref, stage_sequence
from nodes._storage_estimate import estimate_sequence_output_bytes
from nodes.base import Node
from server.models import Ref, RunContext
from server.ports import PortType
from server.registry import register
from server.siril import SirilRuntime, make_progress_handler

# Mirrors server.catalog.matching.DARK_TEMP_TOLERANCE_C: the job builder
# already picked the closest dark within tolerance for each bin, so if
# the per-frame delta exceeds this we know we're on the fallback path
# and should flag it on the node card.
_FALLBACK_TEMP_C: float = 5.0


class CalibrateParams(BaseModel):
    input_basename: str = Field(
        default="light",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_]+$",
        description="Basename of the input sequence (matches Siril `<basename>_NNNNN.fit` "
        "or the `<basename>.fit` FITSEQ container). Output is automatically prefixed "
        "with 'pp_'.",
        json_schema_extra={"ui_hidden": True},
    )
    fitseq: bool = Field(
        default=True,
        description="Operate on a FITSEQ container (single .fit) rather than per-frame "
        "files. Must match the upstream convert_lights setting. Note that the output "
        "is always per-frame when the bundle requires more than one dark.",
        json_schema_extra={"ui_hidden": True},
    )
    cfa: bool = Field(
        default=True,
        description="Pass -cfa for OSC sensors so darks and flats are scaled per "
        "Bayer pattern. On by default — this is the right behavior for any sensor "
        "with BAYERPAT in the FITS header (Dwarf 3, Seestar, ZWO OSC). Flip off "
        "only for mono cameras or already-debayered inputs.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Leave on for any OSC (color) sensor; turning off with a"
                " Bayer-pattern camera produces a cyan/magenta color cast."
            ),
        },
    )
    cosmetic: bool = Field(
        default=True,
        description="Pass -cc=dark to apply hot/cold pixel correction from the dark. "
        "Cheap and almost always wanted.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Keeping on removes bright hot-pixel specks that would otherwise"
                " appear as stars in the final image."
            ),
        },
    )
    equalize_cfa: bool = Field(
        default=True,
        description="Pass -equalize_cfa when calibrating CFA flats; equalizes the two "
        "G channels of the Bayer pattern so post-debayer colors are balanced. Only "
        "meaningful with cfa=True; on by default for the OSC pipeline.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Keeps green channel balance even; turning off can cause"
                " a subtle green or magenta tint."
            ),
        },
    )
    debayer: bool = Field(
        default=True,
        description="Pass -debayer so calibrate emits debayered RGB frames. On for "
        "OSC sensors (Dwarf 3): registration applies sub-pixel shifts that scramble "
        "the Bayer pattern, so we must debayer here before register/stack. Turn off "
        "only for mono cameras or pure-CFA workflows.",
        json_schema_extra={
            "agent_hint": (
                "Leave on for OSC sensors; turning off means registration shifts"
                " will scramble the Bayer mosaic and ruin color."
            ),
        },
    )
    exclude_uncalibratable: bool = Field(
        default=False,
        description="When true, drop frames that have no usable master dark in the "
        "pool. Default false: uncalibratable frames pass through debayered-only "
        "(matching the no-dark fallback), so the stack still includes them. Enable "
        "this when your dark library covers most of your captures and you'd rather "
        "lose a few subs than mix calibrated and uncalibrated frames.",
        json_schema_extra={
            "ui_section": "advanced",
            "agent_hint": (
                "Most users should leave this off; sigma-clip stacking handles"
                " a few uncalibrated frames fine."
            ),
        },
    )
    dark_bins: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Per-dark metadata supplied by the job builder so the node can "
        "match each light to the right dark without reading the dark's FITS headers. "
        "Each entry: {path: str, exptime: float, gain: int|None, ccd_temp: float|"
        "None}. Necessary because some capture programs (notably Dwarf 3 factory "
        "masters) leave EXPTIME/GAIN/CCD-TEMP out of the FITS header and encode "
        "them in the filename — the catalog parses those at ingest time, and this "
        "param threads that authoritative metadata to the node. Empty list = fall "
        "back to reading the dark's headers (works for capture programs that write "
        "complete master-dark headers).",
        json_schema_extra={"ui_hidden": True},
    )


def _read_temp(hdr: fits.Header) -> float | None:
    """Read sensor temperature, accepting Dwarf-3-style DET-TEMP or the more
    conventional CCD-TEMP keys. None when neither is present."""
    for key in ("DET-TEMP", "CCD-TEMP", "CCDTEMP"):
        if key in hdr:
            try:
                return float(hdr[key])
            except (TypeError, ValueError):
                return None
    return None


def _read_fits_meta(path: Path) -> dict[str, Any]:
    """Pull (exptime, gain, ccd_temp) from a FITS primary header."""
    with fits.open(str(path), memmap=False) as hdul:
        hdr = hdul[0].header
        return {
            "path": path,
            "exptime": float(hdr["EXPTIME"]) if "EXPTIME" in hdr else None,
            "gain": int(hdr["GAIN"]) if "GAIN" in hdr else None,
            "ccd_temp": _read_temp(hdr),
        }


def _build_dark_pool(
    dark_refs: list[Ref], dark_bins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the matching pool from `params.dark_bins` when populated;
    otherwise fall back to reading FITS headers from the Ref paths.

    The job builder populates `dark_bins` from the catalog (which has
    authoritative parsed metadata from filename or header at ingest
    time). The fallback path supports callers that hand a list of Refs
    without metadata — e.g. tests that wire `calibrate.dark` manually
    and a legacy single-master explicit override that hasn't been
    re-emitted through the job builder.
    """
    if dark_bins:
        # Trust the param when present. We still want the Ref order to
        # be the source of truth for which masters are visible to this
        # run, so we look each Ref up by path and skip metadata-only
        # entries that don't correspond to a Ref.
        by_path = {entry["path"]: entry for entry in dark_bins}
        pool: list[dict[str, Any]] = []
        for r in dark_refs:
            entry = by_path.get(str(r.path))
            if entry is None:
                # Metadata missing for this Ref — last-resort read from
                # the FITS header. Better to try than to silently drop
                # the dark.
                pool.append(_read_fits_meta(r.path))
                continue
            pool.append(
                {
                    "path": r.path,
                    "exptime": entry.get("exptime"),
                    "gain": entry.get("gain"),
                    "ccd_temp": entry.get("ccd_temp"),
                }
            )
        return pool
    return [_read_fits_meta(r.path) for r in dark_refs]


def _pick_dark(
    light: dict[str, Any], pool: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Choose the best dark for one light from the candidate pool.

    Hard equality on exptime (within 0.001s) and gain. Within that, the
    nearest temp wins; ties broken by first-seen so the result is stable.
    Returns None when no candidate has matching exptime+gain — that frame
    is "uncalibratable" and the caller decides whether to pass it through
    or drop it.
    """
    same = [
        d
        for d in pool
        if d["exptime"] is not None
        and light["exptime"] is not None
        and abs(d["exptime"] - light["exptime"]) < 0.001
        and (d["gain"] is None or light["gain"] is None or d["gain"] == light["gain"])
    ]
    if not same:
        return None
    lt = light["ccd_temp"]
    if lt is None:
        return same[0]
    return min(
        same,
        key=lambda d: (
            abs((d["ccd_temp"] or 0.0) - lt)
            if d["ccd_temp"] is not None
            else float("inf")
        ),
    )


def _extract_fitseq_frames(fitseq_path: Path, dest_dir: Path, basename: str) -> list[Path]:
    """Split a FITSEQ container into per-frame files <basename>_NNNNN.fit
    written into dest_dir. Returns the list of written frame paths.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with fits.open(str(fitseq_path), memmap=False) as hdul:
        idx = 0
        for hdu in hdul:
            if hdu.data is None:
                continue
            idx += 1
            out = dest_dir / f"{basename}_{idx:05d}.fit"
            fits.PrimaryHDU(data=hdu.data, header=hdu.header).writeto(
                str(out), overwrite=True
            )
            written.append(out)
    return written


def _scoped_progress_ctx(
    parent: RunContext,
    *,
    base_done: int,
    size: int,
    total: int,
    progress_lo: float,
    progress_hi: float,
) -> RunContext:
    """Return a RunContext whose `progress` rescales a per-group 0-1
    fraction into the overall progress window.

    Siril doesn't know it's only processing a slice of the bundle, so
    it streams progress that climbs from 0 to ~1 within each group.
    The naive passthrough makes the overall bar reset every time the
    multi-dark loop starts a new group. The wrapper maps that local
    fraction into `[progress_lo + span * base_done/total,
    progress_lo + span * (base_done + size)/total]` so the bar moves
    monotonically across all groups.

    A fraction of 0 maps to the group's start; a fraction of 1 maps
    to the group's end. Values outside [0, 1] are clamped so a noisy
    Siril log line can't push the bar past the group boundary.
    """
    span = progress_hi - progress_lo
    width = (size / total) * span if total > 0 else 0.0
    base = progress_lo + (base_done / total) * span if total > 0 else progress_lo
    parent_progress = parent.progress

    def _wrapped(fraction: float, message: str) -> None:
        clamped = 0.0 if fraction < 0.0 else (1.0 if fraction > 1.0 else fraction)
        parent_progress(base + clamped * width, message)

    return parent.model_copy(update={"progress": _wrapped})


def _run_group_calibrate(
    *,
    group_dir: Path,
    frames: list[Path],
    dark_path: Path | None,
    params: CalibrateParams,
    ctx: RunContext,
    runtime: SirilRuntime,
    flat_ref: Ref | None,
    bias_ref: Ref | None,
) -> list[Path]:
    """Run Siril calibrate on one group of frames sharing a dark.

    Returns the list of output paths under group_dir (named pp_<basename>_NNNNN.fit).
    """
    group_dir.mkdir(parents=True, exist_ok=True)
    basename = params.input_basename

    staged: list[Path] = []
    for i, src in enumerate(sorted(frames, key=lambda p: p.name), start=1):
        link = group_dir / f"{basename}_{i:05d}.fit"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src.resolve())
        staged.append(link)

    opts: list[str] = []
    if dark_path is not None:
        opts.append(f"-dark={quote(dark_path.resolve())}")
    if flat_ref is not None:
        opts.append(f"-flat={quote(flat_ref.path.resolve())}")
    if bias_ref is not None:
        opts.append(f"-bias={quote(bias_ref.path.resolve())}")
    if params.cfa:
        opts.append("-cfa")
    if params.cosmetic and dark_path is not None:
        opts.append("-cc=dark")
    if params.equalize_cfa and params.cfa:
        opts.append("-equalize_cfa")
    if params.debayer:
        opts.append("-debayer")

    commands = [
        f"cd {quote(group_dir.resolve())}",
        f"calibrate {basename} {' '.join(opts)}",
    ]
    result = runtime.run(
        commands,
        working_dir=group_dir,
        on_log=make_progress_handler(ctx),
        cancel=ctx.cancel,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"calibrate: siril exited {result.returncode}\n"
            f"--- ssf ---\n{result.ssf}\n"
            f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
            f"--- stderr ---\n{result.stderr}"
        )

    outputs = sorted(
        p
        for p in group_dir.iterdir()
        if p.name.startswith(f"pp_{basename}_") and p.suffix in (".fit", ".fits")
    )
    if not outputs:
        raise RuntimeError(
            f"calibrate: siril returned 0 but no pp_{basename}_*.fit frames "
            f"landed in {group_dir}.\n--- stdout (tail) ---\n"
            f"{result.stdout[-2000:]}"
        )

    drop_staged(staged)
    return outputs


@register("calibrate")
class CalibrateNode(Node[CalibrateParams]):
    id = "calibrate"
    version = 2
    uses_siril = True
    preview_hidden = True

    inputs = {
        "sequence": PortType.SEQUENCE_FITS,
        "dark": PortType.MASTER_FITS_LIST,
        "flat": PortType.MASTER_FITS,
        "bias": PortType.MASTER_FITS,
    }
    optional_inputs = frozenset({"dark", "flat", "bias"})
    outputs = {"sequence": PortType.SEQUENCE_FITS}
    params_schema = CalibrateParams

    def estimate_storage_bytes(
        self,
        inputs: dict[str, Ref],
        params: CalibrateParams,
    ) -> int | None:
        del params
        seq_in_ref = inputs.get("sequence")
        if not isinstance(seq_in_ref, Ref):
            return None
        # Calibrate writes one pp_*.fit per input frame at the same dims;
        # the uint16->float32 promotion is what bumps the byte count.
        return estimate_sequence_output_bytes(seq_in_ref.path)

    def run(
        self,
        inputs: dict[str, Ref],
        params: CalibrateParams,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        # `dark` is declared MASTER_FITS_LIST, so the runtime hands us
        # a list[Ref] there. Other ports stay scalar.
        typed_inputs: dict[str, Ref | Sequence[Ref]] = inputs  # type: ignore[assignment]
        out_dir_path = Path(out_dir)  # type: ignore[arg-type]
        seq_in_ref = typed_inputs["sequence"]
        assert isinstance(seq_in_ref, Ref)
        seq_in = seq_in_ref.path
        seq_out = out_dir_path / "sequence"

        dark_value = typed_inputs.get("dark")
        if isinstance(dark_value, Sequence) and not isinstance(dark_value, Ref):
            dark_refs: list[Ref] = list(dark_value)
        elif dark_value is None:
            dark_refs = []
        else:
            # Backwards-compat: a scalar Ref also accepted (eg an explicit
            # override producing a single master).
            dark_refs = [dark_value]

        flat_ref = typed_inputs.get("flat")
        if not isinstance(flat_ref, Ref):
            flat_ref = None
        bias_ref = typed_inputs.get("bias")
        if not isinstance(bias_ref, Ref):
            bias_ref = None

        # Fast path: 0 or 1 darks, no per-frame routing needed; delegate
        # to the original single-master flow so existing cache entries
        # remain valid for the common case.
        if len(dark_refs) <= 1:
            return self._run_single(
                seq_in=seq_in,
                seq_out=seq_out,
                dark_ref=dark_refs[0] if dark_refs else None,
                flat_ref=flat_ref,
                bias_ref=bias_ref,
                params=params,
                ctx=ctx,
            )

        return self._run_multi(
            seq_in=seq_in,
            seq_out=seq_out,
            dark_refs=dark_refs,
            flat_ref=flat_ref,
            bias_ref=bias_ref,
            params=params,
            ctx=ctx,
            out_dir=out_dir_path,
        )

    def _run_single(
        self,
        *,
        seq_in: Path,
        seq_out: Path,
        dark_ref: Ref | None,
        flat_ref: Ref | None,
        bias_ref: Ref | None,
        params: CalibrateParams,
        ctx: RunContext,
    ) -> dict[str, Ref]:
        opts: list[str] = []
        if dark_ref is not None:
            opts.append(f"-dark={quote(dark_ref.path.resolve())}")
        if flat_ref is not None:
            opts.append(f"-flat={quote(flat_ref.path.resolve())}")
        if bias_ref is not None:
            opts.append(f"-bias={quote(bias_ref.path.resolve())}")
        if params.cfa:
            opts.append("-cfa")
        if params.cosmetic and dark_ref is not None:
            opts.append("-cc=dark")
        if params.equalize_cfa and params.cfa:
            opts.append("-equalize_cfa")
        if params.debayer:
            opts.append("-debayer")
        if params.fitseq:
            opts.append("-fitseq")

        out_basename = f"pp_{params.input_basename}"
        ctx.progress(0.2, "calibrate: running siril on sequence")

        if not params.fitseq:
            seq_out.mkdir(parents=True, exist_ok=True)
            if not seq_in.exists():
                raise RuntimeError(f"calibrate: input dir does not exist: {seq_in}")
            staged = stage_sequence(
                seq_in, seq_out, params.input_basename, params.fitseq
            )
            if not staged:
                raise RuntimeError(
                    f"calibrate: no input frames matching basename "
                    f"'{params.input_basename}' under {seq_in}"
                )
            n_input = len([p for p in staged if p.suffix in (".fit", ".fits")])
            commands = [
                f"cd {quote(seq_out.resolve())}",
                f"calibrate {params.input_basename} {' '.join(opts)}",
            ]
            result = SirilRuntime().run(
                commands,
                working_dir=seq_out,
                on_log=make_progress_handler(ctx),
                cancel=ctx.cancel,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"calibrate: siril exited {result.returncode}\n"
                    f"--- ssf ---\n{result.ssf}\n"
                    f"--- stdout (tail) ---\n{result.stdout[-4000:]}\n"
                    f"--- stderr ---\n{result.stderr}"
                )
            frames = sorted(
                p
                for p in seq_out.iterdir()
                if p.name.startswith(f"{out_basename}_")
                and p.suffix in (".fit", ".fits")
            )
            if not frames:
                raise RuntimeError(
                    f"calibrate: siril returned 0 but no {out_basename}_*.fit* "
                    f"frames landed in {seq_out}.\n--- stdout (tail) ---\n"
                    f"{result.stdout[-2000:]}"
                )
            if len(frames) != n_input:
                raise RuntimeError(
                    f"calibrate: expected {n_input} calibrated frames, "
                    f"got {len(frames)}"
                )
            drop_staged(staged)
            wrote = f"{len(frames)} frames"
        else:
            commands = [
                f"cd {quote(seq_out.resolve())}",
                f"calibrate {params.input_basename} {' '.join(opts)}",
            ]
            wrote = run_siril_on_sequence(
                node_name="calibrate",
                seq_in=seq_in,
                seq_out=seq_out,
                commands=commands,
                basename=params.input_basename,
                out_basename=out_basename,
                fitseq=params.fitseq,
                ctx=ctx,
                runtime=SirilRuntime(),
            )

        ctx.progress(1.0, f"calibrate: wrote {wrote}")
        return {"sequence": seq_ref(seq_out)}

    def _run_multi(
        self,
        *,
        seq_in: Path,
        seq_out: Path,
        dark_refs: list[Ref],
        flat_ref: Ref | None,
        bias_ref: Ref | None,
        params: CalibrateParams,
        ctx: RunContext,
        out_dir: Path,
    ) -> dict[str, Ref]:
        seq_out.mkdir(parents=True, exist_ok=True)
        basename = params.input_basename

        ctx.progress(0.02, "calibrate: reading frame headers")

        if params.fitseq:
            fitseq_path = seq_in / f"{basename}.fit"
            if not fitseq_path.exists():
                raise RuntimeError(
                    f"calibrate: FITSEQ container {fitseq_path} not found"
                )
            scratch = out_dir / "_extracted"
            light_paths = _extract_fitseq_frames(fitseq_path, scratch, basename)
        else:
            light_paths = sorted(
                p
                for p in seq_in.iterdir()
                if p.name.startswith(f"{basename}_")
                and p.suffix in (".fit", ".fits")
            )
        if not light_paths:
            raise RuntimeError(
                f"calibrate: no input frames matching basename "
                f"'{basename}' under {seq_in}"
            )

        light_meta = [_read_fits_meta(p) for p in light_paths]
        dark_pool = _build_dark_pool(dark_refs, params.dark_bins)

        # Assign each light to its best-matching dark; uncalibratable
        # frames land under key=None.
        groups: dict[Path | None, list[Path]] = {}
        per_dark_temp_deltas: dict[Path, list[float]] = {}
        for lm in light_meta:
            chosen = _pick_dark(lm, dark_pool)
            key: Path | None = chosen["path"] if chosen is not None else None
            groups.setdefault(key, []).append(lm["path"])
            if (
                chosen is not None
                and chosen["ccd_temp"] is not None
                and lm["ccd_temp"] is not None
            ):
                per_dark_temp_deltas.setdefault(chosen["path"], []).append(
                    abs(chosen["ccd_temp"] - lm["ccd_temp"])
                )

        # Surface fallback warnings BEFORE we potentially drop the
        # uncalibratable bucket: if a chosen dark is more than 5C from
        # any of its frames, the matching is best-effort and the user
        # should know. Picks the worst per-dark delta so a single
        # warning per dark suffices regardless of group size.
        for dark_path, deltas in per_dark_temp_deltas.items():
            worst = max(deltas)
            if worst > _FALLBACK_TEMP_C:
                ctx.warn(
                    "fallback",
                    f"{dark_path.name}: nearest available dark is "
                    f"{worst:.1f}C off some frames (>{_FALLBACK_TEMP_C:g}C "
                    "tolerance); calibration is best-effort.",
                    details={
                        "dark": dark_path.name,
                        "max_delta_c": round(worst, 2),
                        "tolerance_c": _FALLBACK_TEMP_C,
                    },
                )

        uncalibratable_count = len(groups.get(None, []))
        if None in groups and params.exclude_uncalibratable:
            groups.pop(None)
            ctx.warn(
                "partial",
                f"dropped {uncalibratable_count} frames with no matching "
                "dark (exclude_uncalibratable=true).",
                details={"dropped": uncalibratable_count},
            )
        elif uncalibratable_count > 0:
            ctx.warn(
                "partial",
                f"{uncalibratable_count} frames had no matching dark and "
                "ran debayer-only (no dark subtraction). Enable "
                "exclude_uncalibratable to drop them instead.",
                details={"uncalibrated": uncalibratable_count},
            )

        if not groups:
            raise RuntimeError(
                "calibrate: every frame was uncalibratable and "
                "exclude_uncalibratable=True; nothing left to process"
            )

        total = sum(len(v) for v in groups.values())
        done = 0
        # The header-read + dispatch span eats 5% off the top and the
        # final merge another 5%; Siril's per-group progress maps into
        # the 5%-95% middle so the bar moves monotonically across all
        # groups instead of bouncing 0->100 per Siril invocation.
        progress_lo = 0.05
        progress_hi = 0.95

        for i, (dark_path, frames) in enumerate(groups.items(), start=1):
            ctx.progress(
                progress_lo + (progress_hi - progress_lo) * (done / max(total, 1)),
                f"calibrate: group {i}/{len(groups)} "
                f"({len(frames)} frames, dark={dark_path.name if dark_path else 'none'})",
            )
            group_dir = out_dir / f"_group_{i:03d}"
            # Wrap ctx so Siril's per-group 0-1 fraction is scaled into the
            # overall progress window. Without this the bar resets to ~0%
            # at the top of every group (Siril doesn't know it's only
            # processing a slice of the bundle).
            group_base = done
            group_size = len(frames)
            group_ctx = _scoped_progress_ctx(
                ctx,
                base_done=group_base,
                size=group_size,
                total=total,
                progress_lo=progress_lo,
                progress_hi=progress_hi,
            )
            outputs = _run_group_calibrate(
                group_dir=group_dir,
                frames=frames,
                dark_path=dark_path,
                params=params,
                ctx=group_ctx,
                runtime=SirilRuntime(),
                flat_ref=flat_ref,
                bias_ref=bias_ref,
            )
            for out_path in outputs:
                renumbered = seq_out / f"pp_{basename}_{(done + 1):05d}.fit"
                shutil.move(str(out_path), str(renumbered))
                done += 1
            shutil.rmtree(group_dir, ignore_errors=True)

        if params.fitseq:
            shutil.rmtree(out_dir / "_extracted", ignore_errors=True)

        ctx.progress(1.0, f"calibrate: wrote {done} frames across {len(groups)} groups")
        return {"sequence": seq_ref(seq_out)}
