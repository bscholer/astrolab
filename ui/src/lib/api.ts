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
  reason?: string | null;
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

export interface SkyInfo {
  ra_deg: number | null;
  dec_deg: number | null;
  magnitude: number | null;
  constellation: string | null;
  object_type: string | null;
}

export interface TargetSummary {
  id: number;
  name: string;
  common_name: string | null;
  sky: SkyInfo | null;
  session_count: number;
  frame_count: number;
  failed_count: number;
  last_session_at: string | null;
}

export interface TargetDetail {
  id: number;
  name: string;
  common_name: string | null;
  sky: SkyInfo | null;
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

/**
 * Custom error subclass that carries the HTTP status and the parsed
 * `detail` field from a FastAPI error response. UI code can pull `.detail`
 * straight into a toast without scrubbing JSON.
 */
export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

// Stable per-tab cache-buster so within a session the browser can cache
// previews freely, but a fresh tab/reload gets fresh URLs (and dodges any
// stale `immutable` PNGs from earlier renderer versions).
const PREVIEW_CACHE_KEY =
  typeof window !== 'undefined' ? Date.now().toString(36) : 'ssr';

async function _readErrorDetail(r: Response): Promise<string> {
  // FastAPI errors come back as { detail: "..." }; non-JSON bodies fall
  // through to the raw text. Either way we never want to dump the raw
  // braces to the user.
  const text = await r.text();
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === 'object' && typeof parsed.detail === 'string') {
      return parsed.detail;
    }
  } catch {
    // not JSON; fall through
  }
  return text || `${r.status} ${r.statusText}`;
}

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) {
    throw new ApiError(r.status, await _readErrorDetail(r));
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
    throw new ApiError(r.status, await _readErrorDetail(r));
  }
  return (await r.json()) as T;
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    throw new ApiError(r.status, await _readErrorDetail(r));
  }
  return (await r.json()) as T;
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    throw new ApiError(r.status, await _readErrorDetail(r));
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

