<!--
  Custom xyflow node renderer for the pipeline graph view.

  Visually mirrors the card-mode head (preview thumb, name, status
  badge, on/off toggle) at a fixed compact size so the graph layout
  doesn't reflow on status changes. Click anywhere outside the toggle
  selects the node; the graph view's parent renders the param form for
  the selected node in a side panel rather than inline (the inline
  expand from card mode doesn't translate to a fixed-bounds graph
  node).
-->
<script lang="ts">
  import { Handle, Position, type NodeProps } from '@xyflow/svelte';
  import { nodeDisplayName } from './graph';

  type GraphNodeData = {
    nid: string;
    kind: string;
    status: 'pending' | 'running' | 'cached' | 'completed' | 'failed';
    enabled: boolean;
    togglable: boolean;
    isOutput: boolean;
    isSelected: boolean;
    progressFraction: number | null;
    progressMessage: string | null;
    durationMs: number | null;
    previewSrc: string | null;
    previewLoaded: boolean;
    inputPorts: string[];
    outputPorts: string[];
    onToggle: () => void;
    onPreviewLoad: () => void;
    onPreviewError: () => void;
  };

  type Props = NodeProps & { data: GraphNodeData };

  let { data }: Props = $props();

  function statusLabel(): string {
    if (data.togglable && !data.enabled) return 'off';
    if (data.status === 'running' && data.progressFraction !== null) {
      return `${Math.round(data.progressFraction * 100)}%`;
    }
    return data.status;
  }

  function durationString(ms: number | null): string {
    if (ms === null || ms === undefined) return '';
    if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
    const m = Math.floor(ms / 60_000);
    const s = Math.round((ms % 60_000) / 1000);
    return `${m}m ${s}s`;
  }
</script>

<div
  class="gnode status-{data.status}"
  class:disabled={data.togglable && !data.enabled}
  class:output={data.isOutput}
  class:selected={data.isSelected}
