<!--
  Pipeline DAG view. Mounts xyflow/svelte to render the project's
  template as a left-to-right node graph with live status / preview
  data flowing through the same state maps the card view consumes.

  Phase 1: read-only topology. Drag-to-reposition is supported and
  positions persist in localStorage keyed by (project_id, node_id).
  Auto-layout via dagre runs once for any node without a saved
  position - so the user only re-arranges when they want to, never
  has to fight an auto-layout that 'corrects' their placement.

  When the user clicks a node, we surface its id via the `selected`
  bind so the parent page can render the param form in a side panel.
  (xyflow nodes are fixed-size, so card-mode's inline expand doesn't
  translate cleanly; the side panel is the natural Phase 1 affordance.)
-->
<script lang="ts">
  import {
    SvelteFlow,
    Background,
    Controls,
    MarkerType,
    type Node as FlowNode,
    type Edge as FlowEdge
  } from '@xyflow/svelte';
  import '@xyflow/svelte/dist/style.css';
  import dagre from '@dagrejs/dagre';
  import PipelineGraphNode from './PipelineGraphNode.svelte';
  import { isNodeVisible, isTogglable } from './graph';
  import type { Project, TemplateSchema } from './api';
  import { api } from './api';

  type NodeStatus = 'pending' | 'running' | 'cached' | 'completed' | 'failed';

  type Props = {
    project: Project;
    schema: TemplateSchema;
    schemaByNodeId: Record<string, TemplateSchema['nodes'][number]>;
    nodeStatus: Record<string, NodeStatus>;
    nodeProgress: Record<string, { fraction: number; message: string }>;
    nodeHash: Record<string, string>;
    nodePort: Record<string, string>;
    nodeKind: Record<string, string>;
    nodeDurationMs: Record<string, number>;
    previewLoaded: Record<string, boolean>;
    outputNodeId: string;
    selectedNodeId: string | null;
    onSelectNode: (nid: string | null) => void;
    onToggleNodeEnabled: (
      nid: string,
      props: Record<string, { default?: unknown }>,
      fullDefaults: Record<string, unknown>,
      overrides: Record<string, unknown>
    ) => void;
    onPreviewLoad: (nid: string) => void;
    onPreviewError: (nid: string) => void;
    effectiveEnabled: (
      defaults: Record<string, unknown>,
      overrides: Record<string, unknown>
    ) => boolean;
  };

  let {
    project,
    schema,
    schemaByNodeId,
    nodeStatus,
    nodeProgress,
    nodeHash,
    nodePort,
    nodeKind,
    nodeDurationMs,
    previewLoaded,
    outputNodeId,
    selectedNodeId,
    onSelectNode,
    onToggleNodeEnabled,
    onPreviewLoad,
    onPreviewError,
    effectiveEnabled
  }: Props = $props();

  // ---- Position persistence ------------------------------------------
  // localStorage shape: { [nodeId]: { x, y } }. Persisted per project so
  // each project keeps its own arrangement.
  const POS_KEY = $derived(`astrolab.pipeline_pos.${project.id}`);

  function loadSavedPositions(): Record<string, { x: number; y: number }> {
    if (typeof localStorage === 'undefined') return {};
    try {
      const raw = localStorage.getItem(POS_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch {
      return {};
    }
  }
  function saveSavedPositions(positions: Record<string, { x: number; y: number }>) {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(positions));
    } catch {
      // Quota or disabled - silently drop. Layout still works in-memory.
    }
  }

  // Auto-layout for nodes without a saved position. dagre lays out
  // left-to-right with reasonable per-node padding; we feed it ALL nodes
  // (including ones with saved positions) so its output is internally
  // consistent, then merge saved positions on top so manual arrangements
  // win.
  const NODE_W = 196;
  const NODE_H = 78;

  function computeAutoLayout(): Record<string, { x: number; y: number }> {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: 'LR', nodesep: 18, ranksep: 50, marginx: 16, marginy: 16 });
    g.setDefaultEdgeLabel(() => ({}));
    for (const n of project.template.nodes) {
      g.setNode(n.id, { width: NODE_W, height: NODE_H });
    }
    for (const n of project.template.nodes) {
      for (const src of Object.values(n.inputs)) {
        const srcId = src.split('.')[0];
        if (g.hasNode(srcId)) g.setEdge(srcId, n.id);
      }
    }
    dagre.layout(g);
    const out: Record<string, { x: number; y: number }> = {};
    for (const id of g.nodes()) {
      const dn = g.node(id);
      // dagre returns the center; xyflow expects top-left.
      out[id] = { x: dn.x - dn.width / 2, y: dn.y - dn.height / 2 };
    }
    return out;
  }

  // Initial positions: saved-or-auto. Recomputed when the project's set
  // of nodes changes (added/removed nodes via future editing).
  let positions = $state<Record<string, { x: number; y: number }>>({});
  $effect(() => {
    // Re-key on the set of node ids so adding/removing nodes triggers a
    // fresh layout pass for new nodes only.
    const ids = project.template.nodes.map((n) => n.id).sort().join('|');
    void ids; // touch for reactivity
    const saved = loadSavedPositions();
    const auto = computeAutoLayout();
    const next: Record<string, { x: number; y: number }> = {};
    for (const n of project.template.nodes) {
      next[n.id] = saved[n.id] ?? auto[n.id] ?? { x: 0, y: 0 };
    }
    positions = next;
  });

  // ---- Visibility (ui_depends_on chain) -------------------------------
  const visibleIds = $derived.by(() => {
    const out = new Set<string>();
    for (const n of schema.nodes) {
      if (isNodeVisible(
        n.node_id,
        schemaByNodeId,
        project.current_overrides as Record<string, Record<string, unknown>>
      )) {
        out.add(n.node_id);
      }
    }
    return out;
  });

  // ---- xyflow nodes / edges ------------------------------------------
  const nodes = $derived.by<FlowNode[]>(() => {
    return schema.nodes
      .filter((n) => visibleIds.has(n.node_id))
      .map((nschema) => {
        const nid = nschema.node_id;
        const overrides =
          (project.current_overrides[nid] as Record<string, unknown>) ?? {};
        const props = nschema.schema.properties ?? {};
        const fullDefaults = {
          ...nschema.defaults,
          ...nschema.template_params
        } as Record<string, unknown>;
        const togglable = isTogglable(props);
        const enabled = togglable ? effectiveEnabled(fullDefaults, overrides) : true;
        const status = nodeStatus[nid] ?? 'pending';
        const prog = nodeProgress[nid];
        const kind = nodeKind[nid] ?? nschema.kind ?? nid;
        const inputPorts = Object.keys(nschema.inputs ?? {});
        // Output ports: pull from the node's params_schema if it exposed a
        // shape, but more reliably from the template's referenced sources.
        // For Phase 1, we display 'image' as the default output port and
        // derive any others by scanning what other nodes consume from this
        // one.
        const outPortSet = new Set<string>();
        for (const other of project.template.nodes) {
          for (const src of Object.values(other.inputs)) {
            const [srcId, srcPort] = src.split('.');
            if (srcId === nid && srcPort) outPortSet.add(srcPort);
          }
        }
        if (outPortSet.size === 0) outPortSet.add('image');
        return {
          id: nid,
          type: 'pipeline',
          position: positions[nid] ?? { x: 0, y: 0 },
          data: {
            nid,
            kind,
            status,
            enabled,
            togglable,
            isOutput: nid === outputNodeId,
            isSelected: selectedNodeId === nid,
            progressFraction: prog?.fraction ?? null,
            progressMessage: prog?.message ?? null,
            durationMs: nodeDurationMs[nid] ?? null,
            inputPorts,
            outputPorts: Array.from(outPortSet),
            onToggle: () =>
              onToggleNodeEnabled(nid, props, fullDefaults, overrides)
          },
          // Disable native dragging on the toggle button only - xyflow
          // does this for us via the .nodrag class but our toggle is
          // declared in the node component.
          dragHandle: '.gnode-thumb, .gnode-overlay, .gnode-name'
        } satisfies FlowNode;
      });
  });

  const edges = $derived.by<FlowEdge[]>(() => {
    const out: FlowEdge[] = [];
    for (const n of project.template.nodes) {
      if (!visibleIds.has(n.id)) continue;
      for (const [toPort, src] of Object.entries(n.inputs ?? {})) {
        const [fromId, fromPort] = src.split('.');
        if (!visibleIds.has(fromId)) continue;
        // Mark edges feeding the in-flight node as animated so the user
        // sees data flowing while a re-render is in progress.
        const animated =
          nodeStatus[n.id] === 'running' || nodeStatus[fromId] === 'running';
        out.push({
          id: `${fromId}.${fromPort}->${n.id}.${toPort}`,
          source: fromId,
          target: n.id,
          sourceHandle: fromPort,
          targetHandle: toPort,
          type: 'default',
          animated,
          markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--border-strong)' }
        });
      }
    }
    return out;
  });

  const nodeTypes = { pipeline: PipelineGraphNode };

  // Drag persistence: capture the position from the event and write to
  // localStorage. We don't fight xyflow's internal node state - it owns
  // position during the drag - we just persist the final value. xyflow's
  // event payload is `{ event, targetNode, nodes }` in 1.x; we only need
  // the target's id + position.
  function onNodeDragStop(detail: { targetNode: FlowNode | null }) {
    const t = detail.targetNode;
    if (!t) return;
    const next = { ...positions, [t.id]: t.position };
    positions = next;
    saveSavedPositions(next);
  }

  function onNodeClick(detail: { node: FlowNode }) {
    onSelectNode(detail.node.id);
  }

  function onPaneClick() {
    onSelectNode(null);
  }
