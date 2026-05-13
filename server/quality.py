"""Quality metrics computation for completed image-output jobs.

Kept in its own module so both api.py (for the /quality endpoint) and
jobs.py (worker _terminate hook) can import it without creating a cycle.
"""

from __future__ import annotations

import logging
import statistics
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .jobs import JobRecord

log = logging.getLogger("astrolab.quality")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SIRIL_WARNING_PHRASES = (
    "calibration frames are probably incorrect",
    "negative pixels",
    "After dark subtraction",
    "inconsistent",
    "warning",
)

_IMAGE_PORT_TYPES = {"image/fits", "image/png"}

_CHANNEL_NAMES_BY_COUNT: dict[int, list[str]] = {
    1: ["L"],
    2: ["L", "A"],
    3: ["R", "G", "B"],
    4: ["R", "G", "B", "A"],
}


# ---------------------------------------------------------------------------
# Per-channel and image statistics
# ---------------------------------------------------------------------------


def _channel_stats(arr: np.ndarray) -> dict[str, Any]:
    """Compute per-channel statistics for a 2-D float32 array clamped to [0,1]."""
    flat = arr.ravel().astype(np.float64)
    p01, p50, p99 = np.percentile(flat, [1, 50, 99])
    return {
        "mean": float(np.mean(flat)),
        "median": float(np.median(flat)),
        "stdev": float(np.std(flat)),
        "p01": float(p01),
        "p50": float(p50),
        "p99": float(p99),
        "clipped_low_pct": float(np.mean(flat <= 0.0)),
        "clipped_high_pct": float(np.mean(flat >= 1.0)),
    }


def _background_stats(arr: np.ndarray) -> dict[str, Any]:
    """Rough background: sigma-clipped mean of the lowest 10th-percentile pixels.

    Also returns sigma = std of the sigma-clipped low pool, used as the
    "Noise" indicator in the UI.
    """
    flat = arr.ravel().astype(np.float64)
    threshold = float(np.percentile(flat, 10))
    low = flat[flat <= threshold]
    for _ in range(3):
        m, s = np.mean(low), np.std(low)
        if s == 0:
            break
        low = low[np.abs(low - m) <= 3 * s]
    level = float(np.mean(low)) if len(low) > 0 else float(np.mean(flat))
    sigma = float(np.std(low)) if len(low) > 1 else 0.0
    pct_below = float(np.mean(flat < level))
    return {"estimated_level": level, "pct_below_threshold": pct_below, "sigma": sigma}


