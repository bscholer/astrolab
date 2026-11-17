<!--
  Crop editor: a box at any angle on the picture the crop node frames.

  The server owns the geometry (api.framing / api.framingParams): it turns
  the stored params into a box on this canvas, and a drawn box into params,
  on the sky when the canvas is plate-solved. A sky box keeps framing the
  same stars when a new night grows or shifts the combined canvas.

  Here the box lives in full-size image pixels of the displayed canvas
  (FITS row 0 at the bottom, as every preview and the saved picture show
  it): centre, width, height, angle clockwise. The SVG overlay uses the
  same units (viewBox = image size), so the drawing needs no conversions.

  - drag on empty picture: a new box (at the current angle)
  - drag the box: move it; drag a handle: resize from the opposite side
  - drag the knob above the box: turn it, snapping to 45 degree steps and
    to north up / east / south / west within 3 degrees (hold Alt for free)
  Each drag commits on release.
-->
<script lang="ts">
  import { api, type FramingBox, type FramingInfo } from '$lib/api';

  interface Props {
    /** Preview of the image the crop node frames (stretched for display). */
    previewUrl: string | null;
    /** The crop node's input: the image the box is drawn on. */
    hash: string | undefined;
    port: string;
    /** Current crop params (defaults + overrides). */
    params: Record<string, unknown>;
    /** New params for the box (replace every framing key), or null to reset. */
    onparams: (next: Record<string, number> | null) => void;
  }

  const { previewUrl, hash, port, params, onparams }: Props = $props();

  let info = $state<FramingInfo | null>(null);
  let error = $state<string | null>(null);
  let box = $state<FramingBox | null>(null);
  let mode = $state<'idle' | 'new' | 'move' | 'resize' | 'rotate'>('idle');
  let busy = $state(false);

  const paramsKey = $derived(JSON.stringify(params));
  let fetchSeq = 0;
  $effect(() => {
    const h = hash;
    const p = JSON.parse(paramsKey) as Record<string, unknown>;
    if (!h) {
      info = null;
      return;
    }
    const seq = ++fetchSeq;
    api
      .framing(h, port, p)
      .then((next) => {
        if (seq !== fetchSeq) return;
        info = next;
        error = null;
        if (mode === 'idle') box = next.box ? { ...next.box } : null;
      })
      .catch((e) => {
        if (seq === fetchSeq) error = e instanceof Error ? e.message : String(e);
      });
  });

  // --- geometry -----------------------------------------------------------
  type P = { x: number; y: number };
  const rad = (d: number) => (d * Math.PI) / 180;
  const norm = (a: number) => ((a % 360) + 360) % 360;
  /** Image-pixel vector from box-local (x right, y down, before turning). */
  function toWorld(v: P, angle: number): P {
    const c = Math.cos(rad(angle)), s = Math.sin(rad(angle));
    return { x: v.x * c - v.y * s, y: v.x * s + v.y * c };
  }
  function toLocal(v: P, angle: number): P {
    return toWorld(v, -angle);
  }

  let svg: SVGSVGElement | null = $state(null);
  let shownWidth = $state(1);
  /** Image pixels per screen pixel, for handle sizes. */
  const k = $derived(info ? info.width / Math.max(shownWidth, 1) : 1);

  function pointer(e: PointerEvent): P {
    if (!svg || !info) return { x: 0, y: 0 };
    const r = svg.getBoundingClientRect();
    return {
      x: ((e.clientX - r.left) / r.width) * info.width,
      y: ((e.clientY - r.top) / r.height) * info.height
    };
  }

  // --- snapping -----------------------------------------------------------
  const SNAP = 3;
  function snaps(): { angle: number; label: string }[] {
    const out = [0, 45, 90, 135, 180, 225, 270, 315].map((a) => ({ angle: a, label: '' }));
    const n = info?.north_angle;
    if (n != null) {
      // The picture's up direction at north / east / south / west (east
      // sits 90 degrees anticlockwise of north on the sky as displayed).
      const names: [number, string][] = [[0, 'north up'], [270, 'east up'], [180, 'south up'], [90, 'west up']];
      for (const [off, label] of names) out.push({ angle: norm(n + off), label });
    }
    return out;
  }
  function snap(a: number, free: boolean): number {
    if (free) return norm(a);
    let best = norm(a), dist = SNAP;
    for (const s of snaps()) {
      const d = Math.abs(((a - s.angle + 540) % 360) - 180);
      if (d <= dist) { dist = d; best = s.angle; }
    }
    return norm(best);
  }
  /** The orientation the box is at, in words ("north up"), if any. */
  const facing = $derived.by(() => {
    if (!box || info?.north_angle == null) return '';
    for (const s of snaps()) {
      if (s.label && Math.abs(((box.angle - s.angle + 540) % 360) - 180) < 0.05) return s.label;
    }
    return '';
  });

  // --- dragging -----------------------------------------------------------
  let start: { p: P; box: FramingBox } | null = null;
  let handle: P = { x: 0, y: 0 };

  function capture(e: PointerEvent) {
    e.stopPropagation();
    e.preventDefault();
    svg?.setPointerCapture?.(e.pointerId);
  }
  function startNew(e: PointerEvent) {
    if (!info || mode !== 'idle') return;
    capture(e);
    mode = 'new';
    const p = pointer(e);
    const angle = box?.angle ?? 0;
    start = { p, box: { cx: p.x, cy: p.y, width: 0, height: 0, angle } };
    box = { ...start.box };
  }
  function startMove(e: PointerEvent) {
    if (!box) return;
    capture(e);
    mode = 'move';
    start = { p: pointer(e), box: { ...box } };
  }
  function startResize(e: PointerEvent, sx: number, sy: number) {
    if (!box) return;
    capture(e);
    mode = 'resize';
    handle = { x: sx, y: sy };
    start = { p: pointer(e), box: { ...box } };
  }
  function startRotate(e: PointerEvent) {
    if (!box) return;
    capture(e);
    mode = 'rotate';
    start = { p: pointer(e), box: { ...box } };
  }

  function onMove(e: PointerEvent) {
    if (mode === 'idle' || !start || !info) return;
    const p = pointer(e);
    const b0 = start.box;
    if (mode === 'new') {
      const d = toLocal({ x: p.x - start.p.x, y: p.y - start.p.y }, b0.angle);
      const mid = toWorld({ x: d.x / 2, y: d.y / 2 }, b0.angle);
      box = { cx: start.p.x + mid.x, cy: start.p.y + mid.y, width: Math.abs(d.x), height: Math.abs(d.y), angle: b0.angle };
    } else if (mode === 'move') {
      const cx = Math.min(Math.max(b0.cx + p.x - start.p.x, 0), info.width);
      const cy = Math.min(Math.max(b0.cy + p.y - start.p.y, 0), info.height);
      box = { ...b0, cx, cy };
    } else if (mode === 'resize') {
      // The opposite side stays put; the pointer is the new edge or corner.
      const anchor = toWorld({ x: (-handle.x * b0.width) / 2, y: (-handle.y * b0.height) / 2 }, b0.angle);
      const ax = b0.cx + anchor.x, ay = b0.cy + anchor.y;
      const d = toLocal({ x: p.x - ax, y: p.y - ay }, b0.angle);
      const half = toWorld({ x: handle.x ? d.x / 2 : 0, y: handle.y ? d.y / 2 : 0 }, b0.angle);
      box = {
        cx: ax + half.x,
        cy: ay + half.y,
        width: handle.x ? Math.abs(d.x) : b0.width,
        height: handle.y ? Math.abs(d.y) : b0.height,
        angle: b0.angle
      };
    } else if (mode === 'rotate') {
      const a = (Math.atan2(p.x - b0.cx, -(p.y - b0.cy)) * 180) / Math.PI;
      box = { ...b0, angle: snap(a, e.altKey) };
    }
  }

  function onUp() {
    if (mode === 'idle') return;
    const was = mode;
    mode = 'idle';
    start = null;
    if (!box || !info) return;
    // A click, not a drag: keep what was there.
    const min = 0.01 * Math.min(info.width, info.height);
    if (was === 'new' && (box.width < min || box.height < min)) {
      box = info.box ? { ...info.box } : null;
      return;
    }
    commit(box);
  }

  async function commit(b: FramingBox) {
    if (!hash) return;
    busy = true;
    try {
      onparams(await api.framingParams(hash, port, { ...b, angle: norm(b.angle) }));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  function turn(by: number) {
    if (!box) return;
    box = { ...box, angle: norm(box.angle + by) };
    commit(box);
  }
  function northUp() {
    if (!box || info?.north_angle == null) return;
    box = { ...box, angle: norm(info.north_angle) };
    commit(box);
  }
  function setAngle(v: string) {
    const a = Number(v);
    if (!box || !Number.isFinite(a)) return;
    box = { ...box, angle: norm(a) };
    commit(box);
  }
  function reset() {
    box = null;
    onparams(null);
  }

  // --- display ------------------------------------------------------------
  const corners = $derived.by(() => {
    if (!box) return '';
    const b = box;
    return [[-1, -1], [1, -1], [1, 1], [-1, 1]]
      .map(([sx, sy]) => {
        const v = toWorld({ x: (sx * b.width) / 2, y: (sy * b.height) / 2 }, b.angle);
        return `${b.cx + v.x},${b.cy + v.y}`;
      })
      .join(' ');
  });
  const HANDLES: [number, number][] = [[-1, -1], [0, -1], [1, -1], [1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0]];
  const size = $derived.by(() => {
    if (!box || !info) return '';
    if (info.arcsec_per_px) {
      const f = info.arcsec_per_px / 60;
      const deg = (v: number) => (v >= 60 ? `${(v / 60).toFixed(2)}°` : `${v.toFixed(0)}′`);
      return `${deg(box.width * f)} × ${deg(box.height * f)}`;
    }
    return `${Math.round(box.width)} × ${Math.round(box.height)} px`;
  });
</script>

<div class="editor">
  {#if !previewUrl || !hash}
    <div class="placeholder">
      <p class="muted small">Render the picture once first: the crop is drawn on the combined stack.</p>
    </div>
  {:else if !info}
    <div class="placeholder"><p class="muted small">{error ?? 'Loading…'}</p></div>
  {:else}
    <div
      class="viewport"
      style:aspect-ratio="{info.width} / {info.height}"
      style:width="min(100%, calc(max(320px, 100vh - 330px) * {info.width / info.height}))"
      bind:clientWidth={shownWidth}
    >
      <img class="bg" src={previewUrl} alt="" draggable="false" />
      <svg
        bind:this={svg}
        viewBox="0 0 {info.width} {info.height}"
        preserveAspectRatio="none"
        role="img"
        aria-label="Crop picker"
        onpointerdown={startNew}
        onpointermove={onMove}
        onpointerup={onUp}
        onpointercancel={onUp}
      >
        {#if box && box.width > 0 && box.height > 0}
          <path
            class="mask"
            fill-rule="evenodd"
            d="M0,0 H{info.width} V{info.height} H0 Z M{corners.replaceAll(' ', ' L')} Z"
          />
          <g transform="translate({box.cx} {box.cy}) rotate({box.angle})">
            <rect
              class="body"
              class:dragging={mode !== 'idle'}
              x={-box.width / 2}
              y={-box.height / 2}
              width={box.width}
              height={box.height}
              onpointerdown={startMove}
              role="button"
              tabindex="-1"
              aria-label="Crop box: drag to move"
            />
            {#each [1, 2] as i (i)}
              <line class="third" x1={-box.width / 2 + (i * box.width) / 3} x2={-box.width / 2 + (i * box.width) / 3} y1={-box.height / 2} y2={box.height / 2} />
              <line class="third" y1={-box.height / 2 + (i * box.height) / 3} y2={-box.height / 2 + (i * box.height) / 3} x1={-box.width / 2} x2={box.width / 2} />
            {/each}
            <line class="stem" x1="0" x2="0" y1={-box.height / 2} y2={-box.height / 2 - 26 * k} />
            <circle
              class="knob"
              cx="0"
              cy={-box.height / 2 - 26 * k}
              r={8 * k}
              onpointerdown={startRotate}
              role="button"
              tabindex="-1"
              aria-label="Turn the crop"
            />
            {#each HANDLES as [sx, sy] (`${sx},${sy}`)}
              <rect
                class="h"
                x={(sx * box.width) / 2 - 7 * k}
                y={(sy * box.height) / 2 - 7 * k}
                width={14 * k}
                height={14 * k}
                onpointerdown={(e) => startResize(e, sx, sy)}
                role="button"
                tabindex="-1"
                aria-label="Resize"
              />
            {/each}
          </g>
        {/if}
      </svg>
    </div>

    <div class="controls">
      {#if box}
        <span class="legend small">
          {size}
          · <label class="angle">
            <input
              type="number"
              step="1"
              value={Math.round(box.angle * 10) / 10}
              onchange={(e) => setAngle((e.currentTarget as HTMLInputElement).value)}
            />°
          </label>
          {#if facing}<span class="facing">{facing}</span>{/if}
        </span>
        <button type="button" class="hbtn" onclick={() => turn(-90)} title="Turn 90° anticlockwise">⟲ 90°</button>
        <button type="button" class="hbtn" onclick={() => turn(90)} title="Turn 90° clockwise">⟳ 90°</button>
        {#if info.north_angle != null}
          <button type="button" class="hbtn" onclick={northUp}>North up</button>
        {/if}
      {:else}
        <span class="muted small">Drag on the picture to frame it.</span>
      {/if}
      <span class="spacer"></span>
      {#if busy}<span class="muted small">saving…</span>{/if}
      <button type="button" class="hbtn ghost" onclick={reset} disabled={!box && !info.box}>Reset</button>
    </div>
    {#if error}<p class="err small">{error}</p>{/if}
    {#if !info.solved}
      <p class="muted small">No plate solve on this image: the box is kept in pixels, not on the sky.</p>
    {/if}
  {/if}
</div>

<style>
  .editor {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    min-width: 0;
  }
  .small { font-size: 0.8rem; }
  .muted { color: var(--fg-mute); }
  .err { color: var(--bad); margin: 0; }
  .placeholder {
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
  }
  .viewport {
    position: relative;
    margin: 0 auto;
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    user-select: none;
    -webkit-user-select: none;
    touch-action: none;
  }
  .bg, svg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
  }
  .bg { pointer-events: none; }
  svg { cursor: crosshair; overflow: visible; }
  .mask { fill: rgba(0, 0, 0, 0.55); pointer-events: none; }
  .body {
    fill: transparent;
    stroke: var(--accent);
    stroke-width: 1.5px;
    vector-effect: non-scaling-stroke;
    cursor: move;
  }
  .body.dragging { stroke-width: 2px; }
  .third {
    stroke: rgba(255, 255, 255, 0.3);
    stroke-width: 1px;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .stem {
    stroke: var(--accent);
    stroke-width: 1px;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
  }
  .knob {
    fill: var(--accent);
    stroke: rgba(0, 0, 0, 0.6);
    vector-effect: non-scaling-stroke;
    cursor: grab;
  }
  .h {
    fill: var(--accent);
    stroke: rgba(0, 0, 0, 0.6);
    vector-effect: non-scaling-stroke;
    cursor: pointer;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .spacer { flex: 1; }
  .legend {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }
  .angle input {
    width: 4.2rem;
    background: var(--bg-elev-2);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.1rem 0.3rem;
    font: inherit;
  }
  .facing {
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.7rem;
    background: var(--accent-soft);
    color: var(--accent);
    border: 1px solid var(--accent);
  }
  .hbtn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--accent);
    padding: 0.2rem 0.65rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .hbtn:hover:not(:disabled) {
    background: rgba(94, 234, 212, 0.08);
    border-color: var(--accent);
  }
  .hbtn:disabled { opacity: 0.4; cursor: not-allowed; }
  .hbtn.ghost { color: var(--fg-mute); }
</style>