</script>

<div class="graph-host">
  <SvelteFlow
    {nodes}
    {edges}
    {nodeTypes}
    nodesDraggable={true}
    nodesConnectable={false}
    elementsSelectable={true}
    fitView
    fitViewOptions={{ padding: 0.06, maxZoom: 1 }}
    minZoom={0.2}
    maxZoom={1.6}
    onnodedragstop={onNodeDragStop}
    onnodeclick={onNodeClick}
    onpaneclick={onPaneClick}
    proOptions={{ hideAttribution: true }}
  >
    <Background />
    <Controls />
  </SvelteFlow>
</div>

<style>
  .graph-host {
    width: 100%;
    /* Sized to read as a panel rather than a full-page canvas. Single-
       row linear templates fill comfortably; branchy templates with
       multi-rank dagre output get headroom via vertical pan. */
    height: 360px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg-elev);
    overflow: hidden;
  }

  /* xyflow ships light-themed default CSS; this carve-out re-skins the
     bits that show through to match the app's dark UI. We keep the
     selectors local with :global because xyflow renders into our
     subtree but its classes aren't ours. */
  :global(.graph-host .svelte-flow) {
    background: var(--bg-elev);
  }
  :global(.graph-host .svelte-flow__background) {
    background-color: var(--bg-elev);
  }
  :global(.graph-host .svelte-flow__edge .svelte-flow__edge-path) {
    stroke: var(--border-strong);
    stroke-width: 1.5;
  }
  :global(.graph-host .svelte-flow__edge.animated .svelte-flow__edge-path) {
    stroke: var(--accent);
    stroke-dasharray: 6 4;
    animation: dash 0.6s linear infinite;
  }
  @keyframes dash {
    to { stroke-dashoffset: -10; }
  }
  :global(.graph-host .svelte-flow__controls) {
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  :global(.graph-host .svelte-flow__controls button) {
    background: transparent;
    border: 0;
    border-bottom: 1px solid var(--border);
    color: var(--fg);
    fill: currentColor;
  }
  :global(.graph-host .svelte-flow__controls button:hover) {
    background: var(--bg-elev);
  }
  :global(.graph-host .svelte-flow__controls button:last-child) {
    border-bottom: 0;
  }
</style>
