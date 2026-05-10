<!--
  Compact xyflow node for the pipeline graph.

  Earlier iteration shipped with a full preview thumb on each node, but
  in a long linear template (16 nodes) that produced a 4400px-wide
  graph that fitView crushed into illegibility. The graph view's job is
  topology + live status; the preview belongs in the side panel where
  it can be big enough to actually look at. So this card is small:
  a status indicator, the node name, an on/off toggle, and a footer
  pill for cached/running/etc state. Click to select; the parent
  surfaces the param form (and a real preview) in the side panel.
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
    inputPorts: string[];
    outputPorts: string[];
    onToggle: () => void;
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

  <span class="gnode-dot status-dot-{data.togglable && !data.enabled ? 'off' : data.status}" aria-hidden="true"></span>
  <span class="gnode-name">{nodeDisplayName(data.kind, data.nid)}</span>
  {#if data.isOutput}
    <span class="gnode-final" title="final output">★</span>
  {/if}
  <span class="gnode-spacer"></span>
  {#if data.togglable}
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

  <!-- Footer status pill — outside the main flex row so it spans the
       full width of the compact card and reads cleanly even at small
       sizes. Doubles as the progress bar when running. -->
  <div class="gnode-footer">
    {#if data.status === 'running' && data.progressFraction !== null}
      <div class="gnode-progress" style:width="{data.progressFraction * 100}%"></div>
    {/if}
    <span class="gnode-status-pill">
      {statusLabel()}{#if (data.status === 'completed' || data.status === 'failed') && data.durationMs && data.enabled} · {durationString(data.durationMs)}{/if}
    </span>
  </div>
</div>

<style>
  .gnode {
    position: relative;
    width: 196px;
    min-height: 56px;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.45rem 0.55rem;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    cursor: pointer;
    transition: border-color 160ms ease, box-shadow 160ms ease, background 160ms ease;
  }
  .gnode:hover { border-color: var(--border-strong); }
  .gnode.selected {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent), 0 6px 24px rgba(0, 0, 0, 0.4);
  }
  .gnode.output {
    border-color: var(--accent-soft);
  }
  .gnode.output.selected {
    box-shadow: 0 0 0 1px var(--accent), 0 6px 24px rgba(0, 0, 0, 0.4);
  }
  .gnode.disabled {
    opacity: 0.55;
    background: var(--bg-elev-2);
  }

  .gnode-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--fg-mute);
  }
  .status-dot-pending { background: var(--fg-mute); }
  .status-dot-running { background: var(--accent); animation: pulse 1.6s ease-in-out infinite; }
  .status-dot-cached { background: var(--accent-soft); border: 1px solid var(--accent); }
  .status-dot-completed { background: var(--accent); }
  .status-dot-failed { background: var(--bad); }
  .status-dot-off { background: rgba(255, 255, 255, 0.15); }
  @keyframes pulse {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }

  .gnode-name {
    font-weight: 600;
    font-size: 0.82rem;
    color: var(--fg);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1 1 auto;
    min-width: 0;
  }
  .gnode-final {
    color: var(--accent);
    font-size: 0.85rem;
    line-height: 1;
    flex-shrink: 0;
  }
  .gnode-spacer { display: none; }

  .gnode-toggle {
    appearance: none;
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid var(--hairline);
    width: 26px;
    height: 14px;
    border-radius: 999px;
    padding: 0;
    cursor: pointer;
    position: relative;
    flex-shrink: 0;
    transition: background-color 160ms ease, border-color 160ms ease;
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

  .gnode-footer {
    flex: 0 0 100%;
    position: relative;
    height: 16px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.04);
    overflow: hidden;
  }
  .gnode-progress {
    position: absolute;
    inset: 0 auto 0 0;
    background: var(--accent);
    opacity: 0.35;
    transition: width 200ms ease;
  }
  .gnode-status-pill {
    position: relative;
    z-index: 1;
    display: block;
    text-align: center;
    font-family: var(--font-mono, monospace);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute);
    line-height: 16px;
  }
  .gnode.status-completed .gnode-status-pill,
  .gnode.status-cached .gnode-status-pill { color: var(--accent); }
  .gnode.status-running .gnode-status-pill { color: var(--accent); }
  .gnode.status-failed .gnode-status-pill { color: var(--bad); }

  /* xyflow handle styling matching app theme */
  :global(.gnode .svelte-flow__handle) {
    width: 8px;
    height: 8px;
    background: var(--bg);
    border: 2px solid var(--border-strong);
  }
  :global(.gnode:hover .svelte-flow__handle) {
    border-color: var(--accent);
  }
</style>
