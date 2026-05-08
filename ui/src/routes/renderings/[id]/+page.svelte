<!--
  Rendering detail page: a live, editable view of one stack pipeline.

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

  Edits debounce 350ms then PATCH the rendering. The active job is the
  current_seq's job_id; we tear down/rebind the WS subscription whenever
  the active job changes.
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
    nodeDisplayName
  } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatDuration, shortAgo } from '$lib/format';
  import NodeParamsForm from '$lib/NodeParamsForm.svelte';

  let rendering = $state<Rendering | null>(null);
  let schema = $state<TemplateSchema | null>(null);
  let activeJob = $state<JobSummary | null>(null);

  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let nodeHash = $state<Record<string, string>>({});
  let nodeKind = $state<Record<string, string>>({});
  let nodePort = $state<Record<string, string>>({});

  let ws: WebSocket | null = null;
  const seenEventKey = new Set<string>();
  let subscribedJobId: string | null = null;

  const PATCH_DEBOUNCE_MS = 350;
  let patchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingOverrides: Record<string, Record<string, unknown> | null> | null = null;
  let patching = $state(false);
  let reprocessing = $state(false);

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
    seenEventKey.clear();
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

  async function loadRendering(rid: string) {
    try {
      const fresh = await api.getRendering(rid);
      if (rid !== id) return;
      rendering = fresh;
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
      toast.error(`Couldn't load rendering ${rid}: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    if (!id) return;
    rendering = null;
    schema = null;
    resetPipelineState();
    detachFromJob();
    loadRendering(id);
  });

  // While this page is mounted, widen the global container so the pipeline
  // strip + params grid actually use ultrawide real estate. Removed on
  // unmount so other pages stay at the comfortable reading width.
  $effect(() => {
    document.body.classList.add('rendering-page');
    return () => document.body.classList.remove('rendering-page');
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

  const finalOutput = $derived.by(() => {
    if (!activeJob?.outputs) return null;
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

<div class="rendering-root">
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

    <!-- Pipeline strip: live preview per node, title + status overlaid. -->
    {#if schema}
      <section class="pipeline">
        <h2 class="section-h">Pipeline</h2>
        <ol class="flow-strip">
          {#each orderedNodeIds as nid (nid)}
            {@const s = nodeStatus[nid] ?? 'pending'}
            {@const p = nodeProgress[nid]}
            {@const h = nodeHash[nid]}
            {@const port = nodePort[nid] ?? 'image'}
            {@const kind = nodeKind[nid] ?? schema.nodes.find((n) => n.node_id === nid)?.kind ?? nid}
            <li class="flow-card flow-{s}">
              {#if (s === 'completed' || s === 'cached') && h}
                <img
                  class="flow-preview"
                  src={api.previewUrl(h, port)}
                  alt="{nid} preview"
                  loading="lazy"
                  onerror={(e) => ((e.currentTarget as HTMLImageElement).style.opacity = '0')}
                />
              {:else}
                <div class="flow-preview placeholder">
                  {#if s === 'running' && p}
                    <span class="pct">{Math.round((p.fraction ?? 0) * 100)}%</span>
                  {:else if s === 'failed'}
                    <span class="err">failed</span>
                  {:else}
                    <span class="muted small">{s}</span>
                  {/if}
                </div>
              {/if}
              <div class="flow-overlay">
                <span class="flow-title">{nodeDisplayName(kind)}</span>
                <span class="status status-mini status-{s}">{s}</span>
              </div>
              {#if s === 'running' && p}
                <div class="flow-progress" style:width="{(p.fraction ?? 0) * 100}%"></div>
              {/if}
            </li>
          {/each}
        </ol>
      </section>
    {/if}

    <!-- Parameters grid: one card per node, form always visible. -->
    {#if schema && rendering}
      <section class="params-section">
        <h2 class="section-h">Parameters</h2>
        <div class="params-grid">
          {#each schema.nodes as nschema (nschema.node_id)}
            {@const nid = nschema.node_id}
            {@const closureCost = blastRadiusCost(rendering.template, nid, costByNode)}
            {@const overrides = (rendering.current_overrides[nid] as Record<string, unknown>) ?? {}}
            {@const props = nschema.schema.properties ?? {}}
            <article class="param-card">
              <header class="param-card-head">
                <span class="param-card-title">{nodeDisplayName(nschema.kind)}</span>
                {#if Object.keys(overrides).length > 0}
                  <span class="badge-modified">{Object.keys(overrides).length} modified</span>
                {/if}
              </header>
              {#if Object.keys(props).length === 0}
                <p class="muted small no-params">No editable parameters.</p>
              {:else}
                <NodeParamsForm
                  nodeId={nid}
                  schemaProps={props}
                  defaults={{ ...nschema.defaults, ...nschema.template_params }}
                  {overrides}
                  cost={closureCost}
                  onchange={(next) => onNodeOverrideChange(nid, next)}
                />
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
        <h2 class="section-h">Output: {name}</h2>
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
        <h2 class="section-h">Error</h2>
        <pre>{activeJob.error}</pre>
      </section>
    {/if}

    <section class="history">
      <h2 class="section-h">History <span class="muted small">({rendering.history.length})</span></h2>
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
</div>

<style>
  .rendering-root {
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

  /* ---------- Pipeline strip ---------- */

  .flow-strip {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    /* Auto-fill at min 220px so on ultrawide we get all 8 nodes in a row;
       on narrow displays they wrap to two/three rows gracefully. */
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.6rem;
  }
  .flow-card {
    position: relative;
    aspect-ratio: 16 / 9;
    background: #0a0c10;
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    overflow: hidden;
    transition: border-color 120ms ease, transform 120ms ease;
  }
  .flow-card.flow-running {
    border-color: #6cf;
    box-shadow: 0 0 0 1px rgba(108, 204, 255, 0.4);
  }
  .flow-card.flow-completed { border-color: #6c9; }
  .flow-card.flow-cached { border-color: #777; }
  .flow-card.flow-failed { border-color: var(--bad, #f88); }
  .flow-preview {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: opacity 120ms ease;
  }
  .flow-preview.placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
  }
  .flow-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    padding: 0.4rem 0.6rem 0.6rem;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.7), rgba(0, 0, 0, 0));
    color: #fff;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    pointer-events: none;
  }
  .flow-title {
    font-weight: 600;
    font-size: 0.95rem;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.6);
  }
  .flow-progress {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 3px;
    background: #6cf;
    transition: width 200ms ease;
  }
  .pct {
    font-size: 1.3rem;
    color: #6cf;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  /* ---------- Status pills ---------- */

  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-left: auto;
  }
  .status-mini {
    font-size: 0.6rem;
    padding: 0.05rem 0.4rem;
  }
  .status-pending { background: #2a2a2a; color: #aaa; }
  .status-queued { background: #444; color: #ccc; }
  .status-running { background: #234; color: #6cf; }
  .status-cached { background: rgba(255, 255, 255, 0.12); color: #ccc; }
  .status-completed { background: rgba(108, 204, 153, 0.25); color: #6c9; }
  .status-failed { background: rgba(255, 122, 138, 0.25); color: var(--bad, #f88); }

  /* ---------- Parameters grid ---------- */

  .params-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 0.75rem;
  }
  .param-card {
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    padding: 0.6rem 0.7rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .param-card-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    padding-bottom: 0.35rem;
  }
  .param-card-title {
    font-weight: 600;
  }
  .badge-modified {
    background: rgba(122, 162, 255, 0.18);
    color: var(--accent, #7aa2ff);
    border: 1px solid var(--accent, #7aa2ff);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-left: auto;
  }
  .no-params {
    margin: 0.2rem 0;
  }

  /* ---------- Output ---------- */

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
