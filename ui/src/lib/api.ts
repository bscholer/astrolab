/**
 * Tiny typed wrapper around fetch. Every call goes through `/api/...`,
 * which the Vite dev server proxies to the FastAPI control plane on :8000;
 * in production the static UI gets mounted under the same origin so no
 * proxy is needed.
 */

export type CalibrationKind = 'dark' | 'flat' | 'bias';
export type MatchQuality = 'exact' | 'approx' | 'none' | 'not_needed';
/**
 * 'not_needed' means the scope subtracts darks and flats on-device (Seestar)
 * and the matcher deliberately did not look. Distinct from 'none' (matcher
 * looked and found nothing), and the UI should render it as a benign state
 * rather than a missing-calibration warning.
 */

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
  // Useful integration time (frame_count - failed_count) * exptime.
  // Null when exptime is unknown.
  integration_seconds: number | null;
  // Total bytes on disk for frames belonging to this session.
  bytes_on_disk: number;
  calibration: CalibrationStatus[];
  // User-attached free-text notes. Null when no note has been saved;
  // the UI suppresses the label entirely in that case.
  description: string | null;
}


export interface SkyInfo {
  ra_deg: number | null;
  dec_deg: number | null;
  magnitude: number | null;
  constellation: string | null;
  object_type: string | null;
}

/**
 * Catalog resolution annotation for a target. Set when the scanner's
 * position-fallback claimed a catalog row.
 *
 * Deliberately null on responses when the target resolved via its name
 * (source='name' internally). The library reads quieter that way: the
 * stored name already conveys the catalog mapping.
 */
export interface ResolvedAs {
  canonical: string;
  common_name: string | null;
  object_type: string | null;
  separation_arcmin: number | null;
  source: 'position';
}

export interface TargetSummary {
  id: number;
  name: string;
  common_name: string | null;
  sky: SkyInfo | null;
  resolved_as: ResolvedAs | null;
  session_count: number;
  frame_count: number;
  failed_count: number;
  last_session_at: string | null;
  // Total useful integration across all the target's sessions.
  integration_seconds: number | null;
  // Total bytes on disk for all of the target's frames.
  bytes_on_disk: number;
}

export interface TargetDetail {
  id: number;
  name: string;
  common_name: string | null;
  sky: SkyInfo | null;
  resolved_as: ResolvedAs | null;
  sessions: SessionSummary[];
}

// ----- session patch (reassign + metadata) -------------------------------

export interface SessionPatchRequest {
  // Reassign: exactly one of target_id / new_target_name (or neither, when
  // patching description only). Empty string for description clears.
  target_id?: number;
  new_target_name?: string;
  description?: string | null;
}

export interface SessionPatchResponse {
  session: SessionSummary;
  // Target ids deleted as a side effect (source target emptied on reassign).
  // Empty when the patch was description-only.
  deleted_target_ids: number[];
}

export interface ReassignCandidateTarget {
  id: number;
  name: string;
  common_name: string | null;
  separation_arcmin: number;
}

export interface ReassignCandidateCatalog {
  canonical: string;
  common_name: string | null;
  object_type: string | null;
  separation_arcmin: number;
}

export interface ReassignCandidatesResponse {
  targets: ReassignCandidateTarget[];
  catalog: ReassignCandidateCatalog[];
}

export interface ScanStartResponse {
  status: 'started' | 'already_running';
}

export interface ScanLastStats {
  discovered: number;
  inserted: number;
  updated: number;
  skipped_unchanged: number;
  /** FITS files we couldn't classify (unknown scope, unsupported IMAGETYP). */
  skipped_unknown: number;
  removed: number;
  failed: number;
  masters_inserted: number;
  masters_updated: number;
  masters_removed: number;
  masters_skipped: number;
  /**
   * Frames ingested per detected scope_id. Only `dwarf3` is validated
   * end-to-end through processing today; the UI surfaces a banner when
   * any other scope's count is nonzero.
   */
  scope_breakdown: Record<string, number>;
}

