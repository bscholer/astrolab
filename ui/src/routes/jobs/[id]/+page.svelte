<script lang="ts">
  import { onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { api, type JobEvent, type JobSummary } from '$lib/api';
  import { layoutTemplate, nodeDisplayName, type Layout } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatDuration, formatExposure, shortAgo } from '$lib/format';

  let job = $state<JobSummary | null>(null);
  let layout = $state<Layout | null>(null);
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let nodeHash = $state<Record<string, string>>({});
  let nodeKind = $state<Record<string, string>>({});
  let nodePort = $state<Record<string, string>>({});
  let recentEvents = $state<JobEvent[]>([]);
  let ws: WebSocket | null = null;
  // Dedupe events seen via the GET /events bootstrap vs WS replay.
  const seenEventKey = new Set<string>();

  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';

  const id = $derived($page.params.id ?? '');

  function eventKey(ev: JobEvent): string {
    // timestamp+type+node_id is unique enough; node_progress events with the
    // same fraction/message at the same timestamp are effectively idempotent.
    return `${ev.timestamp}|${ev.type}|${ev.node_id ?? ''}|${ev.fraction ?? ''}|${ev.message ?? ''}`;
  }

  function applyEvent(ev: JobEvent) {
    const key = eventKey(ev);
    if (seenEventKey.has(key)) return;
    seenEventKey.add(key);

    recentEvents = [...recentEvents.slice(-199), ev];
    if (ev.kind && ev.node_id) {
      nodeKind = { ...nodeKind, [ev.node_id]: ev.kind };
    }
    if (ev.hash && ev.node_id) {
      nodeHash = { ...nodeHash, [ev.node_id]: ev.hash };
    }
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
      // Refresh top-level info to capture outputs and final status.
      api.getJob(id)
        .then((j) => (job = j))
        .catch((err) => toast.error(`Couldn't refresh job: ${(err as Error).message}`));
    }
  }

  function pickPreviewPort(template: NonNullable<JobSummary['template']>, nodeId: string): string {
    // The first declared output port is the canonical one for previews.
    const node = template.nodes.find((n) => n.id === nodeId);
    if (!node) return 'image';
    // Each Node class has its outputs declared in Python; we don't know them
    // from the template alone. Conventions used by the basic nodes:
    if (node.kind === 'seq_stack' || node.kind === 'downscale') return 'image';
    if (
      node.kind === 'convert_lights' ||
      node.kind === 'calibrate' ||
      node.kind === 'seq_register'
    ) return 'sequence';
    return 'image';
  }

  function resetState() {
    job = null;
    layout = null;
    nodeStatus = {};
    nodeProgress = {};
    nodeHash = {};
    nodeKind = {};
    nodePort = {};
    recentEvents = [];
    seenEventKey.clear();
    ws?.close();
    ws = null;
  }

  async function loadJob(jobId: string) {
    try {
      const fresh = await api.getJob(jobId);
      // Bail if the route changed mid-fetch (user navigated again).
      if (jobId !== id) return;
      job = fresh;
      if (job.template) {
        layout = layoutTemplate(job.template);
        const initStatus: Record<string, NodeStatus> = {};
        const initKind: Record<string, string> = {};
        const initPort: Record<string, string> = {};
        for (const n of job.template.nodes) {
          initStatus[n.id] = 'pending';
          initKind[n.id] = n.kind;
          initPort[n.id] = pickPreviewPort(job.template, n.id);
        }
        nodeStatus = initStatus;
        nodeKind = initKind;
        nodePort = initPort;
      }
      // Bootstrap from buffered history so a reload after the WS closed still
      // paints the right state immediately.
      const history = await api.getJobEvents(jobId);
      if (jobId !== id) return;
      for (const ev of history) applyEvent(ev);

      // Only attach the WS while there's still work to do (or the job is so
      // fresh we might race the worker). The server closes the WS on terminal
      // events anyway, but skipping it on a finished job is a clean reload.
      if (job.status === 'queued' || job.status === 'running') {
        ws = api.subscribeJobEvents(jobId, applyEvent);
      }
    } catch (e) {
      toast.error(`Couldn't load job ${jobId}: ${(e as Error).message}`);
    }
  }

  // Reload whenever the route id changes (incl. after Reprocess goto). Without
  // this, navigating between jobs reuses the component instance and the old
  // state (events, node status, ws) lingers until you hit refresh.
  $effect(() => {
    if (!id) return;
    resetState();
    loadJob(id);
  });

  onDestroy(() => {
    ws?.close();
  });

  const finalOutput = $derived.by(() => {
    if (!job?.outputs) return null;
    const entries = Object.entries(job.outputs);
    return entries.length ? entries[0] : null;
  });

  let rerunning = $state(false);
  async function reprocess() {
    if (!job) return;
    rerunning = true;
    try {
      const r = await api.rerunJob(job.id);
      toast.info('Reprocessing — every node will run from scratch');
      goto(`/jobs/${r.job_id}`);
    } catch (e) {
      toast.error(`Couldn't reprocess: ${(e as Error).message}`);
    } finally {
      rerunning = false;
    }
  }
