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
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { page } from '$app/stores';
  import {
    api,
    type CostClass,
    type JobEvent,
    type JobSummary,
    type Project,
    type TemplateSchema
  } from '$lib/api';
  import {
    blastRadiusCost,
    nodeDisplayName
  } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatDuration, shortAgo } from '$lib/format';
  import NodeParamsForm from '$lib/NodeParamsForm.svelte';
  import CropEditor from '$lib/CropEditor.svelte';

  let project = $state<Project | null>(null);
  let schema = $state<TemplateSchema | null>(null);
  let activeJob = $state<JobSummary | null>(null);

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

  function pickPreviewPort(kind: string): string {
    // Sequence-typed outputs are previewed via the first frame; image-typed
    // get rendered directly. Keep this in sync with each node's declared
    // outputs in the Python registry.
    if (
      kind === 'convert_lights' ||
      kind === 'calibrate' ||
      kind === 'seq_resample' ||
      kind === 'seq_offset' ||
      kind === 'seq_register' ||
      kind === 'seq_bg_extract'
    ) return 'sequence';
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
    detachFromJob();
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

  function detachFromJob() {
    ws?.close();
    ws = null;
    subscribedJobId = null;
    activeJob = null;
  }

  async function loadProject(rid: string) {
    try {
      const fresh = await api.getProject(rid);
      if (rid !== id) return;
      project = fresh;
      schema = await api.getTemplateSchema(fresh.template_id);
      if (rid !== id) return;
      const initKind: Record<string, string> = {};
      const initPort: Record<string, string> = {};
      for (const n of fresh.template.nodes) {
        initKind[n.id] = n.kind;
        initPort[n.id] = pickPreviewPort(n.kind);
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

  function onProjectUpdated(next: Project) {
    project = next;
    resetPipelineState();
    if (schema) {
      nodeStatus = Object.fromEntries(
        schema.nodes.map((n) => [n.node_id, 'pending'] as const)
      );
      const initKind: Record<string, string> = {};
      const initPort: Record<string, string> = {};
      for (const n of next.template.nodes) {
        initKind[n.id] = n.kind;
        initPort[n.id] = pickPreviewPort(n.kind);
      }
      nodeKind = initKind;
      nodePort = initPort;
    }
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
      <h1>{project.name}</h1>
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
        class="hbtn reprocess"
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
      {#if activeJob?.capture}
        <span>{activeJob.capture.target_name ?? ''}</span>
        {#if activeJob.capture.frame_count}
          <span aria-hidden="true">·</span>
          <span>{activeJob.capture.frame_count} frame{activeJob.capture.frame_count === 1 ? '' : 's'}</span>
        {/if}
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
            {@const modifiedCount = Object.keys(overrides).length}
            <li class="node-row node-{s}" class:expanded={isExpanded} class:output={isOutput}>
              <button
                type="button"
                class="node-head"
                aria-expanded={isExpanded}
                onclick={() => toggleNode(nid)}
              >
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
                  <span class="head-name">{nodeDisplayName(kind)}</span>
                  <span class="status status-mini status-{s}">
                    {s}{#if (s === 'completed' || s === 'failed') && nodeDurationMs[nid]}<span class="dur"> · {formatStepDuration(nodeDurationMs[nid])}</span>{/if}
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
                  <svg class="chevron" class:rotated={isExpanded} viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                    <polyline points="6 9 12 15 18 9" />
                  </svg>
                </div>
              </button>

              {#if isExpanded}
                <div
                  class="node-body"
                  class:body-output={isOutput}
                  class:body-dual={!isOutput && kind !== 'crop' && Object.keys(props).length > 0 && (s === 'completed' || s === 'cached') && h}
                  transition:slide={{ duration: 220, easing: cubicOut }}
                >
                  {#if isOutput && finalOutput}
                    {@const fname = finalOutput[0]}
                    {@const fref = finalOutput[1]}
                    <a class="big-preview-link" href={api.previewUrl(fref.node_hash, fname)} target="_blank" rel="noopener">
                      <img class="big-preview" src={api.previewUrl(fref.node_hash, fname)} alt="output preview" />
                    </a>
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
                    <p class="path-line muted small">
                      <code class="path">{fref.path}</code>
                      <span class="type-tag">[{fref.type}]</span>
                    </p>
                  {:else}
                    <!-- Non-output expanded body: two columns when both
                         a preview and params exist. Preview goes on
                         the left so the user's eye starts at 'what
                         this stage produces' and lands on the knobs.
                         Crop nodes get their preview embedded in the
                         CropEditor itself, so we skip this strip. -->
                    {#if kind !== 'crop' && (s === 'completed' || s === 'cached') && h}
                      <a
                        class="stage-preview-link"
                        href={api.previewUrl(h, port)}
                        target="_blank"
                        rel="noopener"
                        title="Open full-size in a new tab"
                      >
                        <img class="stage-preview" src={api.previewUrl(h, port)} alt="{nid} preview" />
                      </a>
                    {/if}
                    {#if kind === 'crop'}
                      {@const fullDefaults = { ...nschema.defaults, ...nschema.template_params } as Record<string, unknown>}
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
                          defaults={{ ...nschema.defaults, ...nschema.template_params }}
                          {overrides}
                          cost={closureCost}
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
      <h2 class="section-h">History <span class="muted small">({project.history.length})</span></h2>
      <ol class="history-strip">
        {#each project.history as h (h.seq)}
          <li class="hist-entry" class:active={h.seq === project.current_seq}>
            <button type="button" onclick={() => revertTo(h.seq)} title={h.label ?? ''}>
              <span class="hist-seq muted">v{h.seq + 1}</span>
              <span class="hist-label">{shortHistoryLabel(h.label)}</span>
              <span class="hist-time muted small">{shortAgo(h.created_at)}</span>
            </button>
          </li>
        {/each}
      </ol>
    </section>
  {/if}
</div>

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
    flex: 1;
    font-size: 1.5rem;
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
    color: var(--accent, #7aa2ff);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .hbtn:hover:not(:disabled) {
    background: rgba(122, 162, 255, 0.1);
  }
  .hbtn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
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

  /* Expanded: jump out of the 4-up grid to a full-width row so the
     larger preview + params have horizontal room. dense flow above
     reflows the surrounding tiles to fill the gap. */
  .node-row.expanded {
    grid-column: 1 / -1;
    border-color: var(--border-strong);
  }

  /* Output card — accent halo so the eye lands on it. */
  .node-row.output {
    box-shadow: 0 0 0 1px var(--accent-soft);
  }
  .node-row.output.expanded {
    box-shadow: 0 0 0 1px var(--accent-soft), 0 0 24px rgba(94, 234, 212, 0.06);
  }
  /* Status-tinted left edge — quick scan of the pipeline state. */
  .node-row.node-running { border-left-color: var(--accent); }
  .node-row.node-completed { border-left-color: var(--good); }
  .node-row.node-cached { border-left-color: var(--fg-mute); }
  .node-row.node-failed { border-left-color: var(--bad); }

  /* Head = the clickable card surface. Collapsed: thumb fills, title
     bar overlays at top with a gradient. Expanded: thumb shrinks to
     a small left-side preview, title bar goes flat across the top. */
  .node-head {
    appearance: none;
    background: transparent;
    border: none;
    color: inherit;
    width: 100%;
    padding: 0;
    cursor: pointer;
    text-align: left;
    border-radius: 0;
    position: relative;
    display: block;
  }
  .node-row.expanded .node-head {
    display: grid;
    grid-template-columns: 64px 1fr;
    align-items: center;
    gap: 0.7rem;
    padding: 0.35rem 0.7rem 0.35rem 0.35rem;
  }

  .head-thumb {
    position: relative;
    aspect-ratio: 16 / 9;
    background: var(--bg-elev-2);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .node-row.expanded .head-thumb {
    width: 64px;
    border-radius: 5px;
    box-shadow: inset 0 0 0 1px var(--hairline);
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

  /* Overlay — collapsed = absolute top strip with gradient fade,
     expanded = inline second column with a flat row of meta. */
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
  .node-row.expanded .head-overlay {
    position: static;
    padding: 0;
    background: none;
    color: var(--fg);
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
  .node-row.expanded .head-name { text-shadow: none; }
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

  .node-body {
    padding: 0.85rem 1rem 1rem;
    border-top: 1px solid var(--hairline);
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }
  /* Expanded non-output: big preview left, params right. The 1.6fr/1fr
     split favors the preview because the user is staring at it while
     dragging sliders — the params side just needs to be readable. */
  .node-body.body-dual {
    display: grid;
    grid-template-columns: minmax(360px, 1.6fr) minmax(280px, 1fr);
    gap: 1.25rem;
    align-items: start;
  }
  .stage-preview-link {
    display: block;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 0 0 1px var(--hairline);
    transition: transform 160ms ease;
  }
  .stage-preview-link:hover { transform: scale(1.005); }
  .stage-preview {
    width: 100%;
    height: auto;
    max-height: 60vh;
    object-fit: contain;
    display: block;
    background: var(--bg);
  }
  .stage-params { min-width: 0; }
  @media (max-width: 720px) {
    .node-body.body-dual { grid-template-columns: 1fr; }
    .stage-preview { max-height: 50vh; }
  }

  /* Skeleton shimmer underlay reused for the head thumbnail. */
  @keyframes flow-skeleton-shimmer {
    0%   { background-position: -150% 0, 0 0; }
    100% { background-position: 250% 0, 0 0; }
  }
  .flow-skeleton {
    position: absolute;
    inset: 0;
    background:
      linear-gradient(110deg, transparent 30%, var(--accent-soft) 50%, transparent 70%),
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

  .path-line {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.5rem;
  }
  .path {
    font-family: ui-monospace, monospace;
    word-break: break-all;
  }
  .copy-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #444);
    border-radius: 6px;
    padding: 0.1rem 0.4rem;
    cursor: pointer;
    font-size: 0.85rem;
    line-height: 1;
    color: var(--fg, #ddd);
    opacity: 0.7;
  }
  .copy-btn:hover { opacity: 1; }
  .type-tag {
    color: var(--fg-mute, #888);
    font-size: 0.75rem;
  }
  .big-preview-link {
    display: inline-block;
    margin-top: 0.5rem;
  }
  .big-preview {
    max-width: 100%;
    max-height: 80vh;
    border: 1px solid var(--border, #333);
    border-radius: 6px;
  }

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

  .history-strip {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
    display: flex;
    gap: 0.4rem;
    overflow-x: auto;
    padding-bottom: 0.4rem;
  }
  .hist-entry button {
    appearance: none;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    border-radius: 6px;
    padding: 0.4rem 0.6rem;
    font: inherit;
    text-align: left;
    min-width: 14rem;
    max-width: 22rem;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    cursor: pointer;
  }
  .hist-entry button:hover {
    background: rgba(122, 162, 255, 0.06);
  }
  .hist-entry.active button {
    border-color: var(--accent, #7aa2ff);
    background: rgba(122, 162, 255, 0.12);
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
</style>
