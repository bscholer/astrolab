/**
 * Display helpers shared across the jobs list + detail views.
 */
import type { JobSummary } from './api';

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
