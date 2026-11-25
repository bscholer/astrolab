import type { TonightEntry } from './api';

export type TypeGroup = 'nebulae' | 'galaxies' | 'clusters';

const GROUPS: Record<string, TypeGroup> = {
  Galaxy: 'galaxies',
  'Galaxy Pair': 'galaxies',
  'Galaxy Triplet': 'galaxies',
  'Galaxy Group': 'galaxies',
  'Emission Nebula': 'nebulae',
  'Reflection Nebula': 'nebulae',
  'Dark Nebula': 'nebulae',
  'Planetary Nebula': 'nebulae',
  'Supernova Remnant': 'nebulae',
  'H II Region': 'nebulae',
  Nebula: 'nebulae',
  // Most of these are imaged for the nebula: M 42, the Heart, the Rosette.
  'Cluster with Nebula': 'nebulae',
  'Open Cluster': 'clusters',
  'Globular Cluster': 'clusters',
  'Stellar Association': 'clusters',
  Asterism: 'clusters',
};

export function typeGroup(objectType: string | null): TypeGroup | null {
  return objectType ? (GROUPS[objectType] ?? null) : null;
}

export function displayName(e: TonightEntry): string {
  return e.messier ?? e.name;
}

export function matchesSearch(e: TonightEntry, query: string): boolean {
  const q = query.trim().toLowerCase().replace(/\s+/g, ' ');
  if (!q) return true;
  const compact = q.replace(/ /g, '');
  return [e.name, e.messier, e.common_name].some((field) => {
    const f = field?.toLowerCase();
    return !!f && (f.includes(q) || f.replace(/ /g, '').includes(compact));
  });
}

/**
 * First and last curve samples at or above `minAlt`, as UTC instants. With
 * `skyline` (the altitude the target must clear at each sample, from the
 * scope's panorama), samples behind houses and trees do not count.
 */
export function upWindow(
  curve: number[] | null,
  minAlt: number,
  duskUtc: string | null,
  stepMin: number,
  skyline: number[] | null = null,
): { start: Date; end: Date } | null {
  if (!curve || !duskUtc) return null;
  const clear = (alt: number, i: number) => alt >= minAlt && (!skyline || alt >= (skyline[i] ?? -90));
  const first = curve.findIndex(clear);
  if (first === -1) return null;
  const last = curve.findLastIndex(clear);
  const dusk = new Date(duskUtc).getTime();
  return {
    start: new Date(dusk + first * stepMin * 60_000),
    end: new Date(dusk + last * stepMin * 60_000),
  };
}

/**
 * SVG polygon points for what blocks the sky under a target's path: the
 * skyline curve filled down to the bottom of a sparkline `w` x `h`, with
 * `y` mapping altitude to the box. Empty without a skyline.
 */
export function skylineArea(skyline: number[] | null, w: number, h: number, y: (alt: number) => number): string {
  if (!skyline || skyline.length < 2) return '';
  const dx = w / (skyline.length - 1);
  const pts = skyline.map((alt, i) => `${(i * dx).toFixed(1)},${(alt <= -90 ? h : y(alt)).toFixed(1)}`);
  return `0,${h} ${pts.join(' ')} ${w},${h}`;
}
