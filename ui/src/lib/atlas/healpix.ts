// HEALPix (nested scheme) geometry for drawing HiPS tiles. Only what the
// atlas needs: tile corners and an interior grid as unit vectors. Formulas
// follow healpix_base's xyf2loc, the inverse of server/sky_thumbs.ang_to_xyf.

export type Vec3 = [number, number, number];

const JRLL = [2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4];
const JPLL = [1, 3, 5, 7, 0, 2, 4, 6, 1, 3, 5, 7];

/** Number of tiles at an order: 12 base faces, each split 4^order ways. */
export function tileCount(order: number): number {
  return 12 * 4 ** order;
}

function compact(v: number): number {
  // Gather the even bits of v (up to 2 * 15 bits) into the low bits.
  let out = 0;
  for (let bit = 0; bit < 15; bit++) out |= ((v >>> (2 * bit)) & 1) << bit;
  return out;
}

/** Face and in-face tile coordinates of a nested tile index. */
export function tileXYF(order: number, npix: number): { face: number; tx: number; ty: number } {
  const per = 4 ** order;
  const face = Math.floor(npix / per);
  const ipf = npix - face * per;
  return { face, tx: compact(ipf), ty: compact(ipf >>> 1) };
}

/** Unit vector of the point (x, y) on a base face, both in 0..1. */
export function faceToVec(x: number, y: number, face: number): Vec3 {
  const jr = JRLL[face] - x - y;
  let nr: number;
  let z: number;
  if (jr < 1) {
    nr = jr;
    z = 1 - (nr * nr) / 3;
  } else if (jr > 3) {
    nr = 4 - jr;
    z = (nr * nr) / 3 - 1;
  } else {
    nr = 1;
    z = ((2 - jr) * 2) / 3;
  }
  let t = JPLL[face] * nr + x - y;
  if (t < 0) t += 8;
  if (t >= 8) t -= 8;
  const phi = nr < 1e-15 ? 0 : (Math.PI / 4) * (t / nr);
  const s = Math.sqrt(Math.max(0, 1 - z * z));
  return [s * Math.cos(phi), s * Math.sin(phi), z];
}

/** Point at fractions (fx, fy) across a tile, as a unit vector. fx runs
 *  along the HEALPix x index, which HiPS JPEG tiles store down the rows. */
export function tilePoint(order: number, npix: number, fx: number, fy: number): Vec3 {
  const { face, tx, ty } = tileXYF(order, npix);
  const nside = 2 ** order;
  return faceToVec((tx + fx) / nside, (ty + fy) / nside, face);
}

/** The four children of a tile at the next order. */
export function children(npix: number): number[] {
  const base = npix * 4;
  return [base, base + 1, base + 2, base + 3];
}

/** HiPS path of a tile, e.g. Norder5/Dir0/Npix123.jpg. */
export function tilePath(order: number, npix: number): string {
  return `Norder${order}/Dir${Math.floor(npix / 10000) * 10000}/Npix${npix}.jpg`;
}
