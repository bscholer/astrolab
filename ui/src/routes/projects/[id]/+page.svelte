<!--
  Project detail page: a live, editable view of one stack pipeline.

  Layout (top to bottom):
    1. Header: target name, version pointer, undo/redo, reprocess.
    2. Capture line.
    3. PIPELINE strip: one card per node showing the live preview image,
       with the step name + status overlaid on the image. This is the
       "flow chart" — preview replaces the abstract SVG box, order alone
       (left-to-right) communicates the pipeline.
    4. PARAMETERS grid: one card per node with the auto-built form. No
       previews here — those live in the pipeline strip above. Cards are
       always-open so users see the knobs without clicking.
    5. Final output (big preview).
    6. History strip with revert + Cmd-Z/Cmd-Shift-Z.

  Edits debounce 350ms then PATCH the project. The active job is the
  current_seq's job_id; we tear down/rebind the WS subscription whenever
  the active job changes.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { fade, slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { page } from '$app/stores';
  import {
    api,
    type CalibrationStatus,
    type CostClass,
    type JobEvent,
    type JobSummary,
    type Project,
    type SessionSummary,
    type TemplateSchema
  } from '$lib/api';
  import {
    blastRadiusCost,
    isNodeVisible,
    isTogglable,
    nodeDisplayName
  } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatBytes, formatDuration, formatIntegrationTime, shortAgo } from '$lib/format';
  import NodeParamsForm from '$lib/NodeParamsForm.svelte';
  import CropEditor from '$lib/CropEditor.svelte';
  import CompareSlider from '$lib/CompareSlider.svelte';
  import SessionRow from '$lib/SessionRow.svelte';

  let project = $state<Project | null>(null);
  let schema = $state<TemplateSchema | null>(null);
  let activeJob = $state<JobSummary | null>(null);
  // Reference to the history strip element so we can keep it scrolled
  // to the right edge (newest entry) by default. Without this the user
  // lands on v1 every time and has to drag right to find the version
  // they're actually looking at.
  let historyEl = $state<HTMLOListElement | null>(null);

  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let nodeHash = $state<Record<string, string>>({});
  let nodeKind = $state<Record<string, string>>({});
  let nodePort = $state<Record<string, string>>({});
  // Per-node timing. Captured from event timestamps so we can show
  // "completed (1.3s)" on the status badge — useful for spotting which
  // step is the actual bottleneck without diving into the events list.
  let nodeStartedAt = $state<Record<string, number>>({});
  let nodeDurationMs = $state<Record<string, number>>({});

  // Per-node preview-image load state. The browser may take a noticeable
  // beat to fetch / decode the PNG (especially for the first hit, which
  // forces server-side render_preview). The flow card shows a skeleton
  // shimmer until onload fires so the gap reads as "generating preview"
  // instead of "broken".
  let previewLoaded = $state<Record<string, boolean>>({});

  // Accordion expansion: which node rows are open. We default to just
  // the project's output node so the user lands on the final preview;
  // they can pop other rows open as they tweak. Multi-expansion (a
  // Set, not a single id) so opening Stretch doesn't collapse the
  // output preview the user is staring at.
  let expandedNodes = $state<Set<string>>(new Set());

  function toggleNode(nid: string) {
    const next = new Set(expandedNodes);
    if (next.has(nid)) next.delete(nid);
    else next.add(nid);
    expandedNodes = next;
  }
  function onPreviewLoad(nid: string) {
    previewLoaded = { ...previewLoaded, [nid]: true };
  }
  function onPreviewError(nid: string) {
    // Keep skeleton visible; clear the flag so a subsequent successful
    // fetch flips it back to true and fades the image in.
    previewLoaded = { ...previewLoaded, [nid]: false };
  }

  let ws: WebSocket | null = null;
  const seenEventKey = new Set<string>();
  let subscribedJobId: string | null = null;

  const PATCH_DEBOUNCE_MS = 350;
  let patchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingOverrides: Record<string, Record<string, unknown> | null> | null = null;
  let patching = $state(false);
  let reprocessing = $state(false);
  let coverBusy = $state(false);
  // Per-seq busy guard so spam-clicks on the publish star don't fire
  // overlapping requests against the same row.
  let publishBusy = $state<Record<number, boolean>>({});

  /** Flip a single history entry's gallery-published flag. Orthogonal
   * to cover_seq (cover = this project's thumbnail; published = surface
   * in the global gallery feed). */
  async function togglePublished(seq: number, currentlyPublished: boolean) {
    if (!project || publishBusy[seq]) return;
    publishBusy = { ...publishBusy, [seq]: true };
    try {
      const next = await api.setHistoryPublished(project.id, seq, !currentlyPublished);
      onProjectUpdated(next);
      toast.success(
        !currentlyPublished
          ? `Published v${seq + 1} to the gallery`
          : `Unpublished v${seq + 1}`
      );
    } catch (e) {
      toast.error(`Couldn't update gallery: ${(e as Error).message}`);
    } finally {
      const { [seq]: _, ...rest } = publishBusy;
      publishBusy = rest;
    }
  }

  // ---- Compare slider (issue #4) ------------------------------------
  // Two-slot armed picks. compareA is set on the first compare-button
  // click; the second click on a different seq fills compareB and
  // opens the modal. Picks persist across modal close so reopening
  // doesn't lose the user's selection; navigating away does (the page
  // component unmounts, taking state with it).
  let compareA = $state<number | null>(null);
  let compareB = $state<number | null>(null);
  let compareOpen = $state(false);
  // Lazy preview lookup: history entries other than the current seq
  // don't have their job's outputs in nodeHash[], so we fetch them
  // on-demand when the user picks for compare. Memoize by job_id so
  // re-arming the same seq is free.
  let comparePreviews = $state<Record<string, { hash: string; port: string } | null>>({});
  let compareLoading = $state(false);

  async function ensurePreviewForSeq(seq: number): Promise<{ hash: string; port: string } | null> {
    if (!project) return null;
    const entry = project.history.find((h) => h.seq === seq);
    if (!entry) return null;
    const cached = comparePreviews[entry.job_id];
    if (cached !== undefined) return cached;
    try {
      const job = await api.getJob(entry.job_id);
      if (!job.outputs) {
        comparePreviews = { ...comparePreviews, [entry.job_id]: null };
        return null;
      }
      // Match the gallery's resolution: prefer 'image', fall back to
      // first key. Keeps the wipe consistent with what gallery cards
      // surface, so 'compare from history' shows the same artifact.
      const port = 'image' in job.outputs ? 'image' : Object.keys(job.outputs)[0];
      const ref = job.outputs[port];
      const out = ref ? { hash: ref.node_hash, port } : null;
      comparePreviews = { ...comparePreviews, [entry.job_id]: out };
      return out;
    } catch {
      comparePreviews = { ...comparePreviews, [entry.job_id]: null };
      return null;
    }
  }

  /** Click handler for the per-row compare button.
   *
   * State machine:
   *   - neither armed → arm A
   *   - A armed, click same seq → unarm A
   *   - A armed, click different seq → arm B and open the modal
   *   - both armed, click A's seq → unarm A (modal stays open if B remains)
   *   - both armed, click B's seq → unarm B (modal closes)
   *   - both armed, click a third seq → replace B with the new pick
   */
  async function toggleCompareSlot(seq: number) {
    if (compareLoading) return;
    if (compareA === null) {
      compareLoading = true;
      try {
        await ensurePreviewForSeq(seq);
        compareA = seq;
      } finally {
        compareLoading = false;
      }
      return;
    }
    if (compareA === seq) {
      compareA = null;
      compareOpen = false;
      return;
    }
    if (compareB === seq) {
      compareB = null;
      compareOpen = false;
      return;
    }
    // We have an A; this click sets/replaces B and opens the modal.
    compareLoading = true;
    try {
      await ensurePreviewForSeq(seq);
      compareB = seq;
      compareOpen = true;
    } finally {
      compareLoading = false;
    }
  }

  function closeCompare() {
    compareOpen = false;
  }

  function clearCompare() {
    compareA = null;
    compareB = null;
    compareOpen = false;
  }

  function compareSrcFor(seq: number | null): string | null {
    if (seq === null || !project) return null;
    const entry = project.history.find((h) => h.seq === seq);
    if (!entry) return null;
    const cached = comparePreviews[entry.job_id];
    if (!cached) return null;
    return api.previewUrl(cached.hash, cached.port);
  }

  function onModalKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeCompare();
    }
  }

  /** Toggle the cover pin: if the current seq is already the cover,
   * clear it (server will fall back to the auto-pick). Otherwise pin
   * the current seq. The single round-trip returns the updated
   * project so onProjectUpdated keeps everything in sync. */
  async function toggleCover() {
    if (!project || coverBusy) return;
    const seq = project.cover_seq === project.current_seq ? null : project.current_seq;
    coverBusy = true;
    try {
      const next = await api.setProjectCover(project.id, seq);
      onProjectUpdated(next);
      toast.success(seq === null ? 'Cleared cover' : `Cover set to v${seq + 1}`);
    } catch (e) {
      toast.error(`Couldn't set cover: ${(e as Error).message}`);
    } finally {
      coverBusy = false;
    }
  }

  const id = $derived($page.params.id ?? '');

  const costByNode = $derived.by(() => {
    if (!schema) return {} as Record<string, CostClass>;
    const out: Record<string, CostClass> = {};
    for (const n of schema.nodes) out[n.node_id] = n.cost;
    return out;
  });

  // Pipeline strip operates in topological / template order, which matches
  // how nodes are listed in the schema (we serialize them in order on the
  // server). Schema-driven so we don't need a layout pass.
  const orderedNodeIds = $derived(schema?.nodes.map((n) => n.node_id) ?? []);

  // Index by node_id for the dependency-visibility walk. isNodeVisible
  // reads {ui_depends_on, defaults, template_params}; the live overrides
  // come from project.current_overrides at call time.
  const schemaByNodeId = $derived.by(() => {
    const out: Record<string, TemplateSchema['nodes'][number]> = {};
    if (!schema) return out;
    for (const n of schema.nodes) out[n.node_id] = n;
    return out;
  });

  function eventKey(ev: JobEvent): string {
    return `${ev.timestamp}|${ev.type}|${ev.node_id ?? ''}|${ev.fraction ?? ''}|${ev.message ?? ''}`;
  }

  function applyEvent(ev: JobEvent) {
    const key = eventKey(ev);
    if (seenEventKey.has(key)) return;
    seenEventKey.add(key);
    if (ev.kind && ev.node_id) nodeKind = { ...nodeKind, [ev.node_id]: ev.kind };
    if (ev.hash && ev.node_id) nodeHash = { ...nodeHash, [ev.node_id]: ev.hash };
    if (ev.node_id) {
      const ts = Date.parse(ev.timestamp);
      switch (ev.type) {
        case 'node_started':
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'running' };
          if (!Number.isNaN(ts)) {
            nodeStartedAt = { ...nodeStartedAt, [ev.node_id]: ts };
          }
          break;
        case 'node_progress':
          if (ev.fraction !== undefined && ev.message !== undefined) {
            nodeProgress = {
              ...nodeProgress,
              [ev.node_id]: { fraction: ev.fraction, message: ev.message }
            };
          }
          break;
        case 'node_cached':
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'cached' };
          break;
        case 'node_completed':
        case 'node_failed': {
          nodeStatus = { ...nodeStatus, [ev.node_id]: ev.type === 'node_completed' ? 'completed' : 'failed' };
          // Compute duration from the matching node_started event if we
          // saw it. Cached and post-replay paths don't have a started_at,
          // and that's fine — we just don't render a duration for them.
          const startedAt = nodeStartedAt[ev.node_id];
          if (startedAt !== undefined && !Number.isNaN(ts)) {
            nodeDurationMs = { ...nodeDurationMs, [ev.node_id]: Math.max(0, ts - startedAt) };
          }
          break;
        }
      }
    }
    if (ev.type === 'job_completed' || ev.type === 'job_failed') {
      api.getJob(activeJob?.id ?? '')
        .then((j) => (activeJob = j))
        .catch(() => undefined);
    }
  }

  function pickPreviewPort(nodeId: string): string {
    // The template schema carries each node's declared output ports
    // (server-derived from the Python registry). Prefer `image` when present
    // so canonical viewable nodes still surface their primary frame; fall
    // back to the first declared output otherwise. Defaults to `image` for
    // legacy payloads that predate the `outputs` field on TemplateNodeSchema.
    const node = schema?.nodes.find((n) => n.node_id === nodeId);
    if (node?.outputs) {
      if ('image' in node.outputs) return 'image';
      const first = Object.keys(node.outputs)[0];
      if (first) return first;
    }
    return 'image';
  }

  function resetPipelineState() {
    nodeStatus = {};
    nodeProgress = {};
    nodeHash = {};
    nodeKind = {};
    nodePort = {};
    nodeStartedAt = {};
    nodeDurationMs = {};
    seenEventKey.clear();
  }

  /** Lighter reset for patch / revert / reprocess: keeps nodeHash,
   * nodePort, nodeKind, and previewLoaded so the previously-rendered
   * preview stays visible while the new job runs. Most edits hit the
   * cache for upstream nodes anyway, so the hash stays the same and
   * the IMG element doesn't even reload - no flash. Only job-tied
   * state (status, progress, durations, dedupe set) gets cleared. */
  function softResetForNewJob() {
    nodeStatus = Object.fromEntries(
      (schema?.nodes ?? []).map((n) => [n.node_id, 'pending' as const])
    );
    nodeProgress = {};
    nodeStartedAt = {};
    nodeDurationMs = {};
    seenEventKey.clear();
  }

  function formatStepDuration(ms: number | undefined): string {
    // Sub-second: "0.4s" reads better than "400ms" on a status badge that
    // already trends toward seconds; keep two-digit precision until 10s
    // then drop decimals so the pill doesn't drift wider with wall-clock.
    if (ms === undefined) return '';
    if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
    const m = Math.floor(ms / 60_000);
    const s = Math.round((ms % 60_000) / 1000);
    return `${m}m ${s}s`;
  }

  async function attachToJob(jobId: string) {
    if (subscribedJobId === jobId) return;
    // Don't null activeJob here - keep the previous job's metadata
    // visible (capture line, status pill) until the new fetch resolves.
    // attachToJob's `activeJob = fresh` swap below replaces it in
    // place, so the user never sees an empty header during patches.
    detachFromJob({ clearActiveJob: false });
    subscribedJobId = jobId;
    try {
      const fresh = await api.getJob(jobId);
      if (subscribedJobId !== jobId) return;
      activeJob = fresh;
      const history = await api.getJobEvents(jobId);
      if (subscribedJobId !== jobId) return;
      for (const ev of history) applyEvent(ev);
      if (fresh.status === 'queued' || fresh.status === 'running') {
        ws = api.subscribeJobEvents(jobId, applyEvent);
      }
    } catch (e) {
      toast.error(`Couldn't load job ${jobId}: ${(e as Error).message}`);
    }
  }

  function detachFromJob({ clearActiveJob = true }: { clearActiveJob?: boolean } = {}) {
    ws?.close();
    ws = null;
    subscribedJobId = null;
    if (clearActiveJob) activeJob = null;
  }

  async function loadProject(rid: string) {
    try {
      const fresh = await api.getProject(rid);
      if (rid !== id) return;
      project = fresh;
      syncDescriptionFromProject(fresh);
      schema = await api.getTemplateSchema(fresh.template_id);
      if (rid !== id) return;
      const initKind: Record<string, string> = {};
      const initPort: Record<string, string> = {};
      for (const n of fresh.template.nodes) {
        initKind[n.id] = n.kind;
        initPort[n.id] = pickPreviewPort(n.id);
      }
      nodeKind = initKind;
      nodePort = initPort;
      nodeStatus = Object.fromEntries(
        fresh.template.nodes.map((n) => [n.id, 'pending'])
      );
      await attachToJob(fresh.current_job_id);
    } catch (e) {
      toast.error(`Couldn't load project ${rid}: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    if (!id) return;
    project = null;
    schema = null;
    // Drop the previous project's source-session list so the collapsible
    // doesn't briefly render stale rows under the new project's header.
    sourceSessions = null;
    sourceSessionsForProjectId = null;
    sourceSessionsError = null;
    sessionNotesOpenId = null;
    sessionNotesDraftById = new Map();
    resetPipelineState();
    detachFromJob();
    loadProject(id);
  });

  // While this page is mounted, widen the global container so the pipeline
  // strip + params grid actually use ultrawide real estate. Removed on
  // unmount so other pages stay at the comfortable reading width.
  $effect(() => {
    document.body.classList.add('project-page');
    return () => document.body.classList.remove('project-page');
  });

  onDestroy(() => {
    if (patchTimer) clearTimeout(patchTimer);
    detachFromJob();
  });

  function onNodeOverrideChange(
    nodeId: string,
    partialForNode: Record<string, unknown>
  ) {
    if (!pendingOverrides) pendingOverrides = {};
    pendingOverrides[nodeId] =
      Object.keys(partialForNode).length === 0 ? null : partialForNode;
    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(flushPatch, PATCH_DEBOUNCE_MS);
  }

  /** Project-level on/off toggle for nodes with an `enabled` schema field.
   * Reuses the override mechanism but exposed as a header switch instead of
   * a buried checkbox in the params body. We compute the new override map
   * by hand (vs delegating to NodeParamsForm) because the toggle isn't
   * inside the form. */
  function toggleNodeEnabled(
    nodeId: string,
    schemaProps: Record<string, { default?: unknown }>,
    fullDefaults: Record<string, unknown>,
    currentOverrides: Record<string, unknown>
  ) {
    const cur = ('enabled' in currentOverrides
      ? currentOverrides.enabled
      : fullDefaults.enabled) as boolean | undefined;
    const next = { ...currentOverrides };
    const newValue = !cur;
    // Match NodeParamsForm's "drop override when it equals default" rule
    // so the cache hash and the modified-count badge stay accurate.
    if (newValue === fullDefaults.enabled) {
      delete next.enabled;
    } else {
      next.enabled = newValue;
    }
    onNodeOverrideChange(nodeId, next);
  }

  function effectiveEnabled(
    fullDefaults: Record<string, unknown>,
    overrides: Record<string, unknown>
  ): boolean {
    const v = 'enabled' in overrides ? overrides.enabled : fullDefaults.enabled;
    // Default to true when schema has no `enabled` field at all (e.g. always-on
    // nodes like stretch/save). Callers gate on isTogglable() before reading
    // this, so the fallback only matters for defensive UI code paths.
    return v === undefined ? true : Boolean(v);
  }

  async function flushPatch() {
    patchTimer = null;
    if (!project || !pendingOverrides) return;
    const overrides = pendingOverrides;
    pendingOverrides = null;
    patching = true;
    try {
      const next = await api.patchProject(project.id, { overrides });
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't apply changes: ${(e as Error).message}`);
    } finally {
      patching = false;
    }
  }

  // ---- Notes (description) autosave ---------------------------------
  // Local mirror of project.description so the textarea is responsive
  // without round-tripping every keystroke. Saved on blur (no debounce
  // timer needed); the PATCH does not submit a new job so it's cheap.
  let descriptionDraft = $state<string>('');
  let descriptionSaving = $state(false);

  /** Sync the draft from server state. Called when the project payload
   * lands so re-opens / undo / external edits all flow back into the
   * textarea. Idempotent on identical values to keep the cursor
   * position stable while typing. */
  function syncDescriptionFromProject(p: Project | null) {
    const next = p?.description ?? '';
    if (descriptionDraft !== next) descriptionDraft = next;
  }

  async function saveDescriptionOnBlur() {
    if (!project) return;
    const server = project.description ?? '';
    // No-op when nothing changed (the common case after focus + blur).
    if (descriptionDraft === server) return;
    descriptionSaving = true;
    try {
      const next = await api.patchProject(project.id, {
        description: descriptionDraft,
      });
      project = next;
      // Refresh the local mirror with whatever the server normalized to
      // (empty string -> null), so the next blur doesn't keep retrying.
      syncDescriptionFromProject(next);
    } catch (e) {
      toast.error(`Couldn't save notes: ${(e as Error).message}`);
    } finally {
      descriptionSaving = false;
    }
  }

  function onProjectUpdated(next: Project) {
    project = next;
    syncDescriptionFromProject(next);
    softResetForNewJob();
    attachToJob(next.current_job_id);
  }

  async function reprocess() {
    if (!project) return;
    reprocessing = true;
    try {
      const next = await api.patchProject(project.id, { force: true });
      toast.info('Reprocessing — every step runs from scratch');
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't reprocess: ${(e as Error).message}`);
    } finally {
      reprocessing = false;
    }
  }

  async function revertTo(seq: number) {
    if (!project) return;
    try {
      const next = await api.revertProject(project.id, seq);
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't revert: ${(e as Error).message}`);
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (!project) return;
    const meta = e.metaKey || e.ctrlKey;
    if (!meta) return;
    if (e.key === 'z' && !e.shiftKey) {
      e.preventDefault();
      const prev = project.current_seq - 1;
      if (prev >= 0 && project.history.some((h) => h.seq === prev)) {
        revertTo(prev);
      }
    } else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') {
      e.preventDefault();
      const next = project.current_seq + 1;
      if (project.history.some((h) => h.seq === next)) {
        revertTo(next);
      }
    }
  }

  const finalOutput = $derived.by(() => {
    if (!activeJob?.outputs) return null;
    const entries = Object.entries(activeJob.outputs);
    const named = entries.find(([k]) => k === 'image');
    return named ?? (entries[0] ?? null);
  });

  // The schema declares its public outputs as `{ "image": "save_image.image" }`.
  // We treat the node behind the `image` port (or the first declared
  // output) as the project's "final/output" node — its accordion row
  // gets the bigger preview + share/cover controls when expanded.
  const outputNodeId = $derived.by(() => {
    if (!schema) return null;
    const entries = Object.entries(schema.outputs);
    const named = entries.find(([k]) => k === 'image') ?? entries[0];
    if (!named) return null;
    return named[1].split('.')[0] || null;
  });

  // Land with the output node expanded so the user sees the final
  // image immediately. Re-syncs whenever the project / schema swaps
  // (open a different project: re-prime; same project, same schema:
  // no-op because the set already contains it).
  $effect(() => {
    if (outputNodeId && expandedNodes.size === 0) {
      expandedNodes = new Set([outputNodeId]);
    }
  });

  // Keep the history strip scrolled to the right edge (newest entry).
  // Triggers on every history.length change so a fresh PATCH that
  // appends a new version auto-scrolls into view, and on initial load
  // so the user lands on the most recent render instead of v1. The
  // length read is what makes this $effect reactive.
  $effect(() => {
    const len = project?.history.length ?? 0;
    if (len === 0 || !historyEl) return;
    // Defer one frame so the new <li> is rendered before we measure
    // scrollWidth — otherwise we scroll to the prior right edge and
    // miss the freshly-appended entry.
    requestAnimationFrame(() => {
      if (historyEl) historyEl.scrollLeft = historyEl.scrollWidth;
    });
  });

  const undoDisabled = $derived(
    !project || !project.history.some((h) => h.seq === (project!.current_seq - 1))
  );
  const redoDisabled = $derived(
    !project || !project.history.some((h) => h.seq === (project!.current_seq + 1))
  );

  function shortHistoryLabel(label: string | null): string {
    if (!label) return '';
    return label.length > 80 ? label.slice(0, 77) + '…' : label;
  }

  async function copyToClipboard(text: string, msg = 'Copied to clipboard') {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(msg);
    } catch (e) {
      toast.error(`Copy failed: ${(e as Error).message}`);
    }
  }

  // Resolve the preview URL for whatever node feeds `nid`'s `image` port. The
  // crop editor needs the upstream stretched PNG to drag a rectangle on; we
  // already have the cache hash for completed nodes via nodeHash[].
  function upstreamPreviewFor(nid: string): string | null {
    if (!project) return null;
    const node = project.template.nodes.find((n) => n.id === nid);
    if (!node) return null;
    const src = node.inputs?.image;
    if (!src) return null;
    const srcId = src.split('.')[0];
    const h = nodeHash[srcId];
    const port = nodePort[srcId] ?? 'image';
    if (!h) return null;
    return api.previewUrl(h, port);
  }

  // ---- Source sessions (read-only, collapsible) --------------------
  // The "Sessions used" `<details>` at the top of the page fans out
  // GET /api/sessions/{id} for each id in project.source_session_ids
  // when the user expands the panel. We cache results so reopening is
  // free. Same shape as TargetDetail.sessions on the Library so we can
  // reuse SessionRow visuals.
  let sourceSessions = $state<SessionSummary[] | null>(null);
  let sourceSessionsLoading = $state(false);
  let sourceSessionsError = $state<string | null>(null);
  let sourceSessionsOpen = $state(false);
  // Track which project we've loaded sessions for so navigating to a
  // different project clears the cache (rather than rendering the
  // previous project's session list under the new project's header).
  let sourceSessionsForProjectId = $state<string | null>(null);

  async function loadSourceSessions() {
    if (!project) return;
    const projectId = project.id;
    sourceSessionsLoading = true;
    sourceSessionsError = null;
    try {
      const ids = project.source_session_ids
        .map((s) => Number(s))
        .filter((n) => Number.isFinite(n));
      // Settled-not-rejected: a single 404 (session deleted out from
      // under the project) shouldn't break the whole list.
      const results = await Promise.allSettled(ids.map((id) => api.getSession(id)));
      // Guard against the user navigating away mid-fetch: drop the
      // result if the active project changed.
      if (!project || project.id !== projectId) return;
      const sessions: SessionSummary[] = [];
      for (const r of results) {
        if (r.status === 'fulfilled') sessions.push(r.value);
      }
      sourceSessions = sessions;
      sourceSessionsForProjectId = projectId;
    } catch (e) {
      sourceSessionsError = (e as Error).message;
    } finally {
      sourceSessionsLoading = false;
    }
  }

  function onSourceSessionsToggle(e: Event) {
    const open = (e.currentTarget as HTMLDetailsElement).open;
    sourceSessionsOpen = open;
    // Lazy-load on first open. Subsequent opens hit the cached array
    // (sessions don't change without a rescan, and a rescan is a
    // library-level action).
    if (
      open
      && project
      && (sourceSessions === null || sourceSessionsForProjectId !== project.id)
    ) {
      loadSourceSessions();
    }
  }

  // ---- Per-session notes state (mirrors the Library page) -----------
  // The textarea inside SessionRow autosaves on blur; we own the open
  // id, the per-id drafts, and the "currently saving" id so the row's
  // small "· saving…" affordance lights up correctly.
  let sessionNotesOpenId = $state<number | null>(null);
  let sessionNotesDraftById = $state<Map<number, string>>(new Map());
  let sessionNotesSavingId = $state<number | null>(null);

  function toggleSessionNotes(s: SessionSummary) {
    if (sessionNotesOpenId === s.id) {
      sessionNotesOpenId = null;
      return;
    }
    if (!sessionNotesDraftById.has(s.id)) {
      const next = new Map(sessionNotesDraftById);
      next.set(s.id, s.description ?? '');
      sessionNotesDraftById = next;
    }
    sessionNotesOpenId = s.id;
  }

  function setSessionNotesDraft(sessionId: number, value: string) {
    const next = new Map(sessionNotesDraftById);
    next.set(sessionId, value);
    sessionNotesDraftById = next;
  }

  async function saveSessionNotesOnBlur(s: SessionSummary) {
    const draft = sessionNotesDraftById.get(s.id) ?? '';
    const server = s.description ?? '';
    if (draft === server) return;
    sessionNotesSavingId = s.id;
    try {
      const resp = await api.patchSession(s.id, { description: draft });
      const updated = resp.session;
      if (sourceSessions) {
        sourceSessions = sourceSessions.map((existing) =>
          existing.id === s.id ? updated : existing,
        );
      }
      const draftNext = new Map(sessionNotesDraftById);
      draftNext.set(s.id, updated.description ?? '');
      sessionNotesDraftById = draftNext;
    } catch (e) {
      toast.error(`Couldn't save notes: ${(e as Error).message}`);
    } finally {
      sessionNotesSavingId = null;
    }
  }

  // ---- Calibration tooltip helpers (mirrors the Library page) -------
  // Duplicated rather than imported because they're tiny and tightly
  // coupled to the row visuals; lifting them into $lib feels like
  // premature abstraction.
  const KIND_NAME: Record<string, string> = {
    dark: 'Dark',
    flat: 'Flat',
    bias: 'Bias',
  };
  const QUALITY_HELP: Record<string, string> = {
    exact: 'exact match',
    approx: 'approximate match (within tolerance)',
    none: 'no match found',
  };
  function calLabel(kind: string): string {
    return kind[0].toUpperCase();
  }
  function calTitle(c: CalibrationStatus): string {
    const base = `${KIND_NAME[c.kind] ?? c.kind} · ${QUALITY_HELP[c.quality] ?? c.quality}`;
    return c.reason && c.quality !== 'exact' ? `${base}\n${c.reason}` : base;
  }
  function shortSessionDate(iso: string | null): string {
    if (!iso) return '';
    return iso.slice(0, 10);
  }

  // Crop editor emits a full {enabled,x,y,width,height} bundle. Build the
  // partial-overrides map that NodeParamsForm would have built and run it
  // through the same debounced patch path.
  function onCropChange(
    nid: string,
    next: { enabled: boolean; x: number; y: number; width: number; height: number },
    defaults: Record<string, unknown>
  ) {
    const partial: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(next)) {
      if (defaults[k] !== v) partial[k] = v;
    }
    onNodeOverrideChange(nid, partial);
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="project-root">
  <div class="header">
    {#if project}
      <!-- Header title block: catalog common name as the H1 (e.g.
           "Triangulum Galaxy"), the user's stored target name muted
           beside it (e.g. "M 33"). The muted half is suppressed when
           it would just repeat the H1 (user named the target "Wizard
           Nebula" themselves -> no muted echo). When the project has
           no resolved display, the H1 falls back to the user's stored
           target name (from the capture metadata) and no muted half
           appears at all. -->
      <div class="title-block">
        {#if project.display}
          {@const capName = activeJob?.capture?.target_name?.trim() ?? ''}
          {@const echo = capName && capName.toLowerCase() !== project.display.name.toLowerCase()}
          <h1 title="Catalog: {project.display.canonical}">
            {project.display.name}
            {#if echo}
              <span class="title-canonical muted small">{capName}</span>
            {/if}
          </h1>
        {:else if activeJob?.capture?.target_name}
          <h1>{activeJob.capture.target_name}</h1>
        {:else}
          <h1>{project.name}</h1>
        {/if}
      </div>
      <span class="version muted small">
        v{project.current_seq + 1} of {project.history.length}
      </span>
      <button
        type="button"
        class="hbtn"
        onclick={() => revertTo(project!.current_seq - 1)}
        disabled={undoDisabled}
        title="Undo (Cmd-Z)"
      >↶ Undo</button>
      <button
        type="button"
        class="hbtn"
        onclick={() => revertTo(project!.current_seq + 1)}
        disabled={redoDisabled}
        title="Redo (Cmd-Shift-Z)"
      >↷ Redo</button>
      <button
        type="button"
        class="hbtn warn reprocess"
        onclick={reprocess}
        disabled={reprocessing || patching}
        title="Re-run every step from scratch (bypasses the cache)"
      >
        {reprocessing ? 'Submitting…' : 'Reprocess'}
      </button>
    {/if}
  </div>

  {#if project === null}
    <p class="muted">Loading…</p>
  {:else}
    <p class="capture-line muted small">
      {#if activeJob?.capture?.frame_count}
        <span>{activeJob.capture.frame_count} frame{activeJob.capture.frame_count === 1 ? '' : 's'}</span>
      {/if}
      {#if project.capture?.integration_seconds && project.capture.integration_seconds > 0}
        <span aria-hidden="true">·</span>
        <span title="Useful integration time across source sessions">
          {formatIntegrationTime(project.capture.integration_seconds)} integ
        </span>
      {/if}
      {#if project.capture && project.capture.bytes_on_disk > 0}
        <span aria-hidden="true">·</span>
        <span title="Source-frame bytes on disk">
          {formatBytes(project.capture.bytes_on_disk)}
        </span>
      {/if}
      <span aria-hidden="true">·</span>
      <span title={project.created_at}>created {shortAgo(project.created_at)}</span>
      {#if activeJob?.started_at}
        <span aria-hidden="true">·</span>
        <span>{formatDuration(activeJob.started_at, activeJob.finished_at)}</span>
      {/if}
      {#if activeJob}
        <span aria-hidden="true">·</span>
        <span class="status status-{activeJob.status}">{activeJob.status}</span>
      {/if}
      {#if patching}
        <span aria-hidden="true">·</span>
        <span class="status status-running">applying…</span>
      {/if}
    </p>

    <!-- Free-text notes the user attaches to the project (capture
         conditions, gear tweaks, processing rationale). Autosaves on
         blur; the PATCH does not submit a new job so it's a cheap
         metadata write. Empty/whitespace-only normalizes to null
         server-side; we keep the textarea visible regardless so the
         user can always start typing. -->
    <div class="notes-block">
      <label class="notes-label" for="project-notes">
        Notes
        {#if descriptionSaving}
          <span class="muted small">· saving…</span>
        {/if}
      </label>
      <textarea
        id="project-notes"
        class="notes-area"
        bind:value={descriptionDraft}
        onblur={saveDescriptionOnBlur}
        rows="2"
        placeholder="Add notes (capture conditions, gear tweaks, etc.). Unfocus to save."
      ></textarea>
    </div>

    <!-- Sessions used: collapsible read-only view of the source
         sessions backing this project. Helpful when the user needs to
         look back at the raw capture metadata without leaving the
         project page. Run / reassign / multi-select are intentionally
         omitted (renders happen project-wide; reassigning would yank a
         session out of this project, which is a surprising side-effect
         to hide behind a pencil). -->
    {@const sessionCount = project.source_session_ids.length}
    <details
      class="sources"
      ontoggle={onSourceSessionsToggle}
    >
      <summary class="sources-summary">
        <svg
          class="sources-chevron"
          viewBox="0 0 24 24"
          width="14"
          height="14"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
        <span class="sources-title">Sessions used</span>
        <span class="sources-count muted">({sessionCount})</span>
      </summary>
      <div class="sources-body">
        {#if sourceSessionsLoading && sourceSessions === null}
          <p class="muted small">Loading sessions…</p>
        {:else if sourceSessionsError && sourceSessions === null}
          <p class="muted small">Couldn't load sessions: {sourceSessionsError}</p>
        {:else if sourceSessions && sourceSessions.length === 0}
          <p class="muted small">No source sessions resolved. The capture rows may have been removed since this project was created.</p>
        {:else if sourceSessions}
          <ul class="sources-list">
            {#each sourceSessions as s (s.id)}
              <SessionRow
                session={s}
                shortDate={shortSessionDate(s.started_at)}
                notesOpen={sessionNotesOpenId === s.id}
                notesDraft={sessionNotesDraftById.get(s.id) ?? ''}
                notesSaving={sessionNotesSavingId === s.id}
                onToggleNotes={() => toggleSessionNotes(s)}
                onNotesInput={(v) => setSessionNotesDraft(s.id, v)}
                onNotesBlur={() => saveSessionNotesOnBlur(s)}
                {calLabel}
                {calTitle}
              />
            {/each}
          </ul>
        {/if}
      </div>
    </details>

    <!-- Single accordion: each node is one row, click to expand its
         params + larger preview. Replaces the old parallel pipeline
         strip + params grid + output section. -->
    {#if schema && project}
      {@const isCover = project.cover_seq === project.current_seq}
      <section class="nodes">
        <h2 class="section-h">Pipeline</h2>
        <ol class="node-list">
          {#each schema.nodes as nschema (nschema.node_id)}
            {@const nid = nschema.node_id}
            {@const s = nodeStatus[nid] ?? 'pending'}
            {@const p = nodeProgress[nid]}
            {@const h = nodeHash[nid]}
            {@const port = nodePort[nid] ?? 'image'}
            {@const kind = nodeKind[nid] ?? nschema.kind ?? nid}
            {@const isOutput = nid === outputNodeId}
            {@const isExpanded = expandedNodes.has(nid)}
            {@const closureCost = blastRadiusCost(project.template, nid, costByNode)}
            {@const overrides = (project.current_overrides[nid] as Record<string, unknown>) ?? {}}
            {@const props = nschema.schema.properties ?? {}}
            {@const fullDefaults = { ...nschema.defaults, ...nschema.template_params } as Record<string, unknown>}
            {@const togglable = isTogglable(props)}
            {@const enabled = togglable ? effectiveEnabled(fullDefaults, overrides) : true}
            {@const modifiedCount = Object.keys(overrides).length}
            {@const visible = isNodeVisible(nid, schemaByNodeId, project.current_overrides as Record<string, Record<string, unknown>>)}
            {#if visible}
            <li
              class="node-row node-{s}"
              class:expanded={isExpanded}
              class:output={isOutput}
              class:disabled={togglable && !enabled}
              transition:fade={{ duration: 160, easing: cubicOut }}
            >
              <!-- The head was a `<div role="button">` so we could nest a
                   toggle <button> inside it (HTML disallows nesting real
                   buttons). That worked structurally but kept regressing
                   under Svelte 5's event delegation - click order vs.
                   stopPropagation got us twice. Now the head is a plain
                   relative container; the click target for expand is an
                   invisible full-cover sibling button, and the toggle is
                   a separate sibling. No nesting, no propagation guards,
                   no event-order assumptions. -->
              <div class="node-head">
                <!-- Invisible click target. Sits at the bottom of the
                     z-stack so visual content paints over it; visuals
                     have pointer-events: none so clicks land here. -->
                <button
                  type="button"
                  class="head-expand"
                  aria-expanded={isExpanded}
                  aria-label={isExpanded ? `Collapse ${nodeDisplayName(kind, nid)}` : `Expand ${nodeDisplayName(kind, nid)}`}
                  onclick={() => toggleNode(nid)}
                ></button>
                <!-- Card body: 16:9 preview that fills the tile when
                     collapsed. When the row is expanded this shrinks
                     into a small left-side thumb (CSS handles it via
                     .expanded). -->
                <div class="head-thumb">
                  {#if (s === 'completed' || s === 'cached') && h}
                    {#if !previewLoaded[nid]}
                      <div class="flow-skeleton" aria-hidden="true"></div>
                    {/if}
                    <img
                      class="head-img"
                      class:loaded={previewLoaded[nid]}
                      src={api.previewUrl(h, port)}
                      alt=""
                      loading="lazy"
                      onload={() => onPreviewLoad(nid)}
                      onerror={() => onPreviewError(nid)}
                    />
                  {:else if s === 'running' && p}
                    <span class="head-pct">{Math.round((p.fraction ?? 0) * 100)}%</span>
                  {:else}
                    <span class="head-status muted">{s}</span>
                  {/if}
                  {#if s === 'running' && p}
                    <div class="head-progress" style:width="{(p.fraction ?? 0) * 100}%"></div>
                  {/if}
                </div>

                <!-- Title overlay: gradient strip across the top of
                     the card when collapsed, plain bar when expanded. -->
                <div class="head-overlay">
                  <span class="head-name">{nodeDisplayName(kind, nid)}</span>
                  <span class="status status-mini status-{togglable && !enabled ? 'off' : s}">
                    {togglable && !enabled
                      ? 'off'
                      : s}{#if (s === 'completed' || s === 'failed') && nodeDurationMs[nid] && enabled}<span class="dur"> · {formatStepDuration(nodeDurationMs[nid])}</span>{/if}
                  </span>
                  {#if isOutput}
                    <span class="output-tag">final</span>
                  {/if}
                  <span class="head-spacer"></span>
                  {#if modifiedCount > 0}
                    <span
                      class="badge-modified"
                      title="{modifiedCount} modified parameter{modifiedCount === 1 ? '' : 's'}"
                    >
                      {#if isExpanded}
                        {modifiedCount} modified
                      {:else}
                        ●{modifiedCount}
                      {/if}
                    </span>
                  {/if}
                  {#if togglable}
                    <!-- Sibling-of-the-expand-button under the hood:
                         .head-overlay has pointer-events: none, the
                         toggle re-enables them locally. So even though
                         the markup is nested, the runtime click stack
                         is just (toggle | expand) - no propagation
                         games. -->
                    <button
                      type="button"
                      class="node-toggle"
                      class:on={enabled}
                      role="switch"
                      aria-checked={enabled}
                      aria-label={enabled ? `Disable ${nodeDisplayName(kind, nid)}` : `Enable ${nodeDisplayName(kind, nid)}`}
                      title={enabled ? 'On - click to skip this step' : 'Off - click to run this step'}
                      onclick={() => toggleNodeEnabled(nid, props, fullDefaults, overrides)}
                    >
                      <span class="node-toggle-knob"></span>
                    </button>
                  {/if}
                  <svg class="chevron" class:rotated={isExpanded} viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </div>
              </div>

              {#if isExpanded}
                <div
                  class="node-body"
                  class:body-output={isOutput}
                  transition:slide={{ duration: 220, easing: cubicOut }}
                >
                  {#if isOutput && finalOutput}
                    {@const fname = finalOutput[0]}
                    {@const fref = finalOutput[1]}
                    <div class="output-actions">
                      <button
                        type="button"
                        class="cover-btn"
                        class:active={isCover}
                        disabled={coverBusy}
                        onclick={toggleCover}
                        title={isCover
                          ? 'This version is the project cover. Click to clear.'
                          : 'Pin this version as the project cover'}
                      >
                        <svg viewBox="0 0 24 24" width="14" height="14" fill={isCover ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                          <path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z" />
                        </svg>
                        {isCover ? 'Cover' : 'Set as cover'}
                      </button>
                      <button
                        type="button"
                        class="cover-btn"
                        onclick={() => copyToClipboard(fref.path, 'Copied output path')}
                        title="Copy filesystem path"
                      >
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                          <rect x="9" y="9" width="13" height="13" rx="2" />
                          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                        </svg>
                        Copy path
                      </button>
                      <a
                        class="cover-btn"
                        href={api.previewUrl(fref.node_hash, fname)}
                        target="_blank"
                        rel="noopener"
                        title="Open full-size in a new tab"
                      >
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                          <polyline points="15 3 21 3 21 9" />
                          <line x1="10" y1="14" x2="21" y2="3" />
                        </svg>
                        Open full
                      </a>
                    </div>
                  {:else}
                    <!-- The head card already shows the preview thumbnail,
                         so the body just hosts the editor / params. Crop
                         is a special case where the editor itself wraps
                         a (different, upstream) preview. -->
                    {#if kind === 'crop'}
                      {@const eff = (k: string) => (k in overrides ? overrides[k] : fullDefaults[k])}
                      {@const upstream = upstreamPreviewFor(nid)}
                      <div class="stage-params crop-host">
                        <CropEditor
                          previewUrl={upstream}
                          enabled={Boolean(eff('enabled'))}
                          x={Number(eff('x') ?? 0)}
                          y={Number(eff('y') ?? 0)}
                          width={Number(eff('width') ?? 1)}
                          height={Number(eff('height') ?? 1)}
                          costLabel={closureCost}
                          onchange={(next) => onCropChange(nid, next, fullDefaults)}
                          onreset={() => onNodeOverrideChange(nid, {})}
                        />
                      </div>
                    {:else if Object.keys(props).length > 0}
                      <div class="stage-params">
                        <NodeParamsForm
                          nodeId={nid}
                          schemaProps={props}
                          defaults={fullDefaults}
                          {overrides}
                          cost={closureCost}
                          hideFields={togglable ? ['enabled'] : []}
                          onchange={(next) => onNodeOverrideChange(nid, next)}
                        />
                      </div>
                    {:else}
                      <p class="muted small no-params">No editable parameters.</p>
                    {/if}
                  {/if}
                </div>
              {/if}
            </li>
            {/if}
          {/each}
        </ol>
      </section>
    {/if}

    {#if activeJob?.error}
      <section class="error-box">
        <h2 class="section-h">Error</h2>
        <pre>{activeJob.error}</pre>
      </section>
    {/if}

    <section class="history">
      <div class="history-head">
        <h2 class="section-h">History <span class="muted small">({project.history.length})</span></h2>
        {#if compareA !== null || compareB !== null}
          <span class="compare-status muted small">
            Compare:
            {#if compareA !== null}<span class="slot a">A=v{compareA + 1}</span>{/if}
            {#if compareB !== null}<span class="slot b">B=v{compareB + 1}</span>{/if}
            {#if compareA !== null && compareB !== null}
              <button type="button" class="ghost-btn" onclick={() => (compareOpen = true)}>
                Open
              </button>
            {/if}
            <button type="button" class="ghost-btn" onclick={clearCompare}>Clear</button>
          </span>
        {/if}
      </div>
      <ol class="history-strip" bind:this={historyEl}>
        {#each project.history as h (h.seq)}
          {@const slot = compareA === h.seq ? 'A' : compareB === h.seq ? 'B' : null}
          <li
            class="hist-entry"
            class:active={h.seq === project.current_seq}
            class:slot-a={slot === 'A'}
            class:slot-b={slot === 'B'}
          >
            <button type="button" onclick={() => revertTo(h.seq)} title={h.label ?? ''}>
              <span class="hist-seq muted">v{h.seq + 1}</span>
              <span class="hist-label">{shortHistoryLabel(h.label)}</span>
              <span class="hist-time muted small">{shortAgo(h.created_at)}</span>
            </button>
            <button
              type="button"
              class="publish-toggle"
              class:on={h.published}
              disabled={publishBusy[h.seq]}
              aria-pressed={h.published}
              aria-label={h.published
                ? `Unpublish v${h.seq + 1} from gallery`
                : `Publish v${h.seq + 1} to gallery`}
              title={h.published
                ? 'In gallery — click to unpublish'
                : 'Publish to gallery'}
              onclick={() => togglePublished(h.seq, h.published)}
            >
              <!-- Filled star when published, hollow otherwise. Single
                   path swap keeps the click target stable. -->
              {#if h.published}
                <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
                  <path
                    fill="currentColor"
                    d="M12 2.5l2.95 5.98 6.6.96-4.78 4.66 1.13 6.58L12 17.6l-5.9 3.1 1.13-6.58L2.45 9.44l6.6-.96L12 2.5z"
                  />
                </svg>
              {:else}
                <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
                  <path
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.6"
                    stroke-linejoin="round"
                    d="M12 2.5l2.95 5.98 6.6.96-4.78 4.66 1.13 6.58L12 17.6l-5.9 3.1 1.13-6.58L2.45 9.44l6.6-.96L12 2.5z"
                  />
                </svg>
              {/if}
            </button>
            <button
              type="button"
              class="compare-toggle"
              class:armed={slot !== null}
              disabled={compareLoading}
              aria-pressed={slot !== null}
              aria-label={slot
                ? `Clear compare slot ${slot} (v${h.seq + 1})`
                : compareA === null
                  ? `Pick v${h.seq + 1} as compare A`
                  : `Pick v${h.seq + 1} as compare B`}
              title={slot
                ? `Compare slot ${slot} - click to clear`
                : compareA === null
                  ? 'Pick as compare A'
                  : 'Pick as compare B'}
              onclick={() => toggleCompareSlot(h.seq)}
            >
              {#if slot}
                <span class="compare-slot-letter">{slot}</span>
              {:else}
                <!-- Two-pane wipe glyph: square split by a vertical
                     divider, hinting at the slider this opens. -->
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                  <rect x="1.5" y="2.5" width="13" height="11" rx="2"
                    fill="none" stroke="currentColor" stroke-width="1.4"/>
                  <line x1="8" y1="3" x2="8" y2="13" stroke="currentColor" stroke-width="1.4"/>
                </svg>
              {/if}
            </button>
          </li>
        {/each}
      </ol>
    </section>
  {/if}
</div>

{#if compareOpen && project && compareA !== null && compareB !== null}
  {@const aSrc = compareSrcFor(compareA)}
  {@const bSrc = compareSrcFor(compareB)}
  {@const aEntry = project.history.find((h) => h.seq === compareA)}
  {@const bEntry = project.history.find((h) => h.seq === compareB)}
  <!-- Modal lives outside .project-root so the backdrop can cover the
       whole viewport without z-index gymnastics. The dialog itself
       owns Escape + outside-click handling; the slider focuses on
       arrow-key wipe. -->
  <div
    class="compare-backdrop"
    role="presentation"
    onclick={closeCompare}
    onkeydown={onModalKeydown}
  >
    <div
      class="compare-dialog"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Compare history versions"
      onclick={(e) => e.stopPropagation()}
      onkeydown={onModalKeydown}
    >
      <header class="compare-dialog-head">
        <span class="muted small">Compare</span>
        <span class="compare-titles">
          <span class="slot a">A · v{compareA + 1}</span>
          <span class="muted">vs</span>
          <span class="slot b">B · v{compareB + 1}</span>
        </span>
        <button type="button" class="ghost-btn" onclick={closeCompare} aria-label="Close compare">
          ✕
        </button>
      </header>
      {#if aSrc && bSrc}
        <CompareSlider
          {aSrc}
          {bSrc}
          aLabel={aEntry?.label ?? `v${compareA + 1}`}
          bLabel={bEntry?.label ?? `v${compareB + 1}`}
        />
      {:else}
        <p class="muted">
          Couldn't load one of the previews. The job may have failed or its
          cache may have been evicted.
        </p>
      {/if}
    </div>
  </div>
{/if}

<style>
  .project-root {
    display: contents;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.25rem;
    flex-wrap: wrap;
  }
  .header h1 {
    margin: 0;
    font-size: 1.5rem;
    line-height: 1.15;
  }
  /* Title-block stacks: prominent display name, muted canonical id
     directly below, then the user-set project name in even smaller
     muted text. Vertical stack keeps the header dense without burying
     any of the three identifiers. */
  .title-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.05rem;
    flex: 1;
    min-width: 0;
  }
  .title-canonical {
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }
  .title-projectname {
    /* The user's own project name lives below the catalog labels so
       a rename still shows but doesn't compete with the catalog name
       for the eye. Italic + extra muted to signal "this is your tag,
       not the catalog truth". */
    font-style: italic;
    opacity: 0.75;
    line-height: 1.1;
  }
  .back {
    color: var(--fg-mute, #888);
    text-decoration: none;
  }
  .version {
    font-variant-numeric: tabular-nums;
  }

  .hbtn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #444);
    color: var(--accent, #5eead4);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .hbtn:hover:not(:disabled) {
    background: rgba(94, 234, 212, 0.1);
  }
  .hbtn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  /* Reprocess kicks off a real, expensive job (cache-busting full
     pipeline rerun). Recolor the pill in amber so it visually matches
     the .btn.warn convention used on /settings's Scan now. */
  .hbtn.warn {
    color: var(--warn, #fbbf24);
    border-color: var(--warn, #fbbf24);
  }
  .hbtn.warn:hover:not(:disabled) {
    background: rgba(251, 191, 36, 0.1);
  }
  .reprocess {
    margin-left: 0.25rem;
  }

  .capture-line {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.75rem;
  }
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute, #888);
  }
  .err {
    color: var(--bad, #f88);
  }

  .section-h {
    margin: 1.25rem 0 0.5rem;
    font-size: 1rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }

  /* Notes textarea sits below the capture line. Two rows by default so
     it doesn't dominate the viewport; the user can drag-resize. We
     accept the browser's native resize handle here on purpose: the
     few users who want a paragraph of notes can grow it and the rest
     get a compact one-liner. */
  .notes-block {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin: 0.25rem 0 0.75rem;
  }
  .notes-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }
  .notes-area {
    width: 100%;
    background: var(--bg-elev, #1a1a1a);
    color: var(--fg, #e6e6e6);
    border: 1px solid var(--border, #444);
    border-radius: 6px;
    padding: 0.4rem 0.5rem;
    font-size: 0.9rem;
    line-height: 1.4;
    resize: vertical;
    font-family: inherit;
  }
  .notes-area:focus {
    outline: none;
    border-color: var(--accent, #5eead4);
  }

  /* ---------- Sources collapsible ----------
     Read-only view of the project's source sessions. Stays collapsed
     by default so the pipeline below remains the page's center of
     gravity; rotates a chevron on open like the rest of the app's
     <details> usage. */
  .sources {
    margin: 0 0 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius, 8px);
    background: var(--bg-elev);
    overflow: hidden;
  }
  .sources-summary {
    list-style: none;
    cursor: pointer;
    padding: 0.55rem 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    user-select: none;
  }
  .sources-summary::-webkit-details-marker {
    display: none;
  }
  .sources-title {
    font-size: 0.95rem;
    font-weight: 500;
  }
  .sources-count {
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
  }
  .sources-chevron {
    color: var(--fg-mute);
    transition: transform 160ms ease;
    flex-shrink: 0;
  }
  .sources[open] .sources-chevron {
    transform: rotate(180deg);
  }
  .sources[open] .sources-summary {
    border-bottom: 1px solid var(--border);
  }
  .sources-body {
    padding: 0.6rem 0.85rem 0.85rem;
  }
  .sources-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  /* ---------- Pipeline accordion ----------
     One row per node. Header is always visible (thumb + name + status
     + chevron); body slides in when expanded with a NodeParamsForm
     plus, for the output node, a larger preview + share controls. */

  /* Grid of preview cards. Auto-fill at min 220px lands ~4 columns
     on a 1280-wide viewport, ~5-6 on ultrawide, 2 on tablet, 1 on
     phone. dense flow + each tile keeping its row height makes
     'expanded card spans the row' work without layout thrash. */
  .node-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    /* 300px min lands at 4-across on a typical 13-15" laptop, 5 on
       a 1600+ viewport, 2 on tablets, 1 on phones. Bumping the min
       past 280 was the difference between 'thumbnail you can read'
       and 'thumbnail you have to squint at'. */
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    grid-auto-flow: dense;
    /* Each card sits at the top of its cell instead of stretching to
       match the row's tallest item — without this, expanding one card
       drags every neighbor in the row to the same height. */
    align-items: start;
    gap: 0.7rem;
  }
  .node-row {
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    transition: border-color 160ms ease, transform 160ms ease;
    /* Each non-expanded card keeps its 16:9 footprint via head-thumb.
       Expanded cards override grid-column to span the full row. */
  }
  .node-row:hover { border-color: var(--border-strong); }
  .node-row:hover:not(.expanded) { transform: translateY(-1px); }

  /* Expanded: card grows vertically in place. Keeps the grid stable —
     no jarring full-width takeover, the body just drops down below
     the head card. The grid row's other cards stay where they are. */
  .node-row.expanded {
    border-color: var(--border-strong);
  }

  /* Output card — accent halo so the eye lands on it. */
  .node-row.output {
    box-shadow: 0 0 0 1px var(--accent-soft);
  }
  .node-row.output.expanded {
    box-shadow: 0 0 0 1px var(--accent-soft), 0 0 24px rgba(94, 234, 212, 0.06);
  }
  /* Status is conveyed by the pill in the title overlay; we used to also
     tint the card's left border, but it fought :hover (which sets all
     four sides) and produced two-tone borders. Pill is enough. */

  /* Head = the clickable card surface. Collapsed: thumb fills, title
     bar overlays at top with a gradient. Expanded: thumb shrinks to
     a small left-side preview, title bar goes flat across the top. */
  /* The head keeps the same card layout in both states — collapsed
     and expanded. Expanding doesn't reshape the head into a flat row;
     it just drops a body section below. Less visual jolt. */
  .node-head {
    background: transparent;
    color: inherit;
    width: 100%;
    text-align: left;
    border-radius: 0;
    position: relative;
    display: block;
  }

  /* Invisible click target. Sits at the bottom of the stack and
     receives any click that the visual layers (thumb, overlay) pass
     through via pointer-events: none. The toggle, by contrast, opts
     pointer-events back in so it captures its own clicks without any
     stopPropagation gymnastics. */
  .head-expand {
    position: absolute;
    inset: 0;
    appearance: none;
    background: transparent;
    border: 0;
    padding: 0;
    margin: 0;
    cursor: pointer;
    z-index: 0;
  }
  .head-expand:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }

  .head-thumb {
    position: relative;
    aspect-ratio: 16 / 9;
    background: var(--bg-elev-2);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    /* Visual only - the .head-expand button underneath collects clicks. */
    pointer-events: none;
    /* Smooth the dim/desaturate when a node is toggled off so the
       transition reads as 'this step skipped' rather than the card
       snapping to gray. */
    transition: opacity 220ms ease, filter 220ms ease;
  }
  .head-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    opacity: 0;
    transition: opacity 280ms ease;
  }
  .head-img.loaded { opacity: 1; }
  .head-pct {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--accent);
    font-size: 1.15rem;
  }
  .head-status {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .head-progress {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 2px;
    background: var(--accent);
    transition: width 200ms ease;
    z-index: 2;
  }

  /* Overlay: a top strip on the head card with a gradient fade. Same
     in both collapsed + expanded states so the card identity stays
     intact while expanding. */
  .head-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    padding: 0.45rem 0.6rem 0.85rem;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.85) 30%, rgba(0, 0, 0, 0));
    color: #fff;
    display: flex;
    align-items: center;
    gap: 0.45rem;
    pointer-events: none;
    z-index: 1;
  }

  .head-name {
    font-weight: 600;
    font-size: 0.95rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.6);
    /* Shrink first when the row is crowded so status + modified +
       chevron stay legible; ellipsis takes over below the natural
       width. */
    min-width: 0;
    flex-shrink: 1;
  }
  .head-spacer { flex: 1 1 0; min-width: 0.25rem; }
  .head-overlay > .status,
  .head-overlay > .badge-modified,
  .head-overlay > .output-tag,
  .head-overlay > .chevron { flex-shrink: 0; }
  /* Collapsed cards: shrink the modified badge to a tiny chip ('●N')
     so it doesn't crush the title at 300px wide. Full 'N modified'
     wording comes back when the card expands. */
  .node-row:not(.expanded) .badge-modified {
    border: none;
    background: var(--accent-soft);
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 0.65rem;
    padding: 0.05rem 0.45rem;
    text-transform: none;
    letter-spacing: 0;
  }

  .output-tag {
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--accent);
    background: var(--accent-soft);
    border: 1px solid var(--accent);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
  }

  .chevron {
    color: var(--fg-mute);
    transition: transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
    flex-shrink: 0;
  }
  .chevron.rotated { transform: rotate(180deg); }
  /* Collapsed cards: chevron sits on the dark gradient, so use white
     for contrast. */
  .node-row:not(.expanded) .chevron { color: rgba(255, 255, 255, 0.7); }

  /* Single-column body: preview-or-editor on top, params below. Cards
     are ~300px wide so a side-by-side layout is too tight. */
  .node-body {
    padding: 0.7rem 0.75rem 0.85rem;
    border-top: 1px solid var(--hairline);
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .stage-params { min-width: 0; }

  /* Skeleton shimmer underlay reused for the head thumbnail. */
  @keyframes flow-skeleton-shimmer {
    0%   { background-position: -150% 0, 0 0; }
    100% { background-position: 250% 0, 0 0; }
  }
  .flow-skeleton {
    position: absolute;
    inset: 0;
    background:
      linear-gradient(90deg, transparent 30%, var(--accent-soft) 50%, transparent 70%),
      linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    background-size: 200% 100%, 100% 100%;
    background-repeat: no-repeat;
    animation: flow-skeleton-shimmer 1.6s linear infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .flow-skeleton { animation: none; }
    .chevron { transition: none; }
  }

  /* ---------- Output pane (final node body) ---------- */

  .output-pane {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .output-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  /* ---------- Status pills ---------- */

  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-mini {
    font-size: 0.6rem;
    padding: 0.05rem 0.4rem;
  }
  .status-pending { background: rgba(255, 255, 255, 0.06); color: var(--fg-mute); }
  .status-queued { background: rgba(255, 255, 255, 0.10); color: var(--fg); }
  .status-running { background: var(--accent-soft); color: var(--accent); }
  .status-cached { background: rgba(255, 255, 255, 0.10); color: var(--fg-mute); }
  .status-completed { background: color-mix(in oklab, var(--good) 18%, transparent); color: var(--good); }
  .status-failed { background: color-mix(in oklab, var(--bad) 18%, transparent); color: var(--bad); }
  /* OFF state: visually distinct from 'cached' so the user instantly knows
     this step isn't running, vs running-but-served-from-cache. Muted enough
     to recede behind active steps. */
  .status-off {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg-mute);
    border: 1px solid var(--hairline);
  }

  /* ---------- Per-node enable/disable toggle ---------- */
  /* iOS-style switch in the card header. Lives inside the .head button so
     we have to stop propagation on click — the toggle should not also
     expand/collapse the card. */
  .node-toggle {
    appearance: none;
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid var(--hairline);
    width: 28px;
    height: 16px;
    border-radius: 999px;
    padding: 0;
    cursor: pointer;
    position: relative;
    flex-shrink: 0;
    transition: background-color 160ms ease, border-color 160ms ease;
    /* The overlay sets pointer-events: none so visual layers don't
       swallow clicks meant for the .head-expand sibling underneath.
       The toggle has to opt back in to receive its own clicks. */
    pointer-events: auto;
  }
  .node-toggle:hover {
    border-color: var(--border-strong);
  }
  .node-toggle.on {
    background: var(--accent);
    border-color: var(--accent);
  }
  .node-toggle-knob {
    position: absolute;
    top: 1px;
    left: 1px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--bg);
    transition: transform 160ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .node-toggle.on .node-toggle-knob {
    transform: translateX(12px);
    background: white;
  }
  .node-toggle:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* When a node is toggled off, dim the whole card so the eye skips it
     when scanning the pipeline. The header (toggle, name, chevron) stays
     fully opaque so the controls remain reachable. */
  .node-row.disabled .head-thumb {
    opacity: 0.35;
    filter: grayscale(0.6);
  }
  .node-row.disabled .head-thumb .flow-skeleton,
  .node-row.disabled .head-thumb .head-progress {
    /* No live progress should advertise activity for an OFF node.
       The cached preview img stays visible under the parent's dim +
       grayscale so the disable animates instead of snapping to a gray
       box. */
    display: none;
  }
  .node-row.disabled .head-thumb::after {
    content: 'off';
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-mute);
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .node-row.disabled .badge-modified {
    /* Modified-count chip is still relevant (maybe other params edited),
       but tone it down so the off state reads first. */
    opacity: 0.7;
  }

  .badge-modified {
    background: var(--accent-soft);
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .no-params {
    margin: 0.2rem 0;
  }

  /* ---------- Output / share affordances ---------- */
  /* Cover button — accent-tinted ghost. Filled star + accent BG when
     the current seq is already the cover. Stays light-weight so it
     doesn't compete with Reprocess up top. */
  .cover-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    transition: color 160ms ease, border-color 160ms ease, background 160ms ease;
  }
  .cover-btn:hover:not(:disabled) {
    color: var(--accent);
    border-color: var(--accent);
  }
  .cover-btn.active {
    color: var(--accent-ink);
    background: linear-gradient(135deg, var(--accent), var(--good));
    border-color: transparent;
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 14px var(--accent-soft);
  }
  .cover-btn:disabled { opacity: 0.55; cursor: progress; }

  .error-box {
    background: #2a1818;
    border: 1px solid #500;
    border-radius: 6px;
    padding: 0.75rem;
    margin: 1rem 0;
  }
  .error-box pre {
    margin: 0.25rem 0 0;
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  /* ---------- History strip ---------- */

  .history-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .compare-status {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.03);
  }
  .compare-status .slot {
    font-family: var(--font-mono, monospace);
    font-size: 0.75rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    border: 1px solid var(--border, #333);
  }
  .compare-status .slot.a {
    color: var(--accent, #5eead4);
    border-color: var(--accent, #5eead4);
  }
  .compare-status .slot.b {
    color: var(--bad, #ef4444);
    border-color: var(--bad, #ef4444);
  }
  .ghost-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .ghost-btn:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--accent, #5eead4);
    color: var(--accent, #5eead4);
  }

  .history-strip {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
    display: flex;
    gap: 0.4rem;
    overflow-x: auto;
    padding-bottom: 0.4rem;
  }
  .hist-entry {
    position: relative;
  }
  /* Scoped to the first button so sibling icon buttons (publish star,
     compare-toggle) keep their own dimensions instead of inheriting
     the column layout. */
  .hist-entry > button:first-child {
    appearance: none;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    border-radius: 6px;
    padding: 0.4rem 1.6rem 0.4rem 0.6rem;
    font: inherit;
    text-align: left;
    min-width: 14rem;
    max-width: 22rem;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    cursor: pointer;
  }
  .hist-entry > button:first-child:hover {
    background: rgba(94, 234, 212, 0.06);
  }
  .hist-entry.active > button:first-child {
    border-color: var(--accent, #5eead4);
    background: rgba(94, 234, 212, 0.12);
  }
  /* Armed slots tint the entire entry so the strip reads as 'these
     two are paired' even when the modal is closed. */
  .hist-entry.slot-a > button:first-child {
    border-color: var(--accent, #5eead4);
    box-shadow: inset 3px 0 0 var(--accent, #5eead4);
  }
  .hist-entry.slot-b > button:first-child {
    border-color: var(--bad, #ef4444);
    box-shadow: inset 3px 0 0 var(--bad, #ef4444);
  }
  /* Star toggle pinned to the top-right of the entry. Off state stays
     muted so it doesn't compete for attention; on state lights up in
     accent. Clicking it does NOT trigger revert (separate button). */
  .publish-toggle {
    position: absolute;
    top: 0.3rem;
    right: 0.3rem;
    appearance: none;
    background: transparent;
    border: 0;
    color: var(--fg-mute, #777);
    padding: 0.2rem;
    border-radius: 4px;
    cursor: pointer;
    line-height: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  /* Invisible 44x44 hit area so the icon stays visually small but the
     button meets phone tap-target minimums. */
  .publish-toggle::before {
    content: '';
    position: absolute;
    inset: -12px;
  }
  .publish-toggle:hover:not(:disabled) {
    color: var(--accent, #5eead4);
    background: rgba(255, 255, 255, 0.04);
  }
  .publish-toggle.on {
    color: var(--accent, #5eead4);
  }
  .publish-toggle:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .hist-seq {
    font-variant-numeric: tabular-nums;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .hist-label {
    font-size: 0.85rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .hist-time {
    font-size: 0.7rem;
  }

  /* Compare-toggle. Lives bottom-right of the entry (top-right is
     reserved for the publish star from #3) so the two icon buttons
     coexist without overlap. */
  .compare-toggle {
    position: absolute;
    bottom: 0.3rem;
    right: 0.3rem;
    appearance: none;
    background: transparent;
    border: 0;
    color: var(--fg-mute, #777);
    padding: 0.2rem;
    border-radius: 4px;
    cursor: pointer;
    line-height: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.4rem;
    min-height: 1.4rem;
  }
  /* Invisible 44x44 hit area; same trick as .publish-toggle. */
  .compare-toggle::before {
    content: '';
    position: absolute;
    inset: -12px;
  }
  .compare-toggle:hover:not(:disabled) {
    color: var(--accent, #5eead4);
    background: rgba(255, 255, 255, 0.04);
  }
  .compare-toggle.armed {
    color: var(--accent, #5eead4);
    background: var(--accent-soft, rgba(94, 234, 212, 0.14));
  }
  .compare-toggle:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .compare-slot-letter {
    font-family: var(--font-mono, monospace);
    font-weight: 700;
    font-size: 0.8rem;
    line-height: 1;
  }

  /* ---------- Compare modal ---------- */

  .compare-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.62);
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
    z-index: 100;
    display: grid;
    place-items: center;
    padding: 1.5rem;
  }
  .compare-dialog {
    background: var(--bg-elev-1, #14182b);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-card, 10px);
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.5);
    width: min(96vw, 1100px);
    max-height: 92vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .compare-dialog-head {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.6rem 0.9rem;
    border-bottom: 1px solid var(--border, #333);
  }
  .compare-titles {
    flex: 1;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.95rem;
  }
  .compare-titles .slot {
    font-family: var(--font-mono, monospace);
    font-size: 0.85rem;
  }
  .compare-titles .slot.a { color: var(--accent, #5eead4); }
  .compare-titles .slot.b { color: var(--bad, #ef4444); }
</style>
