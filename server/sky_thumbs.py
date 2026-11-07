"""Sky thumbnails for the Tonight page, cut from the DSS colour HiPS survey.

A HiPS survey is a pyramid of 512px JPEG tiles on the HEALPix grid. We use
static tiles instead of a cutout service because ESA and CDS both host the
same tree, so either host can answer. Rendered thumbnails stay on disk.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

TILE_BASES = (
    "https://skies.esac.esa.int/DSSColor",
    "https://alasky.cds.unistra.fr/DSS/DSSColor",
)
THUMB_PX = 192
# Bump to discard thumbnails on disk after a rendering change.
RENDER_VERSION = "1"

_TILE_ORDER_BITS = 9  # 512px tiles
_MIN_ORDER = 3  # coarsest level the survey publishes with allsky coverage
_MAX_ORDER = 9  # hips_order of the DSS colour survey
_MIN_FOV_DEG = 0.2
_MAX_FOV_DEG = 6.0
_DEFAULT_FOV_DEG = 0.5
_FRAME_MARGIN = 1.5  # sky shown around the object, as a multiple of its size
_SUPERSAMPLE = 2
_MAX_TILES = 16
_TIMEOUT_S = 10

# Courtesy limit on the public tile hosts, shared by all request threads.
_upstream = threading.BoundedSemaphore(4)


CUSTOM_IMAGE_PX = 384


class InvalidImage(Exception):
    """The supplied bytes are not a readable image."""


class ThumbUnavailable(Exception):
    """No tile host could supply the imagery for a thumbnail."""


def fov_for_size(size_arcmin: float | None) -> float:
    """Field of view in degrees that frames an object of this major axis."""
    if size_arcmin is None or size_arcmin <= 0:
        return _DEFAULT_FOV_DEG
    return min(_MAX_FOV_DEG, max(_MIN_FOV_DEG, size_arcmin * _FRAME_MARGIN / 60.0))


def order_for_fov(fov_deg: float, px: int) -> int:
    """Coarsest tile order whose pixels are no larger than the output pixels."""
    # A HEALPix pixel at order p spans about 58.6 / 2^p degrees.
    pixel_order = math.ceil(math.log2(58.6 * px / fov_deg))
    return max(_MIN_ORDER, min(_MAX_ORDER, pixel_order - _TILE_ORDER_BITS))


def tan_grid(
    ra_deg: float, dec_deg: float, fov_deg: float, px: int
) -> tuple[np.ndarray, np.ndarray]:
    """RA/Dec in radians of each output pixel: north up, east left."""
    half = math.tan(math.radians(fov_deg) / 2)
    axis = (np.arange(px) + 0.5) / px * 2 - 1
    xi, eta = np.meshgrid(-axis * half, -axis * half)
    ra0, dec0 = math.radians(ra_deg), math.radians(dec_deg)
    denom = math.cos(dec0) - eta * math.sin(dec0)
    ra = ra0 + np.arctan2(xi, denom)
    dec = np.arctan2(
        (math.sin(dec0) + eta * math.cos(dec0)) * np.cos(ra - ra0), denom
    )
    return np.mod(ra, 2 * math.pi), dec


def ang_to_xyf(
    ra: np.ndarray, dec: np.ndarray, pixel_order: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """HEALPix nested (ix, iy, face) of each direction, at `pixel_order`."""
    nside = 1 << pixel_order
    z = np.sin(dec)
    za = np.abs(z)
    tt = np.mod(ra / (math.pi / 2), 4.0)

    ix = np.zeros(ra.shape, dtype=np.int64)
    iy = np.zeros(ra.shape, dtype=np.int64)
    face = np.zeros(ra.shape, dtype=np.int64)

    equatorial = za <= 2.0 / 3.0
    t1 = nside * (0.5 + tt[equatorial])
    t2 = nside * z[equatorial] * 0.75
    jp = np.floor(t1 - t2).astype(np.int64)
    jm = np.floor(t1 + t2).astype(np.int64)
    ifp = jp >> pixel_order
    ifm = jm >> pixel_order
    face[equatorial] = np.where(
        ifp == ifm, (ifp & 3) + 4, np.where(ifp < ifm, ifp & 3, (ifm & 3) + 8)
    )
    ix[equatorial] = jm & (nside - 1)
    iy[equatorial] = nside - (jp & (nside - 1)) - 1

    polar = ~equatorial
    ntt = np.minimum(3, np.floor(tt[polar]).astype(np.int64))
    tp = tt[polar] - ntt
    tmp = nside * np.sqrt(3 * (1 - za[polar]))
    jp = np.minimum(nside - 1, np.floor(tp * tmp).astype(np.int64))
    jm = np.minimum(nside - 1, np.floor((1 - tp) * tmp).astype(np.int64))
    north = z[polar] >= 0
    face[polar] = np.where(north, ntt, ntt + 8)
    ix[polar] = np.where(north, nside - jm - 1, jp)
    iy[polar] = np.where(north, nside - jp - 1, jm)
    return ix, iy, face


def _interleave(x: int, y: int) -> int:
    out = 0
    for bit in range(30):
        out |= ((x >> bit) & 1) << (2 * bit) | ((y >> bit) & 1) << (2 * bit + 1)
    return out


def tile_npix(face: int, tx: int, ty: int, order: int) -> int:
    """Nested HEALPix index of the tile at (tx, ty) on a base face."""
    return (face << (2 * order)) + _interleave(tx, ty)


def tile_path(order: int, npix: int) -> str:
    return f"Norder{order}/Dir{npix // 10000 * 10000}/Npix{npix}.jpg"


def fetch_tile(order: int, npix: int) -> np.ndarray | None:
    """RGB tile as an array. None means the survey has no such tile."""
    last_error: Exception | None = None
    for base in TILE_BASES:
        url = f"{base}/{tile_path(order, npix)}"
        try:
            with _upstream, urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
                data = resp.read()
            return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            last_error = exc
        except (OSError, ValueError) as exc:
            last_error = exc
    raise ThumbUnavailable(f"no tile host answered: {last_error}")


def render(
    ra_deg: float,
    dec_deg: float,
    fov_deg: float,
    px: int = THUMB_PX,
    fetch: Callable[[int, int], np.ndarray | None] = fetch_tile,
) -> bytes:
    """Render a JPEG of the sky at this position, north up."""
    work_px = px * _SUPERSAMPLE
    order = order_for_fov(fov_deg, work_px)
    ra, dec = tan_grid(ra_deg, dec_deg, fov_deg, work_px)
    ix, iy, face = ang_to_xyf(ra, dec, order + _TILE_ORDER_BITS)
    tx, ty = ix >> _TILE_ORDER_BITS, iy >> _TILE_ORDER_BITS
    mask = (1 << _TILE_ORDER_BITS) - 1
    px_x, px_y = ix & mask, iy & mask

    keys = np.stack([face, tx, ty], axis=-1).reshape(-1, 3)
    tiles = np.unique(keys, axis=0)
    if len(tiles) > _MAX_TILES:
        raise ThumbUnavailable(f"{len(tiles)} tiles exceeds the limit of {_MAX_TILES}")

    out = np.zeros((work_px, work_px, 3), dtype=np.uint8)
    for f, x, y in tiles.tolist():
        tile = fetch(order, tile_npix(f, x, y, order))
        if tile is None:
            continue
        hit = (face == f) & (tx == x) & (ty == y)
        # HiPS tiles store the HEALPix x index along rows and y along columns.
        out[hit] = tile[px_x[hit], px_y[hit]]

    buf = io.BytesIO()
    Image.fromarray(out).reduce(_SUPERSAMPLE).save(buf, "JPEG", quality=82)
    return buf.getvalue()


def thumb_path(root: Path, name: str) -> Path:
    digest = hashlib.sha1(name.encode()).hexdigest()
    return root / f"v{RENDER_VERSION}" / f"{digest}.jpg"


def get_thumb(
    root: Path, name: str, ra_deg: float, dec_deg: float, size_arcmin: float | None
) -> bytes:
    """Return the cached thumbnail, and render it first if necessary."""
    path = thumb_path(root, name)
    if path.exists():
        return path.read_bytes()
    jpeg = render(ra_deg, dec_deg, fov_for_size(size_arcmin))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(jpeg)
    os.replace(tmp, path)
    return jpeg


def custom_image_path(root: Path, name: str) -> Path:
    digest = hashlib.sha1(name.encode()).hexdigest()
    return root / f"{digest}.jpg"


def save_custom_image(root: Path, name: str, data: bytes) -> None:
    """Crop the user's image to a centred square and store it for `name`."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            square = ImageOps.fit(
                ImageOps.exif_transpose(img).convert("RGB"),
                (CUSTOM_IMAGE_PX, CUSTOM_IMAGE_PX),
                Image.Resampling.LANCZOS,
            )
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise InvalidImage(str(exc)) from exc
    buf = io.BytesIO()
    square.save(buf, "JPEG", quality=88)
    path = custom_image_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, path)
