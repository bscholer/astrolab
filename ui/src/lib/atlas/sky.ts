// Sky math for the atlas: unit vectors in J2000 equatorial coordinates,
// the local horizon, and the stereographic view the map draws with.
// The horizon ignores precession (a third of a degree since J2000), which
// is far below what the map can show at horizon scale.

import type { Vec3 } from './healpix';

export type { Vec3 };

const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

export function radec(raDeg: number, decDeg: number): Vec3 {
  const ra = raDeg * D2R;
  const dec = decDeg * D2R;
  return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
}

export function toRaDec(v: Vec3): { ra: number; dec: number } {
  const ra = Math.atan2(v[1], v[0]) * R2D;
  return { ra: (ra + 360) % 360, dec: Math.asin(Math.max(-1, Math.min(1, v[2]))) * R2D };
}

export function dot(a: Vec3, b: Vec3): number {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

export function cross(a: Vec3, b: Vec3): Vec3 {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

export function normalize(a: Vec3): Vec3 {
  const n = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / n, a[1] / n, a[2] / n];
}

/** Local sidereal time in degrees. */
export function lstDeg(timeMs: number, lonDeg: number): number {
  const jd = timeMs / 86_400_000 + 2440587.5;
  const d = jd - 2451545.0;
  const gmst = 280.46061837 + 360.98564736629 * d;
  return (((gmst + lonDeg) % 360) + 360) % 360;
}

/** Zenith, north point and east point of the horizon, in equatorial coordinates. */
export interface Horizon {
  zenith: Vec3;
  north: Vec3;
  east: Vec3;
}

export function horizon(timeMs: number, latDeg: number, lonDeg: number): Horizon {
  const zenith = radec(lstDeg(timeMs, lonDeg), latDeg);
  const pole: Vec3 = [0, 0, 1];
  const d = dot(pole, zenith);
  // At a pole every direction is south or north; pick the meridian of RA 0.
  const north =
    Math.abs(d) > 0.999999
      ? normalize(cross(cross(zenith, [1, 0, 0]), zenith))
      : normalize([pole[0] - d * zenith[0], pole[1] - d * zenith[1], pole[2] - d * zenith[2]]);
  return { zenith, north, east: cross(north, zenith) };
}

/** Direction of an altitude and azimuth (north 0, east 90). */
export function altAzVec(h: Horizon, altDeg: number, azDeg: number): Vec3 {
  const alt = altDeg * D2R;
  const az = azDeg * D2R;
  const c = Math.cos(alt);
  const n = c * Math.cos(az);
  const e = c * Math.sin(az);
  const z = Math.sin(alt);
  return [
    n * h.north[0] + e * h.east[0] + z * h.zenith[0],
    n * h.north[1] + e * h.east[1] + z * h.zenith[1],
    n * h.north[2] + e * h.east[2] + z * h.zenith[2],
  ];
}

export function toAltAz(h: Horizon, v: Vec3): { alt: number; az: number } {
  const alt = Math.asin(Math.max(-1, Math.min(1, dot(v, h.zenith)))) * R2D;
  const az = Math.atan2(dot(v, h.east), dot(v, h.north)) * R2D;
  return { alt, az: (az + 360) % 360 };
}

/**
 * A stereographic view looking out from the centre of the sphere. `up` is
 * the direction that points up the screen: the celestial pole, or the
 * zenith. East sits left of north, as on the sky.
 */
export class View {
  /** Rows of the rotation into the camera frame: right, up, forward. */
  right: Vec3;
  up: Vec3;
  fwd: Vec3;
  /** Screen pixels per unit of tan(angle / 2). */
  k: number;
  cx: number;
  cy: number;

  fovDeg: number;
  width: number;
  height: number;

  constructor(center: Vec3, upRef: Vec3, fovDeg: number, width: number, height: number) {
    this.fovDeg = fovDeg;
    this.width = width;
    this.height = height;
    this.fwd = normalize(center);
    let r = cross(this.fwd, upRef);
    if (Math.hypot(r[0], r[1], r[2]) < 1e-9) r = cross(this.fwd, [1, 0, 0]);
    this.right = normalize(r);
    this.up = cross(this.right, this.fwd);
    // The field of view spans the longer side of the screen.
    this.k = Math.max(width, height) / 2 / Math.tan((fovDeg * D2R) / 4);
    this.cx = width / 2;
    this.cy = height / 2;
  }

  /** Screen position of a direction, or null when it is behind the viewer. */
  project(v: Vec3): { x: number; y: number; z: number } | null {
    const z = dot(v, this.fwd);
    if (z < -0.2) return null;
    const d = 1 + z;
    return {
      x: this.cx + (dot(v, this.right) / d) * this.k,
      y: this.cy - (dot(v, this.up) / d) * this.k,
      z,
    };
  }

  /** Direction under a screen position. */
  unproject(x: number, y: number): Vec3 {
    const px = (x - this.cx) / this.k;
    const py = -(y - this.cy) / this.k;
    const r2 = px * px + py * py;
    const a = 2 / (1 + r2);
    const cx = a * px;
    const cy = a * py;
    const cz = (1 - r2) / (1 + r2);
    return [
      cx * this.right[0] + cy * this.up[0] + cz * this.fwd[0],
      cx * this.right[1] + cy * this.up[1] + cz * this.fwd[1],
      cx * this.right[2] + cy * this.up[2] + cz * this.fwd[2],
    ];
  }

  /** Screen pixels per degree at the centre. */
  get pxPerDeg(): number {
    return (this.k / 2) * D2R;
  }
}

/** Great-circle angle between two unit vectors, in degrees. */
export function separationDeg(a: Vec3, b: Vec3): number {
  return Math.acos(Math.max(-1, Math.min(1, dot(a, b)))) * R2D;
}

/** A point `distDeg` away from `v` towards position angle `paDeg`
 *  (north through east), for drawing outlines on the sky. */
export function offset(v: Vec3, paDeg: number, distDeg: number): Vec3 {
  const pole: Vec3 = [0, 0, 1];
  let east = cross(pole, v);
  if (Math.hypot(east[0], east[1], east[2]) < 1e-9) east = [0, 1, 0];
  east = normalize(east);
  const north = cross(v, east);
  const pa = paDeg * D2R;
  const d = distDeg * D2R;
  const s = Math.sin(d);
  const c = Math.cos(d);
  const dn = Math.cos(pa) * s;
  const de = Math.sin(pa) * s;
  return [
    v[0] * c + north[0] * dn + east[0] * de,
    v[1] * c + north[1] * dn + east[1] * de,
    v[2] * c + north[2] * dn + east[2] * de,
  ];
}
