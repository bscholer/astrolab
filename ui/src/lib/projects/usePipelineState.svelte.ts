/**
 * Pipeline node state: status, progress, hashes, timing, and the
 * dedup set that keeps replay from re-applying events.
 *
 * Extracted so it can be unit-tested without mounting the page.
 */

import type { JobEvent, NodeWarning, TemplateSchema } from '$lib/api';

export type NodeStatus = 'pending' | 'running' | 'cached' | 'skipped' | 'completed' | 'failed';

export interface PipelineState {
  nodeStatus: Record<string, NodeStatus>;
  nodeProgress: Record<string, { fraction: number; message: string }>;
  nodeHash: Record<string, string>;
  nodeKind: Record<string, string>;
  nodePort: Record<string, string>;
  nodeStartedAt: Record<string, number>;
  nodeDurationMs: Record<string, number>;
  // Structured warnings the backend surfaced via `node_warning` events.
  // Stays empty for nodes that ran cleanly; populated for any node
  // whose run hit a fallback / partial-success path (calibrate is the
  // first consumer). Cache hits replay the persisted warnings so the
  // badge survives a page reload.
  nodeWarnings: Record<string, NodeWarning[]>;
  previewLoaded: Record<string, boolean>;
}

export function createPipelineState() {
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string }>>({});
  let nodeHash = $state<Record<string, string>>({});
  let nodeKind = $state<Record<string, string>>({});
  let nodePort = $state<Record<string, string>>({});
  // Per-node timing. Captured from event timestamps so we can show
  // "completed (1.3s)" on the status badge.
  let nodeStartedAt = $state<Record<string, number>>({});
  let nodeDurationMs = $state<Record<string, number>>({});
  let nodeWarnings = $state<Record<string, NodeWarning[]>>({});
  // Per-node preview-image load state for skeleton shimmer.
  let previewLoaded = $state<Record<string, boolean>>({});

  const seenEventKey = new Set<string>();

  function eventKey(ev: JobEvent): string {
    return `${ev.timestamp}|${ev.type}|${ev.node_id ?? ''}|${ev.fraction ?? ''}|${ev.message ?? ''}`;
  }

  function applyEvent(ev: JobEvent, onJobTerminal?: () => void) {
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
        case 'node_skipped':
          // Lazy-skip from the runtime: upstream miss whose downstream
          // consumers all cache-hit, so its outputs would never be read.
          // Distinct from 'cached' because the files aren't on disk —
          // the preview pane shouldn't try to load anything.
          nodeStatus = { ...nodeStatus, [ev.node_id]: 'skipped' };
          break;
        case 'node_warning': {
          if (ev.kind && ev.message) {
            const prior = nodeWarnings[ev.node_id] ?? [];
            const entry: NodeWarning = { kind: ev.kind, message: ev.message };
            if (ev.details) entry.details = ev.details;
            nodeWarnings = {
              ...nodeWarnings,
              [ev.node_id]: [...prior, entry],
            };
          }
          break;
        }
        case 'node_completed':
        case 'node_failed': {
          nodeStatus = {
            ...nodeStatus,
            [ev.node_id]: ev.type === 'node_completed' ? 'completed' : 'failed'
          };
          const startedAt = nodeStartedAt[ev.node_id];
          if (startedAt !== undefined && !Number.isNaN(ts)) {
            nodeDurationMs = {
              ...nodeDurationMs,
              [ev.node_id]: Math.max(0, ts - startedAt)
            };
          }
          break;
        }
      }
    }
    if (ev.type === 'job_completed' || ev.type === 'job_failed') {
      onJobTerminal?.();
    }
  }

  /** Full reset: clears everything including hashes, kinds, ports. */
  function reset() {
    nodeStatus = {};
    nodeProgress = {};
    nodeHash = {};
    nodeKind = {};
    nodePort = {};
    nodeStartedAt = {};
    nodeDurationMs = {};
    nodeWarnings = {};
    seenEventKey.clear();
  }

  /** Soft reset for patch/revert/reprocess: keeps hashes, kinds, ports,
   * and previewLoaded so the previously-rendered preview stays visible
   * while the new job runs. Only job-tied state gets cleared. */
  function softReset(nodes: TemplateSchema['nodes']) {
    nodeStatus = Object.fromEntries(nodes.map((n) => [n.node_id, 'pending' as const]));
    nodeProgress = {};
    nodeStartedAt = {};
    nodeDurationMs = {};
    nodeWarnings = {};
    seenEventKey.clear();
  }

  function initFromTemplate(
    templateNodes: Array<{ id: string; kind: string }>,
    pickPreviewPort: (nid: string) => string,
    schemaNodes: TemplateSchema['nodes']
  ) {
    const initKind: Record<string, string> = {};
    const initPort: Record<string, string> = {};
    for (const n of templateNodes) {
      initKind[n.id] = n.kind;
      initPort[n.id] = pickPreviewPort(n.id);
    }
    nodeKind = initKind;
    nodePort = initPort;
    nodeStatus = Object.fromEntries(templateNodes.map((n) => [n.id, 'pending' as const]));
    seenEventKey.clear();
  }

  function onPreviewLoad(nid: string) {
    previewLoaded = { ...previewLoaded, [nid]: true };
  }
  function onPreviewError(nid: string) {
    previewLoaded = { ...previewLoaded, [nid]: false };
  }

  return {
    get nodeStatus() { return nodeStatus; },
    get nodeProgress() { return nodeProgress; },
    get nodeHash() { return nodeHash; },
    get nodeKind() { return nodeKind; },
    get nodePort() { return nodePort; },
    get nodeStartedAt() { return nodeStartedAt; },
    get nodeDurationMs() { return nodeDurationMs; },
    get nodeWarnings() { return nodeWarnings; },
    get previewLoaded() { return previewLoaded; },
    applyEvent,
    reset,
    softReset,
    initFromTemplate,
    onPreviewLoad,
    onPreviewError,
  };
}
