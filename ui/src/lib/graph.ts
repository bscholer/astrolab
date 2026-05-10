/**
 * Layered DAG layout for Template visualization.
 *
 * Each node lands at column = max(parent column) + 1. Within a column nodes
 * stack vertically with even spacing. Edges are straight segments between
 * the right edge of the source rect and the left edge of the target rect.
 *
 * This is intentionally tiny: enough to render a 5-10 node pipeline. If we
 * end up with branchy templates that look cramped, swap in dagre-d3 later.
 */
import type { CostClass, NodeSpec, Template } from './api';

// Pretty names for the registered nodes. The Python kind is the stable id
// (used in the cache hash); this map is only for display. Keep in sync when
// new node kinds land.
const NODE_DISPLAY_NAMES: Record<string, string> = {
  convert_lights: 'Convert',
  calibrate: 'Calibrate',
  seq_resample: 'Resample',
  seq_offset: 'Pedestal',
  seq_bg_extract: 'Background',
  seq_register: 'Register',
  seq_stack: 'Stack',
  auto_crop: 'Auto-trim',
  graxpert: 'GraXpert',
  stretch: 'Stretch',
  starnet_extract: 'Extract Stars',
  starnet_replace: 'Synth Stars',
  starnet_recombine: 'Recombine',
  crop: 'Crop',
  save_image: 'Save Image',
  downscale: 'Downscale'
};

// Per-node-id overrides for cases where one kind is instantiated multiple
// times in a template (eg GraXpert lives once for bg-extract, once for
// denoising). Falls back to kind-level naming when there's no entry.
const NODE_ID_DISPLAY_NAMES: Record<string, string> = {
  graxpert_bg: 'BG Extract',
  graxpert_denoise: 'Denoise'
};

export function nodeDisplayName(kind: string, nodeId?: string): string {
  if (nodeId && NODE_ID_DISPLAY_NAMES[nodeId]) return NODE_ID_DISPLAY_NAMES[nodeId];
  if (NODE_DISPLAY_NAMES[kind]) return NODE_DISPLAY_NAMES[kind];
  // Fallback: snake_case -> Title Case so a freshly added node still looks
  // tidy until we drop it in the map.
  return kind
    .split('_')
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(' ');
}

