/**
 * Tiny typed wrapper around fetch. Every call goes through `/api/...`,
 * which the Vite dev server proxies to the FastAPI control plane on :8000;
 * in production the static UI gets mounted under the same origin so no
 * proxy is needed.
 */

export type CalibrationKind = 'dark' | 'flat' | 'bias';
export type MatchQuality = 'exact' | 'approx' | 'none';

export interface CalibrationStatus {
  kind: CalibrationKind;
  quality: MatchQuality;
  master_id: number | null;
}

export interface SessionSummary {
  id: number;
  session_key: string;
  target_name: string | null;
  instrument: string | null;
  camera: string | null;
  filter: string | null;
  exptime: number | null;
  gain: number | null;
  binning: number | null;
  started_at: string | null;
  ended_at: string | null;
  frame_count: number;
  failed_count: number;
  calibration: CalibrationStatus[];
}

export interface TargetSummary {
  id: number;
  name: string;
  session_count: number;
  frame_count: number;
  failed_count: number;
  last_session_at: string | null;
}

export interface TargetDetail {
  id: number;
  name: string;
  sessions: SessionSummary[];
}

export interface ScanResponse {
  discovered: number;
  inserted: number;
  updated: number;
  removed: number;
  skipped_unchanged: number;
  failed: number;
  masters_inserted: number;
  masters_updated: number;
  masters_removed: number;
  masters_skipped: number;
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) {
    throw new Error(`${r.status} ${r.statusText}: ${path}`);
  }
  return (await r.json()) as T;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    const detail = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${detail}`);
  }
  return (await r.json()) as T;
}

export const api = {
  listTargets: () => getJSON<TargetSummary[]>('/api/targets'),
  getTarget: (id: number) => getJSON<TargetDetail>(`/api/targets/${id}`),
  scan: (root: string, scope_id = 'dwarf3') =>
    postJSON<ScanResponse>('/api/scan', { root, scope_id })
};
