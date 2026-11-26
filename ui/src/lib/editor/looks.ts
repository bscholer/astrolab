/**
 * Looks: one tap sets several controls at once. Palettes change only the
 * colour mapping (duo-band data); styles change light and colour and keep
 * the palette. Each card in the editor shows the look on the user's own
 * picture, rendered by the instant preview.
 */
import { has, set, type Ctx, type Draft } from './controls';

export interface Look {
  id: string;
  name: string;
  sub: string;
  show: (ctx: Ctx) => boolean;
  apply: (ctx: Ctx) => Draft;
  /** True when the draft already looks like this. */
  active?: (ctx: Ctx) => boolean;
}

const duo = (c: Ctx) => has(c.project, 'channel_map');

function palette(ha: number, oiii: number) {
  return (c: Ctx) => {
    const d = set(c, 'channel_map', { enabled: true, ha_hue: ha, oiii_hue: oiii });
    // Green removal would eat the OIII; it goes off with any palette.
    return has(c.project, 'color_balance') ? set({ ...c, draft: d }, 'color_balance', { enabled: false }) : d;
  };
}

function isPalette(ha: number, oiii: number) {
  return (c: Ctx) => {
    const cm = { ...(c.project.template.nodes.find((n) => n.id === 'channel_map')?.params ?? {}), ...(c.draft.channel_map ?? {}) } as Record<string, unknown>;
    return !!cm.enabled && Number(cm.ha_hue ?? 24) === ha && Number(cm.oiii_hue ?? 215) === oiii;
  };
}

export const PALETTES: Look[] = [
  { id: 'gold-azure', name: 'Gold & azure', sub: 'Modern narrowband', show: duo, apply: palette(24, 215), active: isPalette(24, 215) },
  { id: 'hoo', name: 'HOO', sub: 'Red & teal', show: duo, apply: palette(4, 213), active: isPalette(4, 213) },
  { id: 'copper-teal', name: 'Copper & teal', sub: 'Warm and cool', show: duo, apply: palette(14, 188), active: isPalette(14, 188) },
  { id: 'rose-sky', name: 'Rose & sky', sub: 'Soft pink and blue', show: duo, apply: palette(345, 200), active: isPalette(345, 200) },
  {
    id: 'natural', name: 'Natural', sub: 'As the camera saw it', show: duo,
    apply: (c) => {
      const d = set(c, 'channel_map', { enabled: false });
      return has(c.project, 'color_balance') ? set({ ...c, draft: d }, 'color_balance', { enabled: true }) : d;
    },
    active: (c) => !({ ...(c.project.template.nodes.find((n) => n.id === 'channel_map')?.params ?? {}), ...(c.draft.channel_map ?? {}) } as Record<string, unknown>).enabled,
  },
];

const FINISH = ['stretch', 'auto_bp_shift'];
const TOUCH_KEYS = ['contrast', 'highlights', 'shadows', 'sharpen'];

/** The draft with light and colour back at the template's values; palette,
 *  stars, crop, gradient and noise settings stay. */
function neutral(c: Ctx): Draft {
  const d: Draft = { ...c.draft };
  for (const n of FINISH) delete d[n];
  if (d.starnet_recombine) {
    const r = { ...d.starnet_recombine };
    for (const k of TOUCH_KEYS) delete r[k];
    if (Object.keys(r).length) d.starnet_recombine = r;
    else delete d.starnet_recombine;
  }
  return d;
}

function style(values: {
  stretch?: Record<string, unknown>;
  bp?: Record<string, unknown>;
  touch?: Record<string, unknown>;
}) {
  return (c: Ctx) => {
    let d = neutral(c);
    if (values.stretch) d = set({ ...c, draft: d }, 'stretch', values.stretch);
    if (values.bp && has(c.project, 'auto_bp_shift')) d = set({ ...c, draft: d }, 'auto_bp_shift', values.bp);
    if (values.touch && has(c.project, 'starnet_recombine')) d = set({ ...c, draft: d }, 'starnet_recombine', values.touch);
    return d;
  };
}

const always = () => true;

export const STYLES: Look[] = [
  { id: 'balanced', name: 'Balanced', sub: 'The recipe as made', show: always, apply: style({}) },
  {
    id: 'vivid', name: 'Vivid', sub: 'Richer colour, more pop', show: always,
    apply: style({ stretch: { saturation: 1.6 }, touch: { contrast: 0.25 } }),
  },
  {
    id: 'deep', name: 'Deep sky', sub: 'Black sky, bold nebula', show: always,
    apply: style({ bp: { clip_fraction: 0.025 }, touch: { contrast: 0.35, shadows: -0.25 } }),
  },
  {
    id: 'faint', name: 'Faint detail', sub: 'Lift the dust and wisps', show: always,
    apply: style({ touch: { shadows: 0.45, highlights: -0.25 }, stretch: { saturation: 1.2 } }),
  },
  {
    id: 'soft', name: 'Soft', sub: 'Gentle and airy', show: always,
    apply: style({ touch: { contrast: -0.25, highlights: -0.2 }, stretch: { saturation: 0.9 } }),
  },
];
