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

/** First and last curve samples at or above `minAlt`, as UTC instants. */
export function upWindow(
  curve: number[] | null,
  minAlt: number,
  duskUtc: string | null,
  stepMin: number,
): { start: Date; end: Date } | null {
  if (!curve || !duskUtc) return null;
  const first = curve.findIndex((alt) => alt >= minAlt);
  if (first === -1) return null;
  const last = curve.findLastIndex((alt) => alt >= minAlt);
  const dusk = new Date(duskUtc).getTime();
  return {
    start: new Date(dusk + first * stepMin * 60_000),
    end: new Date(dusk + last * stepMin * 60_000),
  };
}
