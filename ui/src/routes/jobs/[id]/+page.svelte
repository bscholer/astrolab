<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { page } from '$app/stores';
  import { api, type JobEvent, type JobSummary } from '$lib/api';
  import { layoutTemplate, type Layout } from '$lib/graph';

  let job = $state<JobSummary | null>(null);
  let layout = $state<Layout | null>(null);
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let recentEvents = $state<JobEvent[]>([]);
  let error = $state<string | null>(null);
  let ws: WebSocket | null = null;

  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';

  const id = $derived($page.params.id ?? '');

  function applyEvent(ev: JobEvent) {
    recentEvents = [...recentEvents.slice(-99), ev];
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
        .catch((err) => (error = (err as Error).message));
    }
  }

  onMount(async () => {
    try {
      job = await api.getJob(id);
      if (job.template) {
        layout = layoutTemplate(job.template);
        // Initialize all nodes as pending so the graph paints immediately.
        const init: Record<string, NodeStatus> = {};
        for (const n of job.template.nodes) init[n.id] = 'pending';
        nodeStatus = init;
      }
      // Open WebSocket for live events. The server replays history first,
      // so we don't need to separately fetch /events.
      ws = api.subscribeJobEvents(id, applyEvent);
    } catch (e) {
      error = (e as Error).message;
    }
  });

  onDestroy(() => {
    ws?.close();
  });
</script>

<div class="header">
  <a href="/jobs" class="back">← jobs</a>
  {#if job}
    <h1>
      <code>{job.template_id}</code>
      <span class="muted v">v{job.template_version}</span>
    </h1>
    <span class="status status-{job.status}">{job.status}</span>
  {/if}
</div>

{#if error}
  <p class="err">Error: {error}</p>
{:else if job === null}
  <p class="muted">Loading…</p>
{:else}
  <p class="muted small">job <code>{job.id}</code></p>

  {#if layout}
    <section class="graph">
      <svg viewBox="0 0 {layout.width} {layout.height}" width={layout.width} height={layout.height}>
        <!-- edges first so nodes draw on top -->
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
            <text x={n.width / 2} y="22" class="node-id">{n.id}</text>
            <text x={n.width / 2} y="40" class="node-kind">{n.kind}</text>
            {#if s === 'running' && p}
              <rect x="6" y={n.height - 10} width={(n.width - 12) * (p.fraction || 0)} height="4" rx="2" class="node-progress" />
            {/if}
          </g>
        {/each}
      </svg>
    </section>
  {/if}

  {#if job.outputs}
    <section>
      <h2>Outputs</h2>
      <ul class="outputs">
        {#each Object.entries(job.outputs) as [name, ref]}
          <li>
            <strong>{name}</strong>
            <span class="muted">[{ref.type}]</span>
            <code>{ref.path}</code>
          </li>
        {/each}
      </ul>
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
  }
  .v {
    font-size: 0.7em;
    margin-left: 0.5rem;
  }
  .back {
    color: var(--muted, #888);
    text-decoration: none;
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
    margin: 1.5rem 0;
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

  .node-progress {
    fill: #6cf;
    opacity: 0.7;
  }

  .node-id {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    text-anchor: middle;
    fill: #ddd;
  }
  .node-kind {
    font-size: 10px;
    text-anchor: middle;
    fill: #888;
  }

  .edge {
    stroke: #555;
    stroke-width: 1.5;
    fill: none;
  }
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
  .status-queued { background: #444; color: #ccc; }
  .status-running { background: #234; color: #6cf; }
  .status-completed { background: #243; color: #6c9; }
  .status-failed { background: #422; color: #f88; }

  .outputs li {
    font-size: 0.85rem;
    margin-bottom: 0.25rem;
  }
  .outputs code {
    font-size: 0.8rem;
    margin-left: 0.5rem;
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
