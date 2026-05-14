"""Per-node disk-space estimators.

The runtime calls Node.estimate_storage_bytes(inputs, params) right before
it reserves a cache entry, then makes sure that much room exists on disk
(sweeping cache or raising InsufficientStorageError as needed). This file
is the shared helper that pre-stack sequence nodes use to compute the
estimate without each node duplicating FITS header parsing.

Formula matches Siril's own preflight check in src/io/sequence.c
(seq_compute_size) and src/registration/registration.c (the scale-aware
hook): output_bytes = nb_frames * (rx * ry * nb_layers * bps + FITS_HDU).

Rather than parse the .seq file to recover nb_frames and rx/ry, we lean
on the input sequence's on-disk bytes (which already encodes
nb_frames * per-frame bytes) and scale that by (output_bps/input_bps)
and (scale**2). One sample FITS header read tells us input_bps via
BITPIX. That's accurate when the operation doesn't change frame count
(calibrate, seq_register, seq_resample, etc.); for upstream nodes that
expand the frame count (none today) the estimator would need to widen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from astropy.io import fits

log = logging.getLogger("astrolab.storage_estimate")


SIRIL_OUTPUT_BPS_FLOAT32: int = 4
"""Siril 1.4+ writes float32 by default. Nodes that explicitly force
16-bit output should pass output_bps=2."""

FITS_HDU_BYTES: int = 5760
"""Two 2880-byte FITS blocks per Siril's internal frame-size formula."""


def _sample_fits_bitpix(seq_dir: Path) -> int | None:
    """Return absolute BITPIX from the first FITS we can open under seq_dir.

    Returns None if no FITS is found or every probe raises. Failing soft
    is intentional: a missing/bad header should skip estimation, not crash
    the job.
    """
    for f in seq_dir.rglob("*.fit*"):
        try:
            with fits.open(f, memmap=True) as hdul:
                primary = hdul[0]  # noqa: F841 (pyright stub doesn't see __getitem__)
                header = getattr(primary, "header", None)
                if header is None:
                    continue
                bitpix = header.get("BITPIX")
                if isinstance(bitpix, int):
                    return abs(bitpix)
        except Exception:  # noqa: BLE001 (best-effort probe)
            continue
    return None


def _seq_total_bytes(seq_dir: Path) -> int:
    """Sum sizes of every FITS file under seq_dir (recursively)."""
    return sum(f.stat().st_size for f in seq_dir.rglob("*.fit*"))


def estimate_sequence_output_bytes(
    input_seq_dir: Path,
    *,
    scale: float = 1.0,
    output_bps: int = SIRIL_OUTPUT_BPS_FLOAT32,
) -> int | None:
    """Estimate bytes the output sequence will take on disk.

    Approximates Siril's seq_compute_size by scaling the input directory's
    total FITS bytes for bit-depth and spatial-scale changes. Returns None
    when the input dir is empty or no FITS header can be read.

    `scale` is the per-axis linear scale (drizzle_scale, resample factor);
    the formula applies scale**2 since output is rx*scale by ry*scale.
    """
    if not input_seq_dir.exists() or not input_seq_dir.is_dir():
        log.debug("estimate: input not a dir: %s", input_seq_dir)
        return None
    total_in = _seq_total_bytes(input_seq_dir)
    if total_in == 0:
        log.debug("estimate: no FITS under %s", input_seq_dir)
        return None
    in_bitpix = _sample_fits_bitpix(input_seq_dir)
    if in_bitpix is None:
        log.debug("estimate: no readable BITPIX under %s", input_seq_dir)
        return None
    in_bps = in_bitpix // 8
    if in_bps <= 0:
        return None
    return int(total_in * (output_bps / in_bps) * (scale * scale))


def measure_actual_bytes(out_dir: Path) -> int:
    """Sum every file under out_dir. Called post-run for estimate-vs-actual
    logging so the per-node multiplier can be tuned against real data."""
    if not out_dir.exists():
        return 0
    return sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file())