export interface JobCaptureSummary {
  target_name: string | null;
  frame_count: number;
  failed_count: number;
  session_count: number;
  exptime: number | null;
  gain: number | null;
  binning: number | null;
  camera: string | null;
  filter: string | null;
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
  // Only populated when the job was built from catalog session(s).
  capture?: JobCaptureSummary;
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

export type CalibrationMode = 'auto' | 'explicit' | 'none';
export interface CalibrationSpec {
  mode: CalibrationMode;
  master_ids?: Record<string, number>;
}

// PATCH semantics for the project endpoint: a value of null in the
// overrides map clears the corresponding key (per-node or per-param).
export type OverrideValue = unknown;

export interface SubmitFromSessionRequest {
  session_id: number;
  template_id: string;
  calibration?: CalibrationSpec;
}

// ----- projects ----------------------------------------------------------

export type CostClass = 'cheap' | 'medium' | 'expensive';

export interface ProjectHistoryEntry {
  seq: number;
  job_id: string;
  overrides: Record<string, Record<string, unknown>>;
  label: string | null;
  created_at: string;
}

export interface GalleryEntry {
  project_id: string;
  project_name: string;
  target_common_name: string | null;
  template_id: string;
  seq: number;
  label: string | null;
  created_at: string;
  preview_hash: string;
  preview_port: string;
  is_cover: boolean;
}

export interface ProjectCapture {
  session_count: number;
  frame_count: number;
  failed_count: number;
  exptime: number | null;
  gain: number | null;
  filter: string | null;
  started_at: string | null;
  ended_at: string | null;
  target_name: string | null;
  target_common_name: string | null;
}

export interface Project {
  id: string;
  name: string;
  template_id: string;
  template_version: number;
  template: Template;
  base_job: Record<string, unknown>;
  current_seq: number;
  draft_mode: boolean;
  source_session_ids: string[];
  history: ProjectHistoryEntry[];
  current_job_id: string;
  current_overrides: Record<string, Record<string, unknown>>;
  created_at: string;
  updated_at: string;
  // Preview pointer: present when the project's current job has finished
  // and its outputs are still in cache. The UI feeds these to
  // api.previewUrl() to render a row thumbnail.
  preview_hash?: string;
  preview_port?: string;
  // Aggregated capture summary (frames + exposure + filter + dates) for
  // the project's source sessions. Optional for older API responses.
  capture?: ProjectCapture;
  // Pinned cover seq. null/undefined = auto-pick the latest entry with
  // outputs (the v6 default behavior).
  cover_seq?: number | null;
}

export interface CreateProjectFromSessionRequest {
  session_id: number;
  template_id: string;
  name?: string;
  calibration?: CalibrationSpec;
}

export interface PatchProjectRequest {
  overrides?: Record<string, Record<string, unknown> | null> | null;
  draft_mode?: boolean;
  label?: string;
  force?: boolean;
}

/**
 * JSON Schema entry returned by GET /api/templates/{id}/schema. The UI uses
 * this to auto-build per-node param forms with cost-aware affordances.
 */
export interface TemplateNodeSchema {
  node_id: string;
  kind: string;
  variant: string | null;
  cost: CostClass;
  // Pydantic v2 JSON Schema; we don't model it deeply, just walk it.
  schema: {
    properties: Record<string, JSONSchemaField>;
    required?: string[];
    title?: string;
    type?: string;
    [k: string]: unknown;
  };
  defaults: Record<string, unknown>;
  template_params: Record<string, unknown>;
  inputs: Record<string, string>;
}

export interface JSONSchemaField {
  type?: string | string[];
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  enum?: unknown[];
  anyOf?: JSONSchemaField[];
  // The following keys come from Pydantic Field(json_schema_extra=...).
  // hash_precision is informational; ui_hidden / ui_section drive the UI's
  // form rendering (hidden = pipeline plumbing, advanced = collapsed).
  hash_precision?: number;
  ui_hidden?: boolean;
  ui_section?: 'basic' | 'advanced';
  // Conditional visibility: a map of {paramName: expectedValue}. The field
  // is rendered only when every dependency in the map matches the current
  // effective value of that param (overrides ?? defaults). A list value
  // means "any of these"; a scalar means exact match. Useful for showing
  // method-specific knobs (mtf_* only when method=mtf, etc.).
  ui_when?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface TemplateSchema {
  template_id: string;
  template_version: number;
  nodes: TemplateNodeSchema[];
  outputs: Record<string, string>;
}

// ----- storage -----------------------------------------------------------

export interface ProjectStorage {
  project_id: string;
  name: string;
  updated_at: string;
  owned_bytes: number;
  shared_bytes: number;
  entry_count: number;
}

export interface CacheDisk {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
}

export interface StorageSnapshot {
  total_bytes: number;
  entry_count: number;
  unreachable_bytes: number;
  unreachable_count: number;
  cache_root: string;
  cache_disk: CacheDisk;
  per_project: ProjectStorage[];
}

export interface CleanupResponse {
  evicted_count: number;
  bytes_freed: number;
  bytes_remaining: number;
  over_budget: boolean;
  max_bytes: number;
}

export interface SettingsResponse {
  cache_max_bytes: number;
  // Persisted override (null when the server is using the default location).
  cache_root: string | null;
  // What the running process is actually using right now. Differs from
  // cache_root when the user changed the override since the last restart.
  cache_root_active: string;
}

async function deleteJSON<T>(path: string): Promise<T> {
  const r = await fetch(path, { method: 'DELETE' });
  if (!r.ok) {
    throw new ApiError(r.status, await _readErrorDetail(r));
  }
  return (await r.json()) as T;
}

export const api = {
  listTargets: () => getJSON<TargetSummary[]>('/api/targets'),
  getTarget: (id: number) => getJSON<TargetDetail>(`/api/targets/${id}`),
  scan: (root: string, scope_id = 'dwarf3') =>
    postJSON<ScanResponse>('/api/scan', { root, scope_id }),
  listJobs: () => getJSON<JobSummary[]>('/api/jobs'),
  getJob: (id: string) => getJSON<JobSummary>(`/api/jobs/${id}`),
  getJobEvents: (id: string) => getJSON<JobEvent[]>(`/api/jobs/${id}/events`),
  listTemplates: () => getJSON<Template[]>('/api/templates'),
  getTemplateSchema: (id: string) =>
    getJSON<TemplateSchema>(`/api/templates/${id}/schema`),
  submitFromSession: (req: SubmitFromSessionRequest) =>
    postJSON<{ job_id: string }>('/api/jobs/from_session', req),
  rerunJob: (id: string) =>
    postJSON<{ job_id: string }>(`/api/jobs/${id}/rerun`, {}),
  listProjects: () => getJSON<Project[]>('/api/projects'),
  getProject: (id: string) => getJSON<Project>(`/api/projects/${id}`),
  createProjectFromSession: (req: CreateProjectFromSessionRequest) =>
    postJSON<Project>('/api/projects/from_session', req),
  patchProject: (id: string, req: PatchProjectRequest) =>
    patchJSON<Project>(`/api/projects/${id}`, req),
  revertProject: (id: string, seq: number) =>
    postJSON<Project>(`/api/projects/${id}/revert/${seq}`, {}),
  setProjectCover: (id: string, seq: number | null) =>
    putJSON<Project>(`/api/projects/${id}/cover`, { seq }),
  listGallery: () => getJSON<GalleryEntry[]>('/api/gallery'),
  deleteProject: (id: string) =>
    deleteJSON<{ evicted_count: number; bytes_freed: number }>(
      `/api/projects/${id}`
    ),
  purgeProjectCache: (id: string, keepOutputs = false) =>
    deleteJSON<{ evicted_count: number; bytes_freed: number }>(
      `/api/projects/${id}/cache?keep_outputs=${keepOutputs}`
    ),
  getStorage: () => getJSON<StorageSnapshot>('/api/storage'),
  storageCleanup: (max_bytes?: number) =>
    postJSON<CleanupResponse>('/api/storage/cleanup', max_bytes !== undefined ? { max_bytes } : {}),
  getSettings: () => getJSON<SettingsResponse>('/api/settings'),
  patchSettings: (req: { cache_max_bytes?: number; cache_root?: string }) =>
    patchJSON<SettingsResponse>('/api/settings', req),
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
  },
  previewUrl(nodeHash: string, port: string): string {
    // Cache-buster: previously we promised previews were `immutable` for 24h,
    // which means stale renders linger in browser caches even after the
    // server-side renderer changes. Each page load starts a new "session",
    // so we tag URLs with that session id; within the session the browser
    // can cache freely, across sessions it refetches.
    return `/api/preview/${nodeHash}/${encodeURIComponent(port)}?v=${PREVIEW_CACHE_KEY}`;
  }
};