</script>

<div class="header">
  <a href="/jobs" class="back">← jobs</a>
  {#if job}
    <h1>
      {#if job.capture?.target_name}
        {job.capture.target_name}
      {:else}
        <span class="muted">job</span>
      {/if}
    </h1>
    <span class="status status-{job.status}">{job.status}</span>
    <button
      type="button"
      class="reprocess"
      onclick={reprocess}
      disabled={rerunning || job.status === 'queued' || job.status === 'running'}
      title={
        job.status === 'queued' || job.status === 'running'
          ? 'Wait until the current run finishes'
          : 'Re-run every step from scratch (bypasses the cache)'
      }
    >
      {rerunning ? 'Submitting…' : 'Reprocess'}
    </button>
  {/if}
</div>

{#if job === null}
  <p class="muted">Loading…</p>
{:else}
  <p class="capture-line muted small">
    {#if job.capture}
      <span>{formatExposure(job)}</span>
      <span aria-hidden="true">·</span>
    {/if}
    <span title={job.submitted_at}>{shortAgo(job.submitted_at)}</span>
    {#if job.started_at}
      <span aria-hidden="true">·</span>
      <span>{formatDuration(job.started_at, job.finished_at)}</span>
    {/if}
  </p>

  {#if layout}
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

  <section class="steps">
    <h2>Steps</h2>
    <div class="step-grid">
      {#each layout?.nodes ?? [] as n}
        {@const s = nodeStatus[n.id] ?? 'pending'}
        {@const p = nodeProgress[n.id]}
        {@const h = nodeHash[n.id]}
        {@const port = nodePort[n.id] ?? 'image'}
        <article class="step step-{s}">
          <header class="step-head">
            <span class="step-id">{nodeDisplayName(nodeKind[n.id] ?? n.kind)}</span>
            <span class="status status-mini status-{s}">{s}</span>
          </header>
          <div class="step-preview">
            {#if (s === 'completed' || s === 'cached') && h}
              <a href={api.previewUrl(h, port)} target="_blank" rel="noopener">
                <img
                  src={api.previewUrl(h, port)}
                  alt="preview of {n.id}"
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
          {#if h}
            <footer class="step-foot muted">
              <code class="hash">{h.slice(0, 12)}…</code>
            </footer>
          {/if}
        </article>
      {/each}
    </div>
  </section>

  {#if finalOutput}
    {@const name = finalOutput[0]}
    {@const ref = finalOutput[1]}
    <section class="final">
      <h2>Output: {name}</h2>
      <p class="muted small"><code>{ref.path}</code> <span>[{ref.type}]</span></p>
      <a class="big-preview-link" href={api.previewUrl(ref.node_hash, name)} target="_blank" rel="noopener">
        <img class="big-preview" src={api.previewUrl(ref.node_hash, name)} alt="output preview" />
      </a>
    </section>
  {/if}

  {#if job.error}
    <section class="error-box">
      <h2>Error</h2>
      <pre>{job.error}</pre>
    </section>
  {/if}

  <section class="events">
    <h2>Events <span class="muted small">({recentEvents.length})</span></h2>
    {#if recentEvents.length === 0}
      <p class="muted small">No events yet.</p>
    {:else}
      <ol>
        {#each recentEvents.slice().reverse() as ev}
          <li class="ev ev-{ev.type}">
            <span class="muted ts">{ev.timestamp.slice(11, 19)}</span>
            <span class="ev-type">{ev.type}</span>
            {#if ev.node_id}<code>{ev.node_id}</code>{/if}
            {#if ev.fraction !== undefined}
              <span class="muted small">{Math.round(ev.fraction * 100)}%</span>
            {/if}
            {#if ev.message}<span class="muted">{ev.message}</span>{/if}
            {#if ev.error}<span class="err">{ev.error}</span>{/if}
          </li>
        {/each}
      </ol>
    {/if}
  </section>
{/if}

<style>
  .header {
    display: flex;
    align-items: center;
    gap: 1rem;
    margin-bottom: 0.25rem;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }
  .back {
    color: var(--muted, #888);
    text-decoration: none;
  }
  .reprocess {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #444);
    color: var(--accent, #7aa2ff);
    padding: 0.3rem 0.8rem;
    border-radius: 999px;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .reprocess:hover:not(:disabled) {
    background: rgba(122, 162, 255, 0.1);
  }
  .reprocess:disabled {
    opacity: 0.5;
    cursor: not-allowed;
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
    color: var(--muted, #888);
  }
  .err {
    color: #f88;
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
  .node-pending {
    stroke: #555;
  }
  .node-running {
    stroke: #6cf;
    fill: #1a2538;
  }
  .node-cached {
    stroke: #777;
    fill: #1a1f1a;
  }
  .node-completed {
    stroke: #6c9;
    fill: #1a2521;
  }
  .node-failed {
    stroke: #f66;
    fill: #251818;
  }
  .node-progress {
    fill: #6cf;
    opacity: 0.7;
  }
  .node-name {
    font-size: 13px;
    font-weight: 600;
    text-anchor: middle;
    fill: #ddd;
  }
  .edge {
    stroke: #555;
    stroke-width: 1.5;
    fill: none;
  }
  .edge-running {
    stroke: #6cf;
  }
  .edge-cached {
    stroke: #777;
  }
  .edge-completed {
    stroke: #6c9;
  }
  .edge-failed {
    stroke: #f66;
  }

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
  .status-pending {
    background: #2a2a2a;
    color: #aaa;
  }
  .status-queued {
    background: #444;
    color: #ccc;
  }
  .status-running {
    background: #234;
    color: #6cf;
  }
  .status-cached {
    background: #2a2a2a;
    color: #aaa;
  }
  .status-completed {
    background: #243;
    color: #6c9;
  }
  .status-failed {
    background: #422;
    color: #f88;
  }

  .step-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
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
    gap: 0.4rem;
  }
  .step-completed {
    border-color: #2c5;
  }
  .step-cached {
    border-color: #555;
  }
  .step-running {
    border-color: #6cf;
  }
  .step-failed {
    border-color: #f66;
  }
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
  .step-foot {
    font-size: 0.7rem;
  }
  .hash {
    font-family: ui-monospace, monospace;
  }

  .final {
    margin-top: 1.5rem;
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

  .events ol {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
    font-size: 0.85rem;
    max-height: 400px;
    overflow-y: auto;
  }
  .ev {
    padding: 0.2rem 0;
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }
  .ts {
    font-variant-numeric: tabular-nums;
    min-width: 5em;
  }
  .ev-type {
    font-family: ui-monospace, monospace;
    font-size: 0.75rem;
    color: #aaa;
    min-width: 14em;
  }
  .ev-job_failed .ev-type,
  .ev-node_failed .ev-type {
    color: #f88;
  }
  .ev-job_completed .ev-type,
  .ev-node_completed .ev-type {
    color: #6c9;
  }
</style>
