/**
 * Display helpers shared across pages. See ui/design.md for the rules
 * these enforce (friendly names, no snake_case, integer percentages, etc.).
 */
import type { JobSummary } from './api';

/**
 * Pretty-print a template id. Templates are stored as snake_case ids
 * (`calibrate_register_stack`); the UI shows them with bullets and
 * Title Case, matching design.md "no snake_case in the UI".
 *
 * Add overrides here as new templates ship.
 */
const TEMPLATE_DISPLAY_NAMES: Record<string, string> = {
  calibrate_register_stack: 'Calibrate · Register · Stack',
  hoo_dwarf3_dualband: 'HOO Recombine',
  hoo_recombine: 'HOO Recombine',
  hso_recombine: 'HSO Recombine'
};

export function templateDisplayName(id: string): string {
  if (TEMPLATE_DISPLAY_NAMES[id]) return TEMPLATE_DISPLAY_NAMES[id];
  // Fallback: split on underscores, title-case, join with bullets so it
  // at least reads like a phrase rather than a slug.
  return id
    .split('_')
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(' · ');
}

/**
 * Failure percentage for a frame count. Whole percent only — design.md
 * forbids decimals here. A returned string of '' means "don't render
 * anything" (zero frames).
 */
export function formatFailPct(failed: number, total: number): string {
  if (!total) return '';
  const pct = Math.round((failed / total) * 100);
  return `${pct}% failed`;
}

/**
 * CSS class hint for the failure pill. Pairs with .fail-pct in page
 * styles: zero gets a quiet good-color treatment, normal gets warn,
 * a quarter or more gets the loud bad treatment.
 */
export function failPctClass(failed: number, total: number): string {
  if (!total) return '';
  const pct = (failed / total) * 100;
  if (pct === 0) return 'fail-zero';
  if (pct >= 25) return 'fail-high';
  return '';
}


export function shortAgo(iso: string): string {
  const t = new Date(iso).getTime();
  const sec = (Date.now() - t) / 1000;
  if (sec < 60) return 'just now';
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86_400) return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 7 * 86_400) return `${Math.floor(sec / 86_400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric'
  });
}

export function formatDuration(start: string | null, end: string | null): string {
  if (!start) return '';
  const t0 = new Date(start).getTime();
  const t1 = end ? new Date(end).getTime() : Date.now();
  const sec = (t1 - t0) / 1000;
  if (sec < 1) return '<1s';
  if (sec < 60) return `${sec.toFixed(1)}s`;
  return `${Math.floor(sec / 60)}m ${Math.floor(sec % 60)}s`;
}

export function formatExposure(j: Pick<JobSummary, 'capture'>): string {
  const c = j.capture;
  if (!c) return '';
  const parts = [`${c.frame_count.toLocaleString()} × ${c.exptime ?? '?'}s`];
  if (c.gain !== null) parts.push(`gain ${c.gain}`);
  if (c.filter) parts.push(c.filter);
  return parts.join(' · ');
}

/**
 * Render bytes as a human-readable string. Uses binary units (KiB, MiB,
 * etc.) since these are storage numbers; we trade off the GB-vs-GiB
 * confusion for consistency with what `du -h` and `df -h` show on the
 * Linux box.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  // <10 of a unit: one decimal; otherwise round to integer for compact display.
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}
