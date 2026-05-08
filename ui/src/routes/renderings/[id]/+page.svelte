<!--
  Rendering detail page: a live, editable view of one stack pipeline.

  Differences from /jobs/[id]:
    - Each step card carries an inline parameter form (auto-built from the
      template's JSON Schema). Any change debounces and PATCHes the rendering;
      the server submits a fresh job, the cache makes upstream nodes free.
    - A history strip at the bottom shows past states with their auto-
      generated diff labels; click to revert (just moves the pointer, no new
      job runs because the past entry's job is already cached).
    - "Reprocess" lives here as a `force=true` PATCH that appends to history
      rather than spawning an orphan job.

  This page subscribes to events for the *active* job (current_seq's job_id).
  When a PATCH lands, the active job changes and we tear down the old
  subscription and reattach.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import {
    api,
    type CostClass,
    type JobEvent,
    type JobSummary,
    type Rendering,
    type TemplateSchema
  } from '$lib/api';
  import {
    blastRadiusCost,
    layoutTemplate,
    nodeDisplayName,
    type Layout
  } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatDuration, shortAgo } from '$lib/format';
  import NodeParamsForm from '$lib/NodeParamsForm.svelte';

  let rendering = $state<Rendering | null>(null);
  let schema = $state<TemplateSchema | null>(null);
  let activeJob = $state<JobSummary | null>(null);
  let layout = $state<Layout | null>(null);

  // Step state, keyed by node_id. Mirrors the jobs page so the per-step
  // status/preview/progress UX stays consistent.
  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let nodeHash = $state<Record<string, string>>({});
  let nodeKind = $state<Record<string, string>>({});
  let nodePort = $state<Record<string, string>>({});

  let ws: WebSocket | null = null;
  const seenEventKey = new Set<string>();
  // Tracks which job_id the WS is currently subscribed to so we can teardown
  // cleanly when a PATCH switches the active job.
  let subscribedJobId: string | null = null;

  // Debounce timer for slider/input changes; coalesces rapid edits into a
  // single PATCH so we don't queue 60 jobs while the user drags.
  const PATCH_DEBOUNCE_MS = 350;
  let patchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingOverrides: Record<string, Record<string, unknown> | null> | null = null;
  let patching = $state(false);
  let reprocessing = $state(false);

  const id = $derived($page.params.id ?? '');

  // Per-node cost map (cheap/medium/expensive) lifted from the schema.
  const costByNode = $derived.by(() => {
    if (!schema) return {} as Record<string, CostClass>;
    const out: Record<string, CostClass> = {};
    for (const n of schema.nodes) out[n.node_id] = n.cost;
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
      switch (ev.type) {
        case 'node_started':
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'running' };
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
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'completed' };
          break;
        case 'node_failed':
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'failed' };
          break;
      }
    }
    if (ev.type === 'job_completed' || ev.type === 'job_failed') {
      // Refresh the active job so we get final status + outputs (for the
      // big preview at the bottom, etc.).
      api.getJob(activeJob?.id ?? '')
        .then((j) => (activeJob = j))
        .catch(() => undefined);
    }
  }

  function pickPreviewPort(kind: string): string {
    if (
      kind === 'convert_lights' ||
      kind === 'calibrate' ||
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
    seenEventKey.clear();
  }

  async function attachToJob(jobId: string) {
    if (subscribedJobId === jobId) return;
    detachFromJob();
    subscribedJobId = jobId;
    try {
      const fresh = await api.getJob(jobId);
      if (subscribedJobId !== jobId) return; // route/job changed mid-fetch
      activeJob = fresh;
      // Bootstrap from buffered history.
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

  async function loadRendering(rid: string) {
    try {
      const fresh = await api.getRendering(rid);
      if (rid !== id) return;
      rendering = fresh;
      schema = await api.getTemplateSchema(fresh.template_id);
      if (rid !== id) return;
      layout = layoutTemplate(fresh.template);
      const initKind: Record<string, string> = {};
      const initPort: Record<string, string> = {};
      for (const n of fresh.template.nodes) {
        initKind[n.id] = n.kind;
        initPort[n.id] = pickPreviewPort(n.kind);
      }
      nodeKind = initKind;
      nodePort = initPort;
      // Initial status = pending; events from the active job will update.
      nodeStatus = Object.fromEntries(
        fresh.template.nodes.map((n) => [n.id, 'pending'])
      );
      await attachToJob(fresh.current_job_id);
    } catch (e) {
      toast.error(`Couldn't load rendering ${rid}: ${(e as Error).message}`);
    }
  }

  // Reload whenever the route id changes.
  $effect(() => {
    if (!id) return;
    rendering = null;
    schema = null;
    layout = null;
    resetPipelineState();
    detachFromJob();
    loadRendering(id);
  });

  onDestroy(() => {
    if (patchTimer) clearTimeout(patchTimer);
    detachFromJob();
  });

  // -----------------------------------------------------------------
  // Edits: debounced auto-rerun
  // -----------------------------------------------------------------

  /**
   * Called by NodeParamsForm whenever any control fires. `partialForNode`
   * is the FULL override dict for that node (after this change), not just
   * the diff. We accumulate per-node overrides in `pendingOverrides` and
   * dispatch a single PATCH on the trailing edge of the debounce window.
   */
  function onNodeOverrideChange(
    nodeId: string,
    partialForNode: Record<string, unknown>
  ) {
    if (!pendingOverrides) pendingOverrides = {};
    // Empty object means "no overrides for this node" -> we send null so the
    // server drops it (rather than leaving an empty {} hanging around in
    // history forever).
    pendingOverrides[nodeId] =
      Object.keys(partialForNode).length === 0 ? null : partialForNode;

    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(flushPatch, PATCH_DEBOUNCE_MS);
  }

  async function flushPatch() {
    patchTimer = null;
    if (!rendering || !pendingOverrides) return;
    const overrides = pendingOverrides;
    pendingOverrides = null;
    patching = true;
    try {
      const next = await api.patchRendering(rendering.id, { overrides });
      onRenderingUpdated(next);
    } catch (e) {
      toast.error(`Couldn't apply changes: ${(e as Error).message}`);
    } finally {
      patching = false;
    }
  }

  function onRenderingUpdated(next: Rendering) {
    rendering = next;
    // The active job changed; rebind WS + clear stale per-step state.
    resetPipelineState();
    if (layout) {
      nodeStatus = Object.fromEntries(
        layout.nodes.map((n) => [n.id, 'pending'] as const)
      );
    }
    attachToJob(next.current_job_id);
  }

  async function reprocess() {
    if (!rendering) return;
    reprocessing = true;
    try {
      const next = await api.patchRendering(rendering.id, { force: true });
      toast.info('Reprocessing — every step runs from scratch');
      onRenderingUpdated(next);
    } catch (e) {
      toast.error(`Couldn't reprocess: ${(e as Error).message}`);
    } finally {
      reprocessing = false;
    }
  }

  async function revertTo(seq: number) {
    if (!rendering) return;
    try {
      const next = await api.revertRendering(rendering.id, seq);
      onRenderingUpdated(next);
    } catch (e) {
      toast.error(`Couldn't revert: ${(e as Error).message}`);
    }
  }

  // -----------------------------------------------------------------
  // Undo / redo: keyboard shortcuts move the pointer linearly.
  // -----------------------------------------------------------------

  function onKeydown(e: KeyboardEvent) {
    if (!rendering) return;
    const meta = e.metaKey || e.ctrlKey;
    if (!meta) return;
    if (e.key === 'z' && !e.shiftKey) {
      e.preventDefault();
      const prev = rendering.current_seq - 1;
      if (prev >= 0 && rendering.history.some((h) => h.seq === prev)) {
        revertTo(prev);
      }
    } else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') {
      e.preventDefault();
      const next = rendering.current_seq + 1;
      if (rendering.history.some((h) => h.seq === next)) {
        revertTo(next);
      }
    }
  }

  // -----------------------------------------------------------------
  // Derived UI bits.
  // -----------------------------------------------------------------

  const finalOutput = $derived.by(() => {
    if (!activeJob?.outputs) return null;
    // Prefer the 'image' port (final PNG from save_image); fall back to the
    // first declared output if the template names it differently.
    const entries = Object.entries(activeJob.outputs);
    const named = entries.find(([k]) => k === 'image');
    return named ?? (entries[0] ?? null);
  });

  const undoDisabled = $derived(
    !rendering || !rendering.history.some((h) => h.seq === (rendering!.current_seq - 1))
  );
  const redoDisabled = $derived(
    !rendering || !rendering.history.some((h) => h.seq === (rendering!.current_seq + 1))
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
</script>

<svelte:window onkeydown={onKeydown} />

<div class="header">
  <a href="/" class="back">← library</a>
  {#if rendering}
    <h1>{rendering.name}</h1>
    <span class="version muted small">
      v{rendering.current_seq + 1} of {rendering.history.length}
    </span>
    <button
      type="button"
      class="hbtn"
      onclick={() => revertTo(rendering!.current_seq - 1)}
      disabled={undoDisabled}
      title="Undo (Cmd-Z)"
    >↶ Undo</button>
    <button
      type="button"
      class="hbtn"
      onclick={() => revertTo(rendering!.current_seq + 1)}
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

{#if rendering === null}
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
    <span title={rendering.created_at}>created {shortAgo(rendering.created_at)}</span>
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

  {#if layout && schema}
    <section class="graph">
      <svg viewBox="0 0 {layout.width} {layout.height}" width={layout.width} height={layout.height}>
        {#each layout.edges as e}
          <line
            x1={e.x1}
            y1={e.y1}
            x2={e.x2}
            y2={e.y2}
            class="edge edge-{nodeStatus[e.fromId] ?? 'pending'}"
          />
        {/each}
        {#each layout.nodes as n}
          {@const s = nodeStatus[n.id] ?? 'pending'}
          {@const p = nodeProgress[n.id]}
          <g transform="translate({n.x}, {n.y})">
            <rect width={n.width} height={n.height} rx="6" class="node node-{s}" />
            <text x={n.width / 2} y={n.height / 2 + 5} class="node-name">
              {nodeDisplayName(nodeKind[n.id] ?? n.kind)}
            </text>
            {#if s === 'running' && p}
              <rect
                x="6"
                y={n.height - 10}
                width={(n.width - 12) * (p.fraction || 0)}
                height="4"
                rx="2"
                class="node-progress"
              />
            {/if}
          </g>
        {/each}
      </svg>
    </section>
  {/if}

  {#if schema}
    <section class="steps">
      <h2>Steps</h2>
      <div class="step-grid">
        {#each schema.nodes as nschema (nschema.node_id)}
          {@const nid = nschema.node_id}
          {@const s = nodeStatus[nid] ?? 'pending'}
          {@const p = nodeProgress[nid]}
          {@const h = nodeHash[nid]}
          {@const port = nodePort[nid] ?? 'image'}
          {@const closureCost = blastRadiusCost(rendering.template, nid, costByNode)}
          {@const overrides = (rendering.current_overrides[nid] as Record<string, unknown>) ?? {}}
          <article class="step step-{s}">
            <header class="step-head">
              <span class="step-id">{nodeDisplayName(nschema.kind)}</span>
              <span class="status status-mini status-{s}">{s}</span>
            </header>
            <div class="step-preview">
              {#if (s === 'completed' || s === 'cached') && h}
                <a href={api.previewUrl(h, port)} target="_blank" rel="noopener">
                  <img
                    src={api.previewUrl(h, port)}
                    alt="preview of {nid}"
                    loading="lazy"
                    onerror={(e) => ((e.currentTarget as HTMLImageElement).style.display = 'none')}
                  />
                </a>
              {:else if s === 'running' && p}
                <div class="run-msg">
                  <span class="pct">{Math.round((p.fraction ?? 0) * 100)}%</span>
                  <span class="muted small">{p.message}</span>
                </div>
              {:else if s === 'failed'}
                <div class="run-msg err">failed</div>
              {:else}
                <div class="run-msg muted">waiting</div>
              {/if}
            </div>

            {#if Object.keys(nschema.schema.properties ?? {}).length > 0}
              <details class="param-block" open={Object.keys(overrides).length > 0}>
                <summary>
                  <span>Parameters</span>
                  {#if Object.keys(overrides).length > 0}
                    <span class="badge-modified">{Object.keys(overrides).length} modified</span>
                  {/if}
                </summary>
                <NodeParamsForm
                  nodeId={nid}
                  schemaProps={nschema.schema.properties ?? {}}
                  defaults={{ ...nschema.defaults, ...nschema.template_params }}
                  {overrides}
                  cost={closureCost}
                  onchange={(next) => onNodeOverrideChange(nid, next)}
                />
              </details>
            {/if}
          </article>
        {/each}
      </div>
    </section>
  {/if}

  {#if finalOutput}
    {@const name = finalOutput[0]}
    {@const ref = finalOutput[1]}
    <section class="final">
      <h2>Output: {name}</h2>
      <p class="path-line muted small">
        <code class="path">{ref.path}</code>
        <button
          type="button"
          class="copy-btn"
          title="Copy path"
          onclick={() => copyToClipboard(ref.path, 'Copied output path')}
        >📋</button>
        <span class="type-tag">[{ref.type}]</span>
      </p>
      <a class="big-preview-link" href={api.previewUrl(ref.node_hash, name)} target="_blank" rel="noopener">
        <img class="big-preview" src={api.previewUrl(ref.node_hash, name)} alt="output preview" />
      </a>
    </section>
  {/if}

  {#if activeJob?.error}
    <section class="error-box">
      <h2>Error</h2>
      <pre>{activeJob.error}</pre>
    </section>
  {/if}

  <section class="history">
    <h2>History <span class="muted small">({rendering.history.length})</span></h2>
    <ol class="history-strip">
      {#each rendering.history as h (h.seq)}
        <li class="hist-entry" class:active={h.seq === rendering.current_seq}>
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

<style>
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

  .graph {
    margin: 1rem 0;
    overflow-x: auto;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 0.5rem;
  }
  .node {
    fill: #1a1d24;
    stroke: #444;
    stroke-width: 1.5;
    transition: stroke 0.2s, fill 0.2s;
  }
  .node-pending { stroke: #555; }
  .node-running { stroke: #6cf; fill: #1a2538; }
  .node-cached { stroke: #777; fill: #1a1f1a; }
  .node-completed { stroke: #6c9; fill: #1a2521; }
  .node-failed { stroke: #f66; fill: #251818; }
  .node-progress { fill: #6cf; opacity: 0.7; }
  .node-name { font-size: 13px; font-weight: 600; text-anchor: middle; fill: #ddd; }
  .edge { stroke: #555; stroke-width: 1.5; fill: none; }
  .edge-running { stroke: #6cf; }
  .edge-cached { stroke: #777; }
  .edge-completed { stroke: #6c9; }
  .edge-failed { stroke: #f66; }

  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-mini {
    font-size: 0.65rem;
    padding: 0.05rem 0.4rem;
    margin-left: auto;
  }
  .status-pending { background: #2a2a2a; color: #aaa; }
  .status-queued { background: #444; color: #ccc; }
  .status-running { background: #234; color: #6cf; }
  .status-cached { background: #2a2a2a; color: #aaa; }
  .status-completed { background: #243; color: #6c9; }
  .status-failed { background: #422; color: #f88; }

  .step-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 0.75rem;
    margin-top: 0.5rem;
  }
  .step {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .step-completed { border-color: #2c5; }
  .step-cached { border-color: #555; }
  .step-running { border-color: #6cf; }
  .step-failed { border-color: #f66; }
  .step-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .step-id {
    font-family: ui-monospace, monospace;
    font-weight: 600;
  }
  .step-preview {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 120px;
    background: #0a0c10;
    border-radius: 4px;
    overflow: hidden;
  }
  .step-preview img {
    max-width: 100%;
    max-height: 220px;
    display: block;
  }
  .run-msg {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem;
    text-align: center;
    font-size: 0.85rem;
  }
  .pct {
    font-size: 1.2rem;
    color: #6cf;
    font-variant-numeric: tabular-nums;
  }

  .param-block {
    border-top: 1px dashed rgba(255, 255, 255, 0.05);
    padding-top: 0.4rem;
  }
  .param-block summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8rem;
    user-select: none;
  }
  .param-block summary::-webkit-details-marker { display: none; }
  .badge-modified {
    background: rgba(122, 162, 255, 0.18);
    color: var(--accent, #7aa2ff);
    border: 1px solid var(--accent, #7aa2ff);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .final {
    margin-top: 1.5rem;
  }
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
    max-height: 600px;
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

  .history {
    margin-top: 1.5rem;
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
