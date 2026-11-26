/**
 * The editor's controls, each a friendly 0-100 (or -100..100) scale over
 * one or more pipeline params. The draft is the project's full override
 * map as the editor has it (node id -> params), with any param equal to
 * the template's value left out, as the server stores them.
 *
 * `live` controls only touch steps the instant preview runs
 * (server/quick_preview.py); the rest (gradient removal, denoise, crop)
 * show once the pipeline has re-rendered.
 */
import type { Project } from '$lib/api';

export type Draft = Record<string, Record<string, unknown>>;
export type Tab = 'looks' | 'crop' | 'light' | 'color' | 'detail' | 'versions';

export interface Ctx {
  project: Project;
  draft: Draft;
}

// Node defaults for params the template doesn't set itself (mirrors the
// nodes' pydantic defaults).
const FALLBACK: Record<string, Record<string, unknown>> = {
  stretch: { method: 'autostretch', target_bg: 0.25, auto_object: 0.4, saturation: 1, shadows_clip: -2.8 },
  auto_bp_shift: { enabled: true, clip_fraction: 0.01 },
  graxpert_bg: { enabled: false },
  graxpert_denoise: { enabled: false, strength: 0.5 },
  color_balance: { enabled: true, amount: 0.85 },
  channel_map: { enabled: false, oiii_strength: 1, ha_hue: 24, oiii_hue: 215 },
  starnet_extract: { enabled: false },
  starnet_recombine: {
    enabled: true, stars: 1, tighten: 0, contrast: 0, highlights: 0, shadows: 0, sharpen: 0
  },
};

export function has(project: Project, nodeId: string): boolean {
  return project.template.nodes.some((n) => n.id === nodeId);
}

export function templateParams(project: Project, nodeId: string): Record<string, unknown> {
  const node = project.template.nodes.find((n) => n.id === nodeId);
  return { ...(FALLBACK[nodeId] ?? {}), ...((node?.params as Record<string, unknown>) ?? {}) };
}

export function get(ctx: Ctx, nodeId: string, param: string): unknown {
  const o = ctx.draft[nodeId];
  if (o && param in o) return o[param];
  return templateParams(ctx.project, nodeId)[param];
}

const num = (ctx: Ctx, node: string, param: string, dflt = 0) => {
  const v = Number(get(ctx, node, param));
  return Number.isFinite(v) ? v : dflt;
};

/** A new draft with these values set on a node; values equal to the
 *  template's own are dropped, so the override map stays minimal. */
export function set(ctx: Ctx, nodeId: string, values: Record<string, unknown>): Draft {
  const base = templateParams(ctx.project, nodeId);
  const node = { ...(ctx.draft[nodeId] ?? {}) };
  for (const [k, v] of Object.entries(values)) {
    if (base[k] === v) delete node[k];
    else node[k] = v;
  }
  const next = { ...ctx.draft };
  if (Object.keys(node).length) next[nodeId] = node;
  else delete next[nodeId];
  return next;
}

/** Several nodes at once (a control that touches two steps). */
function setMany(ctx: Ctx, changes: Record<string, Record<string, unknown>>): Draft {
  let draft = ctx.draft;
  for (const [node, values] of Object.entries(changes)) draft = set({ ...ctx, draft }, node, values);
  return draft;
}

const round = (x: number, d = 3) => Number(x.toFixed(d));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const unlerp = (a: number, b: number, x: number) => (b === a ? 0 : (x - a) / (b - a));
const clamp = (x: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, x));

export interface SliderDef {
  kind: 'slider';
  id: string;
  tab: Tab;
  label: string;
  icon: string;
  hint: string;
  min: number;
  max: number;
  /** Display unit: '' for plain numbers, '°' for hues. */
  unit?: string;
  /** Filled from the centre (bipolar) rather than from the left. */
  bipolar?: boolean;
  /** Track painted as a hue wheel. */
  hue?: boolean;
  live: boolean;
  show: (ctx: Ctx) => boolean;
  read: (ctx: Ctx) => number;
  write: (ctx: Ctx, v: number) => Draft;
  format?: (v: number) => string;
}

export interface ToggleDef {
  kind: 'toggle';
  id: string;
  tab: Tab;
  label: string;
  icon: string;
  hint: string;
  live: boolean;
  show: (ctx: Ctx) => boolean;
  read: (ctx: Ctx) => boolean;
  write: (ctx: Ctx, on: boolean) => Draft;
}

export type ControlDef = SliderDef | ToggleDef;