>
  <!-- Input handles on the left, output handles on the right. xyflow
       routes edges between matching ids. We only need source handles
       for the OUTPUT ports here; input handles render as visual
       targets at the left edge. -->
  {#each data.inputPorts as port, i (port)}
    <Handle
      type="target"
      position={Position.Left}
      id={port}
      style="top: {((i + 1) / (data.inputPorts.length + 1)) * 100}%;"
    />
  {/each}
  {#each data.outputPorts as port, i (port)}
    <Handle
      type="source"
      position={Position.Right}
      id={port}
      style="top: {((i + 1) / (data.outputPorts.length + 1)) * 100}%;"
    />
  {/each}

  <div class="gnode-thumb">
    {#if (data.status === 'completed' || data.status === 'cached') && data.previewSrc}
      {#if !data.previewLoaded}
        <div class="gnode-skeleton" aria-hidden="true"></div>
      {/if}
      <img
        class="gnode-img"
        class:loaded={data.previewLoaded}
        src={data.previewSrc}
        alt=""
        draggable="false"
        onload={data.onPreviewLoad}
        onerror={data.onPreviewError}
      />
    {:else if data.status === 'running' && data.progressFraction !== null}
      <span class="gnode-pct">{Math.round(data.progressFraction * 100)}%</span>
    {:else}
      <span class="gnode-status muted">{data.status}</span>
    {/if}
    {#if data.status === 'running' && data.progressFraction !== null}
      <div class="gnode-progress" style:width="{data.progressFraction * 100}%"></div>
    {/if}
  </div>

  <div class="gnode-overlay">
    <span class="gnode-name">{nodeDisplayName(data.kind, data.nid)}</span>
    <span class="status status-mini status-{data.togglable && !data.enabled ? 'off' : data.status}">
      {statusLabel()}{#if (data.status === 'completed' || data.status === 'failed') && data.durationMs && data.enabled}<span class="dur"> · {durationString(data.durationMs)}</span>{/if}
    </span>
    {#if data.isOutput}
      <span class="output-tag">final</span>
    {/if}
    <span class="gnode-spacer"></span>
    {#if data.togglable}
      <!-- Toggle has pointer-events: auto so it captures its own clicks
           even though the overlay is pointer-events: none for click-pass-
           through (matches the card-mode pattern). -->
      <button
        type="button"
        class="gnode-toggle"
        class:on={data.enabled}
        role="switch"
        aria-checked={data.enabled}
        aria-label={data.enabled ? 'Disable step' : 'Enable step'}
        title={data.enabled ? 'On - click to skip' : 'Off - click to run'}
        onclick={(e) => {
          e.stopPropagation();
          data.onToggle();
        }}
      >
        <span class="gnode-toggle-knob"></span>
      </button>
    {/if}
  </div>
</div>

<style>
  .gnode {
    position: relative;
    width: 220px;
    height: 124px;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
  }
  .gnode:hover { border-color: var(--border-strong); }
  .gnode.selected {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent), 0 6px 24px rgba(0, 0, 0, 0.4);
  }
  .gnode.output {
    border-color: var(--accent-soft);
  }
  .gnode.output.selected,
  .gnode.output {
    box-shadow: 0 0 0 1px var(--accent-soft);
  }

  .gnode-thumb {
    position: relative;
    width: 100%;
    height: 100%;
    background: var(--bg-elev-2);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    transition: opacity 220ms ease, filter 220ms ease;
  }
  .gnode-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    opacity: 0;
    transition: opacity 220ms ease;
  }
  .gnode-img.loaded { opacity: 1; }
  .gnode-skeleton {
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg,
      rgba(255, 255, 255, 0.02) 0%,
      rgba(255, 255, 255, 0.06) 50%,
      rgba(255, 255, 255, 0.02) 100%);
    background-size: 200% 100%;
    animation: shimmer 1.2s infinite linear;
  }
  @keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  .gnode-pct {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--accent);
    font-size: 1.05rem;
  }
  .gnode-status {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .gnode-progress {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 2px;
    background: var(--accent);
    transition: width 200ms ease;
    z-index: 2;
  }

  .gnode-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    padding: 0.4rem 0.55rem 0.7rem;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.85) 30%, rgba(0, 0, 0, 0));
    color: #fff;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    pointer-events: none;
    z-index: 1;
  }
  .gnode-name {
    font-weight: 600;
    font-size: 0.8rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.6);
    min-width: 0;
    flex-shrink: 1;
  }
  .gnode-spacer { flex: 1 1 0; min-width: 0.2rem; }

  /* Status pill - reuses the .status-* color tokens from the card view
     so the two modes feel consistent. */
  :global(.gnode-overlay .status-mini) {
    font-size: 0.6rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    flex-shrink: 0;
  }
  :global(.gnode-overlay .output-tag) {
    font-size: 0.55rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    background: var(--accent);
    color: var(--accent-ink);
    font-weight: 700;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  :global(.gnode-overlay .dur) { opacity: 0.85; }

  .gnode-toggle {
    appearance: none;
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid rgba(255, 255, 255, 0.25);
    width: 26px;
    height: 14px;
    border-radius: 999px;
    padding: 0;
    cursor: pointer;
    position: relative;
    flex-shrink: 0;
    transition: background-color 160ms ease, border-color 160ms ease;
    pointer-events: auto;
  }
  .gnode-toggle.on {
    background: var(--accent);
    border-color: var(--accent);
  }
  .gnode-toggle-knob {
    position: absolute;
    top: 1px;
    left: 1px;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--bg);
    transition: transform 160ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .gnode-toggle.on .gnode-toggle-knob {
    transform: translateX(12px);
    background: white;
  }

  .gnode.disabled .gnode-thumb {
    opacity: 0.35;
    filter: grayscale(0.6);
  }

  /* xyflow handles: visible little dots so the user can see the flow
     direction. We don't expose port-level connection in this PR but
     the visual cue makes the topology readable. */
  :global(.gnode .svelte-flow__handle) {
    width: 10px;
    height: 10px;
    background: var(--bg);
    border: 2px solid var(--border-strong);
    transition: border-color 160ms ease, background 160ms ease;
  }
  :global(.gnode:hover .svelte-flow__handle) {
    border-color: var(--accent);
  }
</style>
