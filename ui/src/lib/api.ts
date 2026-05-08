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
  common_name: string | null;
  session_count: number;
  frame_count: number;
  failed_count: number;
  last_session_at: string | null;
}

export interface TargetDetail {
  id: number;
  name: string;
  common_name: string | null;
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

// ----- jobs --------------------------------------------------------------

export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';

export interface JobOutputRef {
  path: string;
  type: string;
  node_hash: string;
}

export interface NodeSpec {
  id: string;
  kind: string;
  variant: string | null;
  params: Record<string, unknown>;
  inputs: Record<string, string>;
}

export interface Template {
  id: string;
  version: number;
  description: string;
  profile: string | null;
  nodes: NodeSpec[];
  outputs: Record<string, string>;
}

export interface JobSummary {
  id: string;
  status: JobStatus;
  template_id: string;
  template_version: number;
  submitted_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  outputs: Record<string, JobOutputRef> | null;
  // Only populated by GET /api/jobs/{id}, not the list endpoint.
  template?: Template;
}

export type JobEventType =
  | 'job_queued'
  | 'job_started'
  | 'node_started'
  | 'node_progress'
  | 'node_cached'
  | 'node_completed'
  | 'node_failed'
  | 'job_completed'
  | 'job_failed';

export interface JobEvent {
  type: JobEventType;
  timestamp: string;
  node_id?: string;
  fraction?: number;
  message?: string;
  error?: string;
  hash?: string;
  kind?: string;
}

export const api = {
  listTargets: () => getJSON<TargetSummary[]>('/api/targets'),
  getTarget: (id: number) => getJSON<TargetDetail>(`/api/targets/${id}`),
  scan: (root: string, scope_id = 'dwarf3') =>
    postJSON<ScanResponse>('/api/scan', { root, scope_id }),
  listJobs: () => getJSON<JobSummary[]>('/api/jobs'),
  getJob: (id: string) => getJSON<JobSummary>(`/api/jobs/${id}`),
  getJobEvents: (id: string) => getJSON<JobEvent[]>(`/api/jobs/${id}/events`),
  /**
   * Open a WebSocket for live event streaming. The server replays buffered
   * history and then closes when the job hits a terminal state.
   */
  subscribeJobEvents(id: string, onEvent: (ev: JobEvent) => void): WebSocket {
    const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${wsProto}//${window.location.host}/api/jobs/${id}/events`);
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch (err) {
        console.error('bad event payload', err);
      }
    };
    return ws;
  }
};