def _color_balance(channels: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute R/G and B/G mean ratios. Warn when heavily skewed (>10%)."""
    if len(channels) < 3:
        return {}
    r_mean = channels[0]["mean"]
    g_mean = channels[1]["mean"]
    b_mean = channels[2]["mean"]
    if g_mean == 0:
        return {}
    r_g = r_mean / g_mean
    b_g = b_mean / g_mean
    warning: str | None = None
    if abs(r_g - 1.0) > 0.10 or abs(b_g - 1.0) > 0.10:
        warning = f"color balance skewed: R/G={r_g:.2f} B/G={b_g:.2f}"
    return {"r_g_ratio": round(r_g, 4), "b_g_ratio": round(b_g, 4), "warning": warning}


def _load_image_array(path: Path, port_type: str) -> np.ndarray:
    """Return an (H, W, C) float32 array normalised to [0, 1].

    Supports image/fits and image/png. Raises ValueError for unrecognised types.
    """
    if port_type == "image/fits":
        from astropy.io import fits as astropy_fits

        with astropy_fits.open(str(path)) as hdul:
            data = hdul[0].data  # type: ignore[index]
        if data is None:
            raise ValueError("FITS primary HDU has no data")
        arr = np.array(data, dtype=np.float32)
        lo, hi = arr.min(), arr.max()
        arr = (arr - lo) / (hi - lo) if hi > lo else np.zeros_like(arr)
        if arr.ndim == 2:
            arr = arr[:, :, np.newaxis]
        elif arr.ndim == 3:
            arr = np.moveaxis(arr, 0, -1)
        return arr
    if port_type == "image/png":
        from PIL import Image as PilImage

        img = PilImage.open(str(path)).convert("RGB")
        arr = np.asarray(img, dtype=np.float32) / 255.0
        return arr
    raise ValueError(f"unsupported port type for quality metrics: {port_type!r}")


# ---------------------------------------------------------------------------
# Sharpness
# ---------------------------------------------------------------------------


def _laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the discrete Laplacian of `gray` (2-D float array).

    Uses the 3x3 cross kernel (0,1,0 / 1,-4,1 / 0,1,0) applied via array
    slicing. No scipy required.
    """
    lap = (
        -4.0 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(np.var(lap))


def _run_findstar(image_path: Path, log: logging.Logger) -> dict[str, Any] | None:
    """Run Siril findstar on `image_path` and parse the resulting .lst file.

    Returns a dict with fwhm_px, roundness, star_count, or None when Siril
    is unavailable or the image has too few detectable stars.
    """
    from .siril import SirilNotFound, SirilRuntime

    try:
        runtime = SirilRuntime()
    except SirilNotFound:
        return None

    with tempfile.TemporaryDirectory(prefix="astrolab-findstar-") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        lst_path = tmpdir / "stars.lst"
        commands = [
            f'cd "{tmpdir}"',
            f'load "{image_path.resolve()}"',
            'findstar -out=stars.lst',
        ]
        try:
            result = runtime.run(commands, working_dir=tmpdir, timeout=60.0)
        except Exception as exc:
            log.debug("findstar failed: %s", exc)
            return None

        if result.returncode != 0:
            log.debug("findstar returned %d; skipping FWHM", result.returncode)
            return None

        if not lst_path.exists():
            log.debug("findstar output file missing; skipping FWHM")
            return None

        fwhm_vals: list[float] = []
        roundness_vals: list[float] = []
        try:
            for line in lst_path.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                try:
                    # Common columns: index x y FWHMx FWHMy A B roundness ...
                    fx = float(parts[3])
                    fy = float(parts[4])
                    rnd = float(parts[7])
                except (IndexError, ValueError):
                    continue
                fwhm_vals.append((fx + fy) / 2.0)
                roundness_vals.append(rnd)
        except Exception as exc:
            log.debug("findstar parse error: %s", exc)
            return None

        if len(fwhm_vals) < 3:
            return None

        return {
            "fwhm_px": statistics.median(fwhm_vals),
            "roundness": statistics.median(roundness_vals),
            "star_count": len(fwhm_vals),
        }


# ---------------------------------------------------------------------------
# Siril warnings from job events
# ---------------------------------------------------------------------------


def _siril_warnings(events: list) -> list[str]:
    """Parse a list of JobEvent objects for known Siril warning phrases."""
    warnings: list[str] = []
    for ev in events:
        msg = ev.message or ""
        if not msg:
            continue
        text = msg[5:] if msg.startswith("log: ") else msg
        lower = text.lower()
        if any(phrase.lower() in lower for phrase in _SIRIL_WARNING_PHRASES):
            warnings.append(text.strip())
    return warnings


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_quality_for_record(
    record: JobRecord,
    *,
    events: list | None = None,
) -> dict[str, Any] | None:
    """Compute image quality metrics for a completed job.

    `events` is the list of JobEvent objects for `record`. When None, the
    caller is responsible for supplying it (the worker passes events fetched
    via JobManager; the API endpoint passes None and the caller fetches them).
    Pass an empty list to skip siril_warnings entirely.

    Returns None when the record has no image-typed output or the image
    cannot be loaded. Never raises.
    """
    if not record.outputs:
        return None

    target_ref = None
    for _pub_name, ref in record.outputs.items():
        if str(ref.type) in _IMAGE_PORT_TYPES:
            target_ref = ref
            break

    if target_ref is None:
        return None

    try:
        arr = _load_image_array(target_ref.path, str(target_ref.type))
    except Exception as exc:
        log.warning("quality: failed to load image for job %s: %s", record.id, exc)
        return None

    try:
        h, w, c = arr.shape
        channel_names = _CHANNEL_NAMES_BY_COUNT.get(c, [str(i) for i in range(c)])
        channels: list[dict[str, Any]] = []
        for i in range(c):
            stats = _channel_stats(arr[:, :, i])
            name = channel_names[i] if i < len(channel_names) else str(i)
            channels.append({"name": name, **stats})

        gray = arr[:, :, 0] if c == 1 else np.mean(arr, axis=2)
        background = _background_stats(gray)

        lap_var = _laplacian_variance(gray.astype(np.float32))
        findstar = _run_findstar(target_ref.path, log)
        sharpness: dict[str, Any] = {
            "laplacian_variance": lap_var,
            "fwhm_px": findstar["fwhm_px"] if findstar else None,
            "roundness": findstar["roundness"] if findstar else None,
            "star_count": findstar["star_count"] if findstar else None,
        }

        color_bal = _color_balance(channels)
        dtype_name = arr.dtype.name

        return {
            "output_ref": {"path": str(target_ref.path), "type": str(target_ref.type)},
            "dimensions": {"width": w, "height": h, "channels": c, "dtype": dtype_name},
            "channels": channels,
            "background": background,
            "color_balance": color_bal,
            "siril_warnings": _siril_warnings(events if events is not None else []),
            "sharpness": sharpness,
        }
    except Exception as exc:
        log.warning("quality: computation error for job %s: %s", record.id, exc)
        return None
