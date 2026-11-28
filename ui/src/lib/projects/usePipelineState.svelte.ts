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
  nodeProgress: Record<string, { fraction: number; message: string; at?: number }>;
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

const RAW_STATUS: Record<string, NodeStatus> = {
  node_started: 'running',
  node_cached: 'cached',
  node_skipped: 'skipped',
  node_completed: 'completed',
  node_failed: 'failed'
};

export function createPipelineState() {
  let nodeStatus = $state<Record<string, NodeStatus>>({});
  let nodeProgress = $state<Record<string, { fraction: number; message: string; at?: number }>>({});
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

  // Per-night steps ('stack@42') run once per night. The page shows one row per
  // template step, so keep each night's own status and progress here and fold them
  // into the step: done when every night is, progress as the share of nights done.
  // The same tallies give the job-level percentage, over the job's real node count.
  const DONE_STATUS = new Set<NodeStatus>(['completed', 'cached', 'skipped']);
  let rawStatus: Record<string, NodeStatus> = {};
  let rawProgress: Record<string, { fraction: number; message: string; at?: number }> = {};
  let shape = $state({ nodeTotal: 0, nights: 1 });

  function foldNights(base: string) {
    const ids = Object.keys(rawStatus).filter((k) => k.startsWith(`${base}@`));
    if (!ids.length) return;
    const n = Math.max(shape.nights, ids.length);
    const sts = ids.map((i) => rawStatus[i]);
    const done = sts.filter((st) => DONE_STATUS.has(st)).length;
    const running = ids.filter((i) => rawStatus[i] === 'running');
    let status: NodeStatus;
    if (sts.includes('failed')) status = 'failed';
    else if (done >= n) status = sts.every((st) => st === 'cached') ? 'cached' : 'completed';
    else status = 'running';
    nodeStatus = { ...nodeStatus, [base]: status };
    if (status === 'running') {
      const partial = running.reduce((a, i) => a + (rawProgress[i]?.fraction ?? 0), 0);
      const last = running.length ? rawProgress[running[running.length - 1]] : undefined;
      nodeProgress = {
        ...nodeProgress,
        [base]: { fraction: (done + partial) / n, message: last?.message ?? '', at: last?.at }
      };
    } else if (base in nodeProgress) {
      const { [base]: _gone, ...rest } = nodeProgress;
      nodeProgress = rest;
    }
  }

  /** What the page knows about the job's size: how many nodes it runs, how many nights. */
  function setShape(nodeTotal: number | undefined, nights: number | undefined) {
    const next = { nodeTotal: nodeTotal ?? 0, nights: Math.max(1, nights ?? 1) };
    if (next.nodeTotal === shape.nodeTotal && next.nights === shape.nights) return;
    shape = next;
    const bases = new Set(
      Object.keys(rawStatus).filter((k) => k.includes('@')).map((k) => k.split('@')[0])
    );
    for (const b of bases) foldNights(b);
  }

  /** Share of the job done, 0..1: finished nodes plus running nodes' progress, over all nodes. */
  function jobFraction(): number {
    const ids = Object.keys(rawStatus);
    const total = Math.max(shape.nodeTotal, ids.length);
    if (!total) return 0;
    let done = 0;
    for (const id of ids) {
      const st = rawStatus[id];
      if (DONE_STATUS.has(st)) done += 1;
      else if (st === 'running') done += Math.min(1, Math.max(0, rawProgress[id]?.fraction ?? 0));
    }
    return Math.min(1, done / total);
  }

  function eventKey(ev: JobEvent): string {
    return `${ev.timestamp}|${ev.type}|${ev.node_id ?? ''}|${ev.fraction ?? ''}|${ev.message ?? ''}`;
  }

  function applyEvent(ev: JobEvent, onJobTerminal?: () => void) {
    const key = eventKey(ev);
    if (seenEventKey.has(key)) return;
    seenEventKey.add(key);
    // Per-night copies ('stack@42') report on their template node; see foldNights.
    const fullId = ev.node_id;
    const nightly = !!fullId?.includes('@');
    if (fullId && ev.type.startsWith('node_')) {
      if (ev.type === 'node_progress') {
        if (ev.fraction !== undefined) {
          const prev = rawProgress[fullId]?.fraction ?? 0;
          rawProgress[fullId] = {
            fraction: Math.max(prev, ev.fraction),
            message: ev.message ?? '',
            at: Date.parse(ev.timestamp) || undefined
          };
        }
      } else if (ev.type in RAW_STATUS) {
        rawStatus[fullId] = RAW_STATUS[ev.type];
      }
    }
    if (nightly) ev = { ...ev, node_id: fullId!.split('@')[0] };
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
              [ev.node_id]: { fraction: ev.fraction, message: ev.message, at: ts }
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
    if (nightly) foldNights(ev.node_id!);
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
    rawStatus = {};
    rawProgress = {};
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
    rawStatus = {};
    rawProgress = {};
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
    rawStatus = {};
    rawProgress = {};
    seenEventKey.clear();
  }

  function onPreviewLoad(nid: string) {
    previewLoaded = { ...previewLoaded, [nid]: true };
  }
  function onPreviewError(nid: string) {
    previewLoaded = { ...previewLoaded, [nid]: false };
  }

  return {
    setShape,
    jobFraction,
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