export interface LayoutNode {
  id: string;
  kind: string;
  col: number;
  row: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutEdge {
  fromId: string;
  toId: string;
  fromPort: string;
  toPort: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface Layout {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

const NODE_W = 160;
const NODE_H = 64;
const COL_GAP = 60;
const ROW_GAP = 24;
const MARGIN = 24;

export function layoutTemplate(template: Template): Layout {
  // Build adjacency from explicit input edges.
  const byId = new Map<string, NodeSpec>();
  for (const n of template.nodes) byId.set(n.id, n);

  const parents = new Map<string, string[]>();
  for (const n of template.nodes) {
    const ps: string[] = [];
    for (const src of Object.values(n.inputs)) {
      const srcId = src.split('.', 1)[0];
      if (byId.has(srcId)) ps.push(srcId);
    }
    parents.set(n.id, ps);
  }

  // Compute column = longest path from a source. Memoized topo over `parents`.
  const col = new Map<string, number>();
  const visiting = new Set<string>();
  function colOf(id: string): number {
    if (col.has(id)) return col.get(id)!;
    if (visiting.has(id)) return 0; // cycle guard, shouldn't happen
    visiting.add(id);
    const ps = parents.get(id) ?? [];
    const c = ps.length === 0 ? 0 : Math.max(...ps.map(colOf)) + 1;
    visiting.delete(id);
    col.set(id, c);
    return c;
  }
  for (const n of template.nodes) colOf(n.id);

  // Group nodes by column to assign rows.
  const byCol = new Map<number, string[]>();
  for (const n of template.nodes) {
    const c = col.get(n.id)!;
    if (!byCol.has(c)) byCol.set(c, []);
    byCol.get(c)!.push(n.id);
  }
  for (const [, ids] of byCol) ids.sort();

  // Place nodes.
  const nodes: LayoutNode[] = [];
  const placed = new Map<string, LayoutNode>();
  const colCount = Math.max(...Array.from(byCol.keys()), 0) + 1;
  const totalRows = Math.max(...Array.from(byCol.values(), (a) => a.length));
  for (const [c, ids] of byCol) {
    ids.forEach((id, row) => {
      const x = MARGIN + c * (NODE_W + COL_GAP);
      const y = MARGIN + row * (NODE_H + ROW_GAP);
      const node: LayoutNode = {
        id,
        kind: byId.get(id)!.kind,
        col: c,
        row,
        x,
        y,
        width: NODE_W,
        height: NODE_H
      };
      nodes.push(node);
      placed.set(id, node);
    });
  }

  // Edges: one per (target, input_port).
  const edges: LayoutEdge[] = [];
  for (const n of template.nodes) {
    const target = placed.get(n.id);
    if (!target) continue;
    for (const [toPort, src] of Object.entries(n.inputs)) {
      const [fromId, fromPort] = src.split('.', 2);
      const source = placed.get(fromId);
      if (!source) continue;
      edges.push({
        fromId,
        toId: n.id,
        fromPort,
        toPort,
        x1: source.x + source.width,
        y1: source.y + source.height / 2,
        x2: target.x,
        y2: target.y + target.height / 2
      });
    }
  }

  return {
    nodes,
    edges,
    width: MARGIN * 2 + colCount * NODE_W + (colCount - 1) * COL_GAP,
    height: MARGIN * 2 + totalRows * NODE_H + (totalRows - 1) * ROW_GAP
  };
}

/**
 * Togglable nodes: the project page surfaces an OFF/ON switch on each
 * node card whose schema declares an `enabled` boolean. The switch
 * piggybacks on the existing override mechanism (`overrides[nid].enabled`);
 * when a node is OFF its run() pass-through path emits identity outputs
 * so the rest of the DAG stays consistent. Non-togglable nodes (convert,
 * calibrate, register, stack, save) don't expose `enabled` and so don't
 * get a switch.
 */
export function isTogglable(
  schemaProps: Record<string, { type?: string | string[]; default?: unknown }>
): boolean {
  const f = schemaProps['enabled'];
  if (!f) return false;
  // Tolerate both 'boolean' and ['boolean', 'null'] shapes that pydantic
  // emits for Field(default=False) vs Optional[bool].
  const t = f.type;
  if (Array.isArray(t)) return t.includes('boolean');
  return t === 'boolean';
}

/**
 * Walk the per-template ui_depends_on chain to decide whether a node
 * should render in the pipeline view.
 *
 * A node declares a single upstream id whose `enabled` param must be
 * true. That upstream may itself declare its own upstream, so chains
 * collapse transitively (recombine -> replace -> extract).
 *
 * Returns false if any link in the chain has enabled=false. Always
 * returns true when the chain has no entry. Fails open if a chain
 * names a non-existent node so a typo doesn't blank the whole UI.
 */
export function isNodeVisible(
  nodeId: string,
  schemaByNodeId: Record<
    string,
    {
      ui_depends_on?: string | null;
      defaults: Record<string, unknown>;
      template_params: Record<string, unknown>;
    }
  >,
  overridesByNodeId: Record<string, Record<string, unknown>>
): boolean {
  const seen = new Set<string>();
  let cursor: string | null | undefined = schemaByNodeId[nodeId]?.ui_depends_on;
  while (cursor) {
    if (seen.has(cursor)) return true; // cycle guard, shouldn't happen
    seen.add(cursor);
    const upstream = schemaByNodeId[cursor];
    if (!upstream) return true; // typo / unknown ref; fail open
    const merged = { ...upstream.defaults, ...upstream.template_params };
    const ov = overridesByNodeId[cursor] ?? {};
    const v = 'enabled' in ov ? ov.enabled : merged.enabled;
    // Nodes without an `enabled` field implicitly count as on. Otherwise
    // any falsy value hides this whole subchain.
    if (v !== undefined && !v) return false;
    cursor = upstream.ui_depends_on;
  }
  return true;
}

/**
 * Cost-aware affordance helpers.
 *
 * When the user tweaks a param on node N, every downstream node has to
 * re-execute (its inputs changed). The "blast radius" for editing N is the
 * set {N} ∪ descendants(N), and the "cost" of editing N is the worst
 * cost-class in that set (cheap < medium < expensive). We surface this in
 * the UI as a colored badge per param so users see "this slider triggers a
 * full re-stack" before they touch it.
 *
 * Cost classes are lifted from the schema endpoint (one per node). For
 * nodes the schema doesn't cover (shouldn't happen with valid templates),
 * we conservatively assume 'expensive' so we don't accidentally claim a
 * tweak is free.
 */

const COST_RANK: Record<CostClass, number> = {
  cheap: 0,
  medium: 1,
  expensive: 2
};

export function descendantsOf(template: Template, nodeId: string): Set<string> {
  // Build a child map (id -> ids that consume its outputs) once and BFS.
  const children = new Map<string, string[]>();
  for (const n of template.nodes) {
    for (const src of Object.values(n.inputs)) {
      const srcId = src.split('.', 1)[0];
      if (!children.has(srcId)) children.set(srcId, []);
      children.get(srcId)!.push(n.id);
    }
  }
  const out = new Set<string>();
  const queue = [nodeId];
  while (queue.length) {
    const cur = queue.shift()!;
    for (const c of children.get(cur) ?? []) {
      if (!out.has(c)) {
        out.add(c);
        queue.push(c);
      }
    }
  }
  return out;
}

export function blastRadiusCost(
  template: Template,
  nodeId: string,
  costByNodeId: Record<string, CostClass>
): CostClass {
  const closure = descendantsOf(template, nodeId);
  closure.add(nodeId);
  let worst: CostClass = 'cheap';
  for (const id of closure) {
    const c = costByNodeId[id] ?? 'expensive';
    if (COST_RANK[c] > COST_RANK[worst]) worst = c;
  }
  return worst;
}