export interface ScanStatusResponse {
  running: boolean;
  started_at: number | null;
  finished_at: number | null;
  current_path: string | null;
  discovered: number;
  inserted: number;
  updated: number;
  skipped: number;
  removed: number;
  failed: number;
  error: string | null;
  last_stats: ScanLastStats | null;
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
  // Map of output port name -> PortType string ('image/fits', 'sequence/fits',
  // 'image/png', etc.). Server-derived from the Node class; absent when the
  // template payload was built before this field existed (defensive: the UI
  // falls back to 'image' if missing).
  outputs?: Record<string, string>;
  // Optional: hide this node in the pipeline view when the named
  // upstream node has enabled=false. Walks transitively through chained
  // declarations.
  ui_depends_on?: string | null;
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

export interface ProjectHistoryEntry {
  seq: number;
  job_id: string;
  overrides: Record<string, Record<string, unknown>>;
  label: string | null;
  created_at: string;
  // Whether this entry is opted in to the public gallery feed. Defaults
  // to false; the user toggles it via setHistoryPublished().
  published: boolean;
  // Discriminator: 'edit' for ordinary override changes, 'swap_sessions'
  // for entries created by the PATCH /sessions endpoint. Older entries
  // predating the column come back as 'edit' by default.
  kind?: string;
  // Free-form blob attached to non-edit kinds. For swap_sessions: carries
  // the new session_ids list, frame count, and integration time at the
  // time of the swap so a future revert / audit can navigate back through
  // it without re-querying the catalog.
  snapshot?: Record<string, unknown> | null;
}

export interface SuggestedAdditions {
  session_ids: number[];
  session_count: number;
  frame_count: number;
  integration_seconds: number;
  // Stable hash of session_ids. The UI keys a 'dismissed' localStorage
  // flag on this token so capturing a new session re-shows the banner
  // even after the user previously dismissed it.
  suggestions_token: string;
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
  // Total useful integration across the project's source sessions.
  integration_seconds: number | null;
  // Total bytes on disk for all source-session frames.
  bytes_on_disk: number;
}

/**
 * Catalog-resolved display info for the project header.
 *
 * Present when every source session points at the same canonical target
 * AND OpenNGC has a friendly common_name for it. The UI promotes `name`
 * (e.g. "Triangulum Galaxy") to the page header and surfaces `canonical`
 * (e.g. "NGC 598") as a muted sub-label.
 *
 * Null for multi-target projects, unresolved targets, or when OpenNGC
 * has no common name; the UI falls back to `project.name` in that case.
 */
export interface ProjectDisplay {
  name: string;
  canonical: string;
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
  // User-attached free-text notes. Null when no note has been saved;
  // the UI suppresses the label entirely in that case.
  description: string | null;
  // Catalog-resolved display info for the page header. Null for
  // multi-target / unresolved projects; UI falls back to `name`.
  display: ProjectDisplay | null;
  // Inline suggestion payload: orphan sessions on the same canonical
  // target. Null when there are no candidates or when the project is
  // multi-target. The UI renders the suggestion banner off this.
  suggested_additions: SuggestedAdditions | null;
  // Latest version of this project's template available on disk. Equal
  // to template_version when the project is on the latest; greater when
  // a template bump is available to upgrade into.
  latest_template_version?: number;
}

export type UpgradeTemplateResponse = Project & {
  new_job_id: string;
  dropped_overrides: string[];
};

export interface CreateProjectFromSessionRequest {
  session_id: number;
  template_id: string;
  name?: string;
  calibration?: CalibrationSpec;
}

export interface CreateProjectFromSessionsRequest {
  session_ids: number[];
  template_id: string;
  name?: string;
  calibration?: CalibrationSpec;
}

export interface PatchProjectRequest {
  overrides?: Record<string, Record<string, unknown> | null> | null;
  draft_mode?: boolean;
  label?: string;
  force?: boolean;
  // Free-text notes. Empty string clears; omit to leave unchanged.
  // Updating description does NOT kick the pipeline.
  description?: string | null;
}

/**
 * JSON Schema entry returned by GET /api/templates/{id}/schema. The UI uses
 * this to auto-build per-node param forms with cost-aware affordances.
 */
export interface TemplateNodeSchema {
  node_id: string;
  kind: string;
  variant: string | null;
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
  // Same as NodeSpec.outputs: port name -> PortType string. Sourced from the
  // registered Node class so the UI can pick a preview port without guessing.
  outputs?: Record<string, string>;
  // Optional id of an upstream node whose `enabled` param must be true
  // for this node to render in the pipeline view. Walks transitively.
  ui_depends_on?: string | null;
  // True when this node's FITS output is already display-ready (post-stretch);
  // the preview does not apply autostretch, so it faithfully represents the output.
  preview_display_ready?: boolean;
  // True when this node's preview is intentionally suppressed in the UI: the
  // card renders progress + params without a thumbnail well. Used for the
  // pre-stack sequence ops (convert, calibrate, resample, offset, bg_extract)
  // whose per-frame previews are visually uninformative.
  preview_hidden?: boolean;
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
  // Inline warning shown beneath a boolean field when its effective value
  // is true. Used for opt-in toggles whose enablement has a meaningful
  // cost (storage, RAM, runtime), e.g. drizzle.
  ui_warning_when_true?: string;
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
  // Where the user's raw captures live; null when not yet configured. Used
  // by /api/scan as its scan target. Lives server-side (not localStorage)
  // so it survives across browsers and devices.
  capture_root: string | null;
  // Observer site for the Tonight planner; all three null until the user
  // sets them. Tonight refuses to compute alt/az without all three.
  site_latitude: number | null;
  site_longitude: number | null;
  site_elevation_m: number | null;
}

// ----- system -----------------------------------------------------------

export interface SystemHost {
  hostname: string;
  os: string;
  kernel: string;
  cpu_model: string;
  cores: number;
  ram_total: number;
  disk_total: number;
  gpu_model: string | null;
  uptime_s: number;
}

export interface SystemCpu {
  percent: number;
  per_core: number[];
  load_avg: [number, number, number];
  temp_c: number | null;
}

export interface SystemMem {
  used: number;
  total: number;
  swap_used: number;
}

export interface SystemDisk {
  mount: string;
  used: number;
  total: number;
  read_bps: number;
  write_bps: number;
  temp_c: number | null;
}

export interface SystemGpu {
  model: string;
  util: number;
  vram_used: number;
  vram_total: number;
  temp_c: number;
}

export interface SystemActiveJob {
  id: string;
  target_name: string | null;
  template_name: string | null;
  // Project that owns this job (the active history entry on a project).
  // Null for ad-hoc submissions that aren't backed by a Project row.
  project_id: string | null;
  project_name: string | null;
  // 1-indexed version, matches the v{N} chips elsewhere in the UI.
  project_version: number | null;
  started_at: string | null;
  // Step-aware [0, 1]: (completed_nodes + current_node_fraction) / total.
  progress: number;
}

export interface SystemJobs {
  queued: number;
  running: number;
  completed_24h: number;
  failed_24h: number;
  active: SystemActiveJob[];
  // Twelve 30-min buckets, oldest first.
  throughput_6h: number[];
}

export interface SystemSnapshot {
  host: SystemHost;
  cpu: SystemCpu;
  mem: SystemMem;
  disk: SystemDisk;
  gpu: SystemGpu | null;
  jobs: SystemJobs;
  sampled_at: number;
}

// ----- tonight ----------------------------------------------------------

export interface TonightEntry {
  name: string;
  common_name: string | null;
  object_type: string | null;
  constellation: string | null;
  ra_deg: number;
  dec_deg: number;
  magnitude: number | null;
  alt_now_deg: number;
  az_now_deg: number;
  // ISO 8601 UTC; null when the target doesn't transit during the night.
  transit_utc: string | null;
  hours_above_min_alt: number;
  // Number of capture sessions the user has on this target; 0 = never
  // captured. Joined by canonical name against the user's targets table.
  session_count: number;
  last_session_at: string | null;
  // Altitude samples in degrees, evenly spaced from dusk_utc to dawn_utc
  // at alt_curve_step_min cadence. Null when the night window is
  // degenerate (polar day with no twilight).
  alt_curve_deg: number[] | null;
}

export interface TonightResponse {
  at_utc: string;
  site_latitude: number;
  site_longitude: number;
  site_elevation_m: number;
  min_alt_deg: number;
  max_magnitude: number;
  // Twilight window endpoints, both null at sites in 24h daylight.
  dusk_utc: string | null;
  dawn_utc: string | null;
  // Spacing in minutes between consecutive samples in each entry's
  // alt_curve_deg. Constant within a response so the UI reconstructs the
  // time axis as dusk_utc + i * step.
  alt_curve_step_min: number;
  entries: TonightEntry[];
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
  getSession: (id: number) => getJSON<SessionSummary>(`/api/sessions/${id}`),
  patchSession: (id: number, req: SessionPatchRequest) =>
    patchJSON<SessionPatchResponse>(`/api/sessions/${id}`, req),
  getSessionReassignCandidates: (id: number) =>
    getJSON<ReassignCandidatesResponse>(
      `/api/sessions/${id}/reassign_candidates`
    ),
  scan: (root: string) =>
    postJSON<ScanStartResponse>('/api/scan', { root }),
  scanStatus: () => getJSON<ScanStatusResponse>('/api/scan/status'),
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
  createProjectFromSessions: (req: CreateProjectFromSessionsRequest) =>
    postJSON<Project>('/api/projects/from_sessions', req),
  patchProject: (id: string, req: PatchProjectRequest) =>
    patchJSON<Project>(`/api/projects/${id}`, req),
  revertProject: (id: string, seq: number) =>
    postJSON<Project>(`/api/projects/${id}/revert/${seq}`, {}),
  upgradeProjectTemplate: (id: string) =>
    postJSON<UpgradeTemplateResponse>(
      `/api/projects/${id}/upgrade_template`,
      {}
    ),
  setProjectCover: (id: string, seq: number | null) =>
    putJSON<Project>(`/api/projects/${id}/cover`, { seq }),
  setHistoryPublished: (id: string, seq: number, published: boolean) =>
    putJSON<Project>(`/api/projects/${id}/history/${seq}/published`, {
      published,
    }),
  listGallery: () => getJSON<GalleryEntry[]>('/api/gallery'),
  deleteProject: (id: string) =>
    deleteJSON<{ evicted_count: number; bytes_freed: number }>(
      `/api/projects/${id}`
    ),
  purgeProjectCache: (id: string, keepOutputs = false) =>
    deleteJSON<{ evicted_count: number; bytes_freed: number }>(
      `/api/projects/${id}/cache?keep_outputs=${keepOutputs}`
    ),
  /** Preview the eviction the matching DELETE /cache would perform.
   * Used by the Manage Sessions modal's live diff. */
  projectCacheDryRun: (id: string, keepOutputs = false) =>
    getJSON<{ evicted_count: number; bytes_to_free: number }>(
      `/api/projects/${id}/cache?keep_outputs=${keepOutputs}`
    ),
  getProjectSuggestions: (id: string) =>
    getJSON<SuggestedAdditions>(`/api/projects/${id}/suggestions`),
  /** Replace the project's source-session set. Returns the updated
   * project plus what the cache nuke freed. */
  patchProjectSessions: (
    id: string,
    req: { session_ids: number[]; auto_render?: boolean }
  ) =>
    patchJSON<
      Project & {
        evicted_count: number;
        evicted_bytes: number;
        new_job_id: string | null;
      }
    >(`/api/projects/${id}/sessions`, req),
  getStorage: () => getJSON<StorageSnapshot>('/api/storage'),
  storageCleanup: (max_bytes?: number) =>
    postJSON<CleanupResponse>('/api/storage/cleanup', max_bytes !== undefined ? { max_bytes } : {}),
  getSettings: () => getJSON<SettingsResponse>('/api/settings'),
  patchSettings: (req: {
    cache_max_bytes?: number;
    cache_root?: string;
    capture_root?: string;
    site_latitude?: number | null;
    site_longitude?: number | null;
    site_elevation_m?: number | null;
  }) => patchJSON<SettingsResponse>('/api/settings', req),
  getSystem: () => getJSON<SystemSnapshot>('/api/system'),
  getTonight: (params?: {
    at?: string;
    min_alt?: number;
    max_mag?: number;
  }) => {
    const qs = new URLSearchParams();
    if (params?.at) qs.set('at', params.at);
    if (params?.min_alt !== undefined) qs.set('min_alt', String(params.min_alt));
    if (params?.max_mag !== undefined) qs.set('max_mag', String(params.max_mag));
    const tail = qs.toString();
    return getJSON<TonightResponse>(`/api/tonight${tail ? `?${tail}` : ''}`);
  },
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