const stretchMethod = (ctx: Ctx) => String(get(ctx, 'stretch', 'method'));
const hooOn = (ctx: Ctx) => has(ctx.project, 'channel_map') && !!get(ctx, 'channel_map', 'enabled');
const starsSplit = (ctx: Ctx) => !!get(ctx, 'starnet_extract', 'enabled');

/** Stars and Tighten need StarNet; it runs only while either is in use. */
function starsWrite(ctx: Ctx, stars: number, tighten: number): Draft {
  const on = stars < 1 || tighten > 0;
  return setMany(ctx, {
    starnet_extract: { enabled: on },
    starnet_recombine: { stars: round(stars), tighten: round(tighten) },
  });
}

export const CONTROLS: ControlDef[] = [
  // ---- Light
  {
    kind: 'slider', id: 'brightness', tab: 'light', label: 'Brightness', icon: 'sun',
    hint: 'How bright the picture comes out.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'stretch') && ['autostretch', 'auto'].includes(stretchMethod(c)),
    read: (c) =>
      stretchMethod(c) === 'auto'
        ? 100 * unlerp(0.15, 0.7, num(c, 'stretch', 'auto_object', 0.4))
        : 100 * Math.sqrt(clamp(unlerp(0.01, 0.35, num(c, 'stretch', 'target_bg', 0.25)), 0, 1)),
    write: (c, v) =>
      stretchMethod(c) === 'auto'
        ? set(c, 'stretch', { auto_object: round(lerp(0.15, 0.7, v / 100)) })
        : set(c, 'stretch', { target_bg: round(lerp(0.01, 0.35, (v / 100) ** 2)) }),
  },
  {
    kind: 'slider', id: 'background', tab: 'light', label: 'Background', icon: 'moon',
    hint: 'How dark the sky gets: more pushes the faintest pixels to black.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'auto_bp_shift') && get(c, 'auto_bp_shift', 'enabled') !== false,
    read: (c) => 100 * Math.sqrt(clamp(unlerp(0.0005, 0.08, num(c, 'auto_bp_shift', 'clip_fraction', 0.01)), 0, 1)),
    write: (c, v) => set(c, 'auto_bp_shift', { clip_fraction: round(lerp(0.0005, 0.08, (v / 100) ** 2), 5) }),
  },
  {
    kind: 'slider', id: 'contrast', tab: 'light', label: 'Contrast', icon: 'contrast',
    hint: 'Darks darker and lights lighter, or the other way.', min: -100, max: 100, bipolar: true, live: true,
    show: (c) => has(c.project, 'starnet_recombine'),
    read: (c) => 100 * num(c, 'starnet_recombine', 'contrast'),
    write: (c, v) => set(c, 'starnet_recombine', { contrast: round(v / 100, 2) }),
  },
  {
    kind: 'slider', id: 'highlights', tab: 'light', label: 'Highlights', icon: 'highlights',
    hint: 'Lift or hold back the bright parts of the nebula.', min: -100, max: 100, bipolar: true, live: true,
    show: (c) => has(c.project, 'starnet_recombine'),
    read: (c) => 100 * num(c, 'starnet_recombine', 'highlights'),
    write: (c, v) => set(c, 'starnet_recombine', { highlights: round(v / 100, 2) }),
  },
  {
    kind: 'slider', id: 'shadows', tab: 'light', label: 'Shadows', icon: 'shadows',
    hint: 'Lift faint detail out of the dark, or deepen it.', min: -100, max: 100, bipolar: true, live: true,
    show: (c) => has(c.project, 'starnet_recombine'),
    read: (c) => 100 * num(c, 'starnet_recombine', 'shadows'),
    write: (c, v) => set(c, 'starnet_recombine', { shadows: round(v / 100, 2) }),
  },
  {
    kind: 'toggle', id: 'gradient', tab: 'detail', label: 'Remove gradient', icon: 'gradient',
    hint: 'Takes out light pollution and glow. Turn off if a nebula fills the frame.', live: false,
    show: (c) => has(c.project, 'graxpert_bg'),
    read: (c) => !!get(c, 'graxpert_bg', 'enabled'),
    write: (c, on) => set(c, 'graxpert_bg', { enabled: on }),
  },
  // ---- Colour
  {
    kind: 'slider', id: 'balance', tab: 'color', label: 'Ha ↔ OIII', icon: 'balance',
    hint: 'How much of the nebula takes the OIII colour.', min: -100, max: 100, bipolar: true, live: true,
    show: hooOn,
    read: (c) => 100 * (num(c, 'channel_map', 'oiii_strength', 1) - 1),
    write: (c, v) => set(c, 'channel_map', { oiii_strength: round(1 + v / 100, 2) }),
  },
  {
    kind: 'slider', id: 'ha_hue', tab: 'color', label: 'H-alpha colour', icon: 'hue',
    hint: 'The colour of hydrogen-rich nebula: 24 gold, 4 classic red.', min: 0, max: 360, unit: '°', hue: true, live: true,
    show: hooOn,
    read: (c) => num(c, 'channel_map', 'ha_hue', 24),
    write: (c, v) => set(c, 'channel_map', { ha_hue: Math.round(v) }),
  },
  {
    kind: 'slider', id: 'oiii_hue', tab: 'color', label: 'OIII colour', icon: 'hue',
    hint: 'The colour of oxygen-rich nebula: 215 azure, 190 teal.', min: 0, max: 360, unit: '°', hue: true, live: true,
    show: hooOn,
    read: (c) => num(c, 'channel_map', 'oiii_hue', 215),
    write: (c, v) => set(c, 'channel_map', { oiii_hue: Math.round(v) }),
  },
  {
    kind: 'slider', id: 'saturation', tab: 'color', label: 'Saturation', icon: 'drop',
    hint: '50 leaves colour as it is; more is more vivid.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'stretch'),
    read: (c) => 50 * num(c, 'stretch', 'saturation', 1),
    write: (c, v) => set(c, 'stretch', { saturation: round(v / 50, 2) }),
  },
  {
    kind: 'toggle', id: 'green', tab: 'color', label: 'Remove green cast', icon: 'leaf',
    hint: 'Colour cameras see too much green; this neutralises it.', live: true,
    show: (c) => has(c.project, 'color_balance') && !hooOn(c),
    read: (c) => get(c, 'color_balance', 'enabled') !== false,
    write: (c, on) => set(c, 'color_balance', { enabled: on }),
  },
  // ---- Detail
  {
    kind: 'slider', id: 'stars', tab: 'detail', label: 'Stars', icon: 'star',
    hint: '100 keeps the stars as shot; lower shrinks them, faint ones first.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'starnet_extract') && has(c.project, 'starnet_recombine'),
    read: (c) => (starsSplit(c) ? 100 * num(c, 'starnet_recombine', 'stars', 1) : 100),
    write: (c, v) => starsWrite(c, v / 100, starsSplit(c) ? num(c, 'starnet_recombine', 'tighten') : 0),
  },
  {
    kind: 'slider', id: 'tighten', tab: 'detail', label: 'Tighten stars', icon: 'sparkle',
    hint: 'Pulls bloated stars back to points.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'starnet_extract') && has(c.project, 'starnet_recombine'),
    read: (c) => (starsSplit(c) ? 100 * num(c, 'starnet_recombine', 'tighten') : 0),
    write: (c, v) => starsWrite(c, starsSplit(c) ? num(c, 'starnet_recombine', 'stars', 1) : 1, v / 100),
  },
  {
    kind: 'slider', id: 'sharpen', tab: 'detail', label: 'Sharpen', icon: 'focus',
    hint: 'Crisper fine structure. A little goes a long way.', min: 0, max: 100, live: true,
    show: (c) => has(c.project, 'starnet_recombine'),
    read: (c) => 100 * num(c, 'starnet_recombine', 'sharpen'),
    write: (c, v) => set(c, 'starnet_recombine', { sharpen: round(v / 100, 2) }),
  },
  {
    kind: 'slider', id: 'denoise', tab: 'detail', label: 'Noise reduction', icon: 'wave',
    hint: 'GraXpert AI denoise. 40-60 is the usual range.', min: 0, max: 100, live: false,
    show: (c) => has(c.project, 'graxpert_denoise'),
    read: (c) =>
      get(c, 'graxpert_denoise', 'enabled') ? 100 * num(c, 'graxpert_denoise', 'strength', 0.5) : 0,
    write: (c, v) =>
      set(c, 'graxpert_denoise', v === 0 ? { enabled: false } : { enabled: true, strength: round(v / 100, 2) }),
  },
];

/** What the control reads with no overrides of its own (its reset value). */
export function defaultOf(def: ControlDef, ctx: Ctx): number | boolean {
  return def.read({ project: ctx.project, draft: {} });
}

/** Nodes whose changes the instant preview can show. */
export const LIVE_NODES = new Set([
  'channel_map', 'color_balance', 'auto_bp_shift', 'stretch',
  'starnet_extract', 'starnet_replace', 'starnet_recombine',
]);

/** True when moving from `a` to `b` changes a step the preview can't run. */
export function needsRender(a: Draft, b: Draft): boolean {
  const nodes = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const n of nodes) {
    if (LIVE_NODES.has(n)) continue;
    if (JSON.stringify(a[n] ?? {}) !== JSON.stringify(b[n] ?? {})) return true;
  }
  return false;
}
