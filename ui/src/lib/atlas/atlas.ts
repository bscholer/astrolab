import type { TonightEntry } from '$lib/api';

/** Field of the DWARF 3 telephoto: 3856 x 2180 px of 2 µm behind 150 mm. */
export const TELE_FOV_DEG: [number, number] = [2.95, 1.67];

export interface Layers {
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
