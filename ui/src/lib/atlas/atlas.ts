import type { TonightEntry } from '$lib/api';

/** Field of the DWARF 3 telephoto: 3856 x 2180 px of 2 µm behind 150 mm. */
export const TELE_FOV_DEG: [number, number] = [2.95, 1.67];

export interface Layers {
  captured: boolean;
  grid: boolean;
  constellations: boolean;
  labels: boolean;
  frame: boolean;
  ground: boolean;
}

export interface Constellations {
  lines: Record<string, [number, number][][]>;
  names: Record<string, { name: string; ra: number; dec: number }>;
}

// Same list as server/sky.py _UNFEATURED_TYPES: nothing to frame.
const UNFEATURED = new Set(['Star', 'Double Star', 'Duplicate Entry', 'Non-existent', 'Nova']);

export function isFeatured(e: TonightEntry): boolean {
  return !UNFEATURED.has(e.object_type ?? '');
}

/** How much an object deserves a place on a crowded map, in 0..~1.2.
 *  Tonight's score leads; fame, size and brightness count when it is down. */
export function importance(e: TonightEntry): number {
  const size = Math.min((e.size_arcmin ?? 0) / 60, 1);
  const bright = e.magnitude == null ? 0.2 : Math.max(0, Math.min(1, (11 - e.magnitude) / 8));
  const fame = Math.max(e.score, e.messier ? 0.45 : 0, e.common_name ? 0.3 : 0);
  return fame + 0.15 * size + 0.1 * bright;
}

/** Field of view that frames an object with room around it. */
export function framingFov(e: TonightEntry): number {
  const size = (e.size_arcmin ?? 10) / 60;
  return Math.max(4.5, Math.min(40, size * 4));
}

/** Overlap between neighbouring mosaic panels, as a share of the frame. */
export const MOSAIC_OVERLAP = 0.2;
export const MOSAIC_MAX_SIDE = 6;

export interface Grid {
  cols: number;
  rows: number;
}

/** A box on the sky, north up, sized in degrees. */
export interface SkyBox {
  ra: number;
  dec: number;
  w: number;
  h: number;
}

/**
 * Panel centres of a mosaic, row by row from the north-east corner. Same
 * layout as server/coverage.plan_mosaic: frames come out north up, so each
 * row keeps one declination and columns step by the frame width there.
 */
export function planMosaic(ra: number, dec: number, grid: Grid, overlap = MOSAIC_OVERLAP): SkyBox[] {
  const [w, h] = TELE_FOV_DEG;
  const stepX = w * (1 - overlap);
  const stepY = h * (1 - overlap);
  const top = dec + ((grid.rows - 1) / 2) * stepY;
  const out: SkyBox[] = [];
  for (let r = 0; r < grid.rows; r++) {
    const d = Math.max(-89, Math.min(89, top - r * stepY));
    const perDeg = 1 / Math.max(Math.cos((d * Math.PI) / 180), 1e-6);
    for (let c = 0; c < grid.cols; c++) {
      const a = ra + ((grid.cols - 1) / 2 - c) * stepX * perDeg;
      out.push({ ra: ((a % 360) + 360) % 360, dec: d, w, h });
    }
  }
  return out;
}

/** Extent of a mosaic in degrees, east-west and north-south. */
export function mosaicSize(grid: Grid, overlap = MOSAIC_OVERLAP): [number, number] {
  const [w, h] = TELE_FOV_DEG;
  return [w + (grid.cols - 1) * w * (1 - overlap), h + (grid.rows - 1) * h * (1 - overlap)];
}

/** The smallest grid that covers an object with a margin, at least two panels. */
export function suggestGrid(e: TonightEntry): Grid {
  const [w, h] = TELE_FOV_DEG;
  const major = ((e.size_arcmin ?? 0) / 60) * 1.15;
  const minor = ((e.minor_arcmin ?? e.size_arcmin ?? 0) / 60) * 1.15;
  // Lay the long axis east-west when the position angle is closer to 90 than 0.
  const pa = (((e.pa_deg ?? 90) % 180) + 180) % 180;
  const along = Math.abs(Math.sin((pa * Math.PI) / 180));
  const ew = Math.max(major * along + minor * (1 - along), minor);
  const ns = Math.max(major * (1 - along) + minor * along, minor);
  const fit = (need: number, frame: number) =>
    need <= frame ? 1 : Math.ceil((need - frame) / (frame * (1 - MOSAIC_OVERLAP))) + 1;
  let cols = Math.min(MOSAIC_MAX_SIDE, fit(ew, w));
  const rows = Math.min(MOSAIC_MAX_SIDE, fit(ns, h));
  if (cols * rows < 2) cols = 2;
  return { cols, rows };
}

/** Name of a mosaic in the scope's terms, as the server stores it. */
export function mosaicName(name: string): string {
  return name.replace(/[^A-Za-z0-9 +.-]+/g, ' ').split(/\s+/).filter(Boolean).join(' ');
}

/** True when the point falls inside a north-up box. */
export function inBox(b: SkyBox, ra: number, dec: number): boolean {
  if (Math.abs(b.dec - dec) > b.h) return false;
  const dra = ((((ra - b.ra + 540) % 360) + 360) % 360) - 180;
  const dx = dra * Math.cos((((b.dec + dec) / 2) * Math.PI) / 180);
  return Math.abs(dx) <= b.w / 2 && Math.abs(dec - b.dec) <= b.h / 2;
}

export function fmtHours(s: number): string {
  const h = s / 3600;
  return h >= 10 ? `${Math.round(h)} h` : h >= 0.1 ? `${h.toFixed(1)} h` : `${Math.round(s / 60)} min`;
}
