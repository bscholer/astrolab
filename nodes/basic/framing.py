"""Framing geometry: a crop box on the sky or on the canvas, rotated or not.

Shared by the crop node (which cuts the box out) and the API the crop
editor calls (which converts between what the editor draws and what is
stored).

Coordinates
-----------
- Canvas (x, r): continuous pixel coordinates of the FITS array, x along
  columns, r along rows, pixel (i, j) covering [i, i+1] x [j, j+1]. Array
  index = coordinate - 0.5.
- Display (X, Y): what the previews and the saved picture show. Siril
  writes FITS rows bottom-up (row 0 at the bottom), the astronomical
  convention, so X = x and Y = height - r. On a plate-solved stack from
  this pipeline that is north up, east left.
- A box is (centre, width, height, theta): theta in radians turns the box
  counter-clockwise on the canvas (x right, r up). On the display that is
  the same turn, and the editor's angle phi (degrees clockwise on screen)
  is -theta.

A sky box is (ra, dec) of the centre, width and height in arcminutes, and
the position angle of the picture's up direction (degrees east of north,
0 = north up). It maps to the same patch of sky on any canvas with a WCS,
so the framing survives a new night growing or shifting the combined
canvas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from astropy.io import fits

ARCMIN_PER_DEG = 60.0


@dataclass(frozen=True)
class CanvasBox:
    """A (possibly rotated) box on the canvas, in canvas coordinates."""

    cx: float
    cr: float
    width: float
    height: float
    theta: float = 0.0  # radians, counter-clockwise with r up

    @property
    def phi(self) -> float:
        """Display angle: degrees clockwise on screen."""
        return _wrap_deg(-math.degrees(self.theta))


@dataclass(frozen=True)
class SkyBox:
    ra: float
    dec: float
    width_arcmin: float
    height_arcmin: float
    angle: float = 0.0  # position angle of "up", degrees east of north


def _wrap_deg(a: float) -> float:
    a = math.fmod(a, 360.0)
    if a < 0:
        a += 360.0
    return 0.0 if abs(a - 360.0) < 1e-9 else a


# ---------------------------------------------------------------------------
# WCS
# ---------------------------------------------------------------------------


def celestial_wcs(header: fits.Header):  # -> astropy.wcs.WCS | None
    """The 2-D celestial WCS of a plate-solved image, else None."""
    if not str(header.get("CTYPE1", "")).startswith("RA"):
        return None
    import warnings

    from astropy.wcs import WCS, FITSFixedWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        try:
            w = WCS(header, naxis=2)
        except Exception:
            return None
    return w if w.has_celestial else None


@dataclass(frozen=True)
class _Local:
    """The sky around one canvas point: unit vectors to north and east in
    canvas coordinates, and the scale in degrees per pixel."""

    north: np.ndarray
    east: np.ndarray
    scale_deg: float


def _local(w, cx: float, cr: float) -> _Local:
    from astropy.coordinates import SkyCoord

    centre = w.pixel_to_world(cx - 0.5, cr - 0.5)
    step = 1.0 / ARCMIN_PER_DEG  # one arcminute
    c = np.array([cx, cr])

    def towards(offset_ra: float, offset_dec: float) -> np.ndarray:
        dec = centre.dec.deg + offset_dec
        ra = centre.ra.deg + offset_ra / max(math.cos(math.radians(centre.dec.deg)), 1e-6)
        px = np.array(w.world_to_pixel(SkyCoord(ra, dec, unit="deg"))) + 0.5
        return px - c

    n, e = towards(0.0, step), towards(step, 0.0)
    scale = step / math.sqrt(abs(n[0] * e[1] - n[1] * e[0]))
    return _Local(n / np.linalg.norm(n), e / np.linalg.norm(e), scale)


def sky_to_canvas(w, sky: SkyBox) -> CanvasBox:
    from astropy.coordinates import SkyCoord

    cx, cr = (np.array(w.world_to_pixel(SkyCoord(sky.ra, sky.dec, unit="deg"))) + 0.5).tolist()
    loc = _local(w, cx, cr)
    pa = math.radians(sky.angle)
    up = math.cos(pa) * loc.north + math.sin(pa) * loc.east
    theta = math.atan2(-up[0], up[1])
    px_per_arcmin = 1.0 / (loc.scale_deg * ARCMIN_PER_DEG)
    return CanvasBox(
        cx, cr, sky.width_arcmin * px_per_arcmin, sky.height_arcmin * px_per_arcmin, theta
    )


def canvas_to_sky(w, box: CanvasBox) -> SkyBox:
    centre = w.pixel_to_world(box.cx - 0.5, box.cr - 0.5)
    loc = _local(w, box.cx, box.cr)
    up = np.array([-math.sin(box.theta), math.cos(box.theta)])
    pa = math.degrees(math.atan2(float(up @ loc.east), float(up @ loc.north)))
    arcmin_per_px = loc.scale_deg * ARCMIN_PER_DEG
    return SkyBox(
        float(centre.ra.deg),
        float(centre.dec.deg),
        box.width * arcmin_per_px,
        box.height * arcmin_per_px,
        _wrap_deg(pa),
    )


def north_display_angle(w, height: int, width: int) -> float:
    """Where north points on the display, degrees clockwise from up."""
    loc = _local(w, width / 2, height / 2)
    return _wrap_deg(math.degrees(math.atan2(loc.north[0], loc.north[1])))


# ---------------------------------------------------------------------------
# Display <-> canvas
# ---------------------------------------------------------------------------


def display_to_canvas(
    height: int, cx: float, cy: float, w: float, h: float, phi_deg: float
) -> CanvasBox:
    """A box as the editor draws it (display pixels, Y down, phi clockwise)."""
    return CanvasBox(cx, height - cy, w, h, -math.radians(phi_deg))


def canvas_to_display(height: int, box: CanvasBox) -> dict[str, float]:
    return {
        "cx": box.cx,
        "cy": height - box.cr,
        "width": box.width,
        "height": box.height,
        "angle": box.phi,
    }


# ---------------------------------------------------------------------------
# Cutting a box out
# ---------------------------------------------------------------------------


def _rotation(theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    return np.array([[c, -s], [s, c]])


def _keys(t: np.ndarray) -> list[np.ndarray]:
    """Cubic convolution weights (Keys, a = -0.5) for taps -1, 0, 1, 2."""
    a = -0.5

    def near(d: np.ndarray) -> np.ndarray:
        return (a + 2) * d**3 - (a + 3) * d**2 + 1

    def far(d: np.ndarray) -> np.ndarray:
        return a * d**3 - 5 * a * d**2 + 8 * a * d - 4 * a

    return [far(1 + t), near(t), near(1 - t), far(2 - t)]


def _is_axis_aligned(box: CanvasBox) -> bool:
    if abs(math.sin(box.theta)) > 1e-9 or math.cos(box.theta) < 0:
        return False
    out_w, out_h = max(1, round(box.width)), max(1, round(box.height))
    x0, r0 = box.cx - out_w / 2, box.cr - out_h / 2
    return abs(x0 - round(x0)) < 1e-6 and abs(r0 - round(r0)) < 1e-6


def cut(planes: np.ndarray, box: CanvasBox) -> np.ndarray:
    """Cut `box` out of (C, H, W) planes; outside the canvas becomes 0 (nodata).

    Axis-aligned boxes on whole pixels are sliced. Anything else is
    resampled with cubic convolution, which keeps stars sharper than
    bilinear. A pixel whose nearest source pixel is nodata stays nodata,
    so the registration border does not smear into the picture.
    """
    _, height, width = planes.shape
    out_w, out_h = max(1, round(box.width)), max(1, round(box.height))
    if _is_axis_aligned(box):
        x0, r0 = round(box.cx - out_w / 2), round(box.cr - out_h / 2)
        out = np.zeros((planes.shape[0], out_h, out_w), planes.dtype)
        sx0, sx1 = max(0, x0), min(width, x0 + out_w)
        sr0, sr1 = max(0, r0), min(height, r0 + out_h)
        if sx1 > sx0 and sr1 > sr0:
            out[:, sr0 - r0 : sr1 - r0, sx0 - x0 : sx1 - x0] = planes[:, sr0:sr1, sx0:sx1]
        return out

    rot = _rotation(box.theta)
    oy, ox = np.mgrid[0:out_h, 0:out_w].astype(np.float64)
    ox += 0.5 - out_w / 2
    oy += 0.5 - out_h / 2
    # Source array indices for each output pixel centre.
    sx = box.cx + rot[0, 0] * ox + rot[0, 1] * oy - 0.5
    sr = box.cr + rot[1, 0] * ox + rot[1, 1] * oy - 0.5
    del ox, oy
    nx, nr = np.rint(sx).astype(np.int64), np.rint(sr).astype(np.int64)
    inside = (nx >= 0) & (nx < width) & (nr >= 0) & (nr < height)
    ix0, ir0 = np.floor(sx).astype(np.int64), np.floor(sr).astype(np.int64)
    wx = [k.astype(np.float32) for k in _keys((sx - ix0).astype(np.float32))]
    wr = [k.astype(np.float32) for k in _keys((sr - ir0).astype(np.float32))]
    del sx, sr
    xs = [np.clip(ix0 + d, 0, width - 1) for d in (-1, 0, 1, 2)]
    rs = [np.clip(ir0 + d, 0, height - 1) for d in (-1, 0, 1, 2)]
    nx, nr = np.clip(nx, 0, width - 1), np.clip(nr, 0, height - 1)
    out = np.zeros((planes.shape[0], out_h, out_w), np.float32)
    for c in range(planes.shape[0]):
        p = np.nan_to_num(planes[c].astype(np.float32, copy=False))
        acc = np.zeros((out_h, out_w), np.float32)
        for j in range(4):
            row = np.zeros((out_h, out_w), np.float32)
            for i in range(4):
                row += wx[i] * p[rs[j], xs[i]]
            acc += wr[j] * row
        lit = inside & (p[nr, nx] != 0)
        # Ringing must not dip a real pixel to 0 (that reads as nodata).
        out[c] = np.where(lit, np.maximum(acc, 1e-6), 0.0)
    return out.astype(planes.dtype, copy=False)


_WCS_PREFIXES = (
    "CRPIX", "CD1_", "CD2_", "PC1_", "PC2_", "CDELT", "CROTA", "A_", "B_", "AP_", "BP_"
)


def cut_header(header: fits.Header, box: CanvasBox, out_w: int, out_h: int) -> fits.Header:
    """The input header with its WCS moved onto the cut-out.

    Output index p_out maps to input index c_in + R (p_out - c_out), so the
    new reference pixel is c_out + R^T (crpix - c_in) and the new CD matrix
    CD R. SIP distortion terms are dropped (a linear TAN over the crop).
    """
    out = header.copy()
    w = celestial_wcs(header)
    if w is None:
        # No sky solution: only the reference pixel would mean anything.
        if "CRPIX1" in out and abs(math.sin(box.theta)) < 1e-9:
            out["CRPIX1"] = float(out["CRPIX1"]) - round(box.cx - out_w / 2)
            out["CRPIX2"] = float(out["CRPIX2"]) - round(box.cr - out_h / 2)
        return out
    rot = _rotation(box.theta)
    c_in = np.array([box.cx - 0.5, box.cr - 0.5])
    c_out = np.array([out_w / 2 - 0.5, out_h / 2 - 0.5])
    crpix0 = np.array(w.wcs.crpix) - 1.0
    new_crpix0 = c_out + rot.T @ (crpix0 - c_in)
    cd = w.pixel_scale_matrix @ rot
    for key in list(out.keys()):
        if key.startswith(_WCS_PREFIXES):
            del out[key]
    ctype = [t.replace("-SIP", "") for t in w.wcs.ctype]
    out["CTYPE1"], out["CTYPE2"] = ctype[0], ctype[1]
    out["CRPIX1"], out["CRPIX2"] = float(new_crpix0[0] + 1), float(new_crpix0[1] + 1)
    out["CD1_1"], out["CD1_2"] = float(cd[0, 0]), float(cd[0, 1])
    out["CD2_1"], out["CD2_2"] = float(cd[1, 0]), float(cd[1, 1])
    return out


# ---------------------------------------------------------------------------
# The crop editor's view: stored params <-> a box on the displayed canvas
# ---------------------------------------------------------------------------

SKY_KEYS = ("ra", "dec", "sky_width", "sky_height", "angle")
CANVAS_KEYS = ("x", "y", "width", "height", "rotation")


def canvas_info(header: fits.Header, height: int, width: int, params: dict) -> dict:
    """What the editor needs to draw the stored framing on this canvas.

    `box` is in display pixels of the full-size image (cx, cy, width,
    height, angle clockwise), or None when no box is set (auto-trim).
    `north_angle` is where north points on the display, for the editor's
    north-up snaps; None without a plate solve.
    """
    w = celestial_wcs(header)
    box = None
    if float(params.get("sky_width") or 0) > 0 and float(params.get("sky_height") or 0) > 0:
        if w is not None:
            sky = SkyBox(
                float(params.get("ra") or 0),
                float(params.get("dec") or 0),
                float(params["sky_width"]),
                float(params["sky_height"]),
                float(params.get("angle") or 0),
            )
            box = canvas_to_display(height, sky_to_canvas(w, sky))
    elif float(params.get("width") or 0) > 0:
        x, y = float(params.get("x") or 0), float(params.get("y") or 0)
        bw, bh = float(params["width"]), float(params.get("height") or 0)
        box = {
            "cx": (x + bw / 2) * width,
            "cy": (y + bh / 2) * height,
            "width": bw * width,
            "height": bh * height,
            "angle": _wrap_deg(float(params.get("rotation") or 0)),
        }
    scale = None
    north = None
    if w is not None:
        loc = _local(w, width / 2, height / 2)
        scale = loc.scale_deg * 3600
        north = north_display_angle(w, height, width)
    return {
        "width": width,
        "height": height,
        "solved": w is not None,
        "north_angle": north,
        "arcsec_per_px": scale,
        "box": box,
    }


def params_for_box(header: fits.Header, height: int, width: int, box: dict) -> dict:
    """The crop params to store for a box drawn on this canvas.

    On a plate-solved canvas the box is stored on the sky (and the canvas
    fields zeroed); without a solve, as fractions of the displayed canvas.
    """
    phi = _wrap_deg(float(box.get("angle") or 0))
    cx, cy = float(box["cx"]), float(box["cy"])
    bw, bh = float(box["width"]), float(box["height"])
    w = celestial_wcs(header)
    if w is not None:
        sky = canvas_to_sky(w, display_to_canvas(height, cx, cy, bw, bh, phi))
        return {
            "ra": round(sky.ra, 6),
            "dec": round(sky.dec, 6),
            "sky_width": round(sky.width_arcmin, 3),
            "sky_height": round(sky.height_arcmin, 3),
            "angle": round(sky.angle, 3),
            **{k: 0.0 for k in CANVAS_KEYS},
        }
    nx, ny = (cx - bw / 2) / width, (cy - bh / 2) / height
    return {
        "x": round(min(max(nx, 0.0), 1.0), 4),
        "y": round(min(max(ny, 0.0), 1.0), 4),
        "width": round(min(bw / width, 1.0), 4),
        "height": round(min(bh / height, 1.0), 4),
        "rotation": round(phi, 3),
        **{k: 0.0 for k in SKY_KEYS},
    }
