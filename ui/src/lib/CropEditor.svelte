<!--
  Interactive crop picker. Renders the upstream stretch preview in a fixed
  16:9 viewport and lets the user drag a rectangle to define the crop. The
  rectangle is stored in normalized coords (0..1 of the input frame's spatial
  dims) so it stays valid across resample-mode flips.

  Coords flow: drag updates a local `box` ($state) in real time; we only call
  onchange (which the parent debounces and PATCHes) on pointerup. This keeps
  the rectangle responsive without spawning a job per drag pixel.

  Three drag modes:
    - Click+drag on empty area  → start a fresh box from that corner
    - Drag the body of the box  → translate the existing box
    - Drag a corner/edge handle → resize from that anchor

  Coords are clamped to [0,1] so the box can't escape the frame.
-->
<script lang="ts">
  type Box = { x: number; y: number; w: number; h: number };

  interface Props {
    /** Preview URL to use as the canvas (the upstream stretch output PNG). */
    previewUrl: string | null;
    /** Current normalized values from the project's overrides + defaults. */
    enabled: boolean;
    x: number;
    y: number;
    width: number;
    height: number;
    /** Cost pill content for the form header. */
    costLabel: string;
    /** Emit a partial overrides map for the parent to merge. */
    onchange: (next: { enabled: boolean; x: number; y: number; width: number; height: number }) => void;
    /** Reset the override back to template default (full frame, disabled). */
    onreset: () => void;
  }

  const { previewUrl, enabled, x, y, width, height, costLabel, onchange, onreset }: Props = $props();

  // Local working box. Seed from props; commit (call onchange) on pointerup.
  // We don't reactively re-seed on every prop change because the user is
  // mid-drag — that would yank the rectangle out from under them. Instead
  // we seed on mount + when previewUrl swaps (i.e., the upstream image
  // changed, so the previous box is stale anyway). Going through $derived
  // here keeps Svelte 5 happy with prop reads inside the effect.
  const propBox = $derived<Box>({ x, y, w: width, h: height });
  const previewKey = $derived(previewUrl);
  // svelte-ignore state_referenced_locally
  let box = $state<Box>({ x, y, w: width, h: height });
  // svelte-ignore state_referenced_locally
  let lastPreviewKey: string | null = previewUrl;
  $effect(() => {
    if (previewKey !== lastPreviewKey) {
      lastPreviewKey = previewKey;
      box = { ...propBox };
    }
  });

  let viewport: HTMLDivElement | null = $state(null);
  let mode = $state<'idle' | 'new' | 'move' | 'resize'>('idle');
  let resizeAnchor = $state<{ ax: number; ay: number } | null>(null);
  let dragStart = $state<{ px: number; py: number; bx: number; by: number; bw: number; bh: number } | null>(null);

  function clamp01(v: number): number {
    return Math.max(0, Math.min(1, v));
  }

  function eventToFrac(e: PointerEvent): { fx: number; fy: number } {
    if (!viewport) return { fx: 0, fy: 0 };
    const rect = viewport.getBoundingClientRect();
    return {
      fx: clamp01((e.clientX - rect.left) / rect.width),
      fy: clamp01((e.clientY - rect.top) / rect.height)
    };
  }

  // Handle picker: 8 grab handles around the box. Each maps to which corner
  // stays fixed during the resize (the "anchor"). E.g., dragging the SE
  // handle keeps the NW corner pinned.
  type Handle = 'nw' | 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w';
  const HANDLE_ANCHOR: Record<Handle, { ax: number; ay: number }> = {
    nw: { ax: 1, ay: 1 },
    n: { ax: 0, ay: 1 },
    ne: { ax: 0, ay: 1 },
    e: { ax: 0, ay: 0 },
    se: { ax: 0, ay: 0 },
    s: { ax: 1, ay: 0 },
    sw: { ax: 1, ay: 0 },
    w: { ax: 1, ay: 0 }
  };

  function startResize(e: PointerEvent, handle: Handle) {
    e.stopPropagation();
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    mode = 'resize';
    resizeAnchor = HANDLE_ANCHOR[handle];
  }

  function startMove(e: PointerEvent) {
    e.stopPropagation();
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    mode = 'move';
    const { fx, fy } = eventToFrac(e);
    dragStart = { px: fx, py: fy, bx: box.x, by: box.y, bw: box.w, bh: box.h };
  }

  function startNew(e: PointerEvent) {
    if (mode !== 'idle') return;
    if (!viewport) return;
    e.preventDefault();
    viewport.setPointerCapture?.(e.pointerId);
    mode = 'new';
    const { fx, fy } = eventToFrac(e);
    dragStart = { px: fx, py: fy, bx: fx, by: fy, bw: 0, bh: 0 };
    box = { x: fx, y: fy, w: 0, h: 0 };
  }

  function onPointerMove(e: PointerEvent) {
    if (mode === 'idle') return;
    const { fx, fy } = eventToFrac(e);
    if (mode === 'new') {
      if (!dragStart) return;
      const x0 = Math.min(dragStart.px, fx);
      const y0 = Math.min(dragStart.py, fy);
      const x1 = Math.max(dragStart.px, fx);
      const y1 = Math.max(dragStart.py, fy);
      box = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    } else if (mode === 'move') {
      if (!dragStart) return;
      const dx = fx - dragStart.px;
      const dy = fy - dragStart.py;
      // Translate, clamping so the box stays inside the frame.
      const nx = clamp01(dragStart.bx + dx);
      const ny = clamp01(dragStart.by + dy);
      box = {
        x: Math.min(nx, 1 - dragStart.bw),
        y: Math.min(ny, 1 - dragStart.bh),
        w: dragStart.bw,
        h: dragStart.bh
      };
    } else if (mode === 'resize') {
      if (!resizeAnchor) return;
      // The anchor corner stays fixed; the dragging point becomes the
      // opposite corner. ax/ay are 0 (left/top) or 1 (right/bottom).
      const ax = resizeAnchor.ax;
      const ay = resizeAnchor.ay;
      const anchorX = box.x + ax * box.w;
      const anchorY = box.y + ay * box.h;
      const x0 = Math.min(anchorX, fx);
      const y0 = Math.min(anchorY, fy);
      const x1 = Math.max(anchorX, fx);
      const y1 = Math.max(anchorY, fy);
      box = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    }
  }

  function commit() {
    // Drop tiny accidental boxes (single-click on empty space) — under 1%
    // either dimension is almost certainly a misclick, not a real crop.
    const tooSmall = box.w < 0.01 || box.h < 0.01;
    if (tooSmall) {
      box = { x, y, w: width, h: height };
      return;
    }
    onchange({
      enabled: true,
      x: round4(box.x),
      y: round4(box.y),
      width: round4(box.w),
      height: round4(box.h)
    });
  }

  function round4(v: number): number {
    return Math.round(v * 10000) / 10000;
  }

  function onPointerUp() {
    if (mode === 'idle') return;
    const wasNew = mode === 'new';
    const wasResize = mode === 'resize';
    const wasMove = mode === 'move';
    mode = 'idle';
    dragStart = null;
    resizeAnchor = null;
    if (wasNew || wasResize || wasMove) commit();
  }

  function reset() {
    box = { x: 0, y: 0, w: 1, h: 1 };
    onreset();
  }

  function toggleEnabled() {
    onchange({
      enabled: !enabled,
      x: box.x,
      y: box.y,
      width: box.w,
      height: box.h
    });
  }

  // Display-only percentages (rounded to whole percent) for the legend.
  const pctX = $derived(Math.round(box.x * 100));
  const pctY = $derived(Math.round(box.y * 100));
  const pctW = $derived(Math.round(box.w * 100));
  const pctH = $derived(Math.round(box.h * 100));

  // Disable the rectangle visuals when the box is degenerate (full frame
  // or zero size) so the "no crop" state doesn't draw a bizarre overlay.
  const showBox = $derived(box.w > 0 && box.h > 0 && (box.w < 0.999 || box.h < 0.999));
</script>

<div class="form">
  <header class="form-head">
    <span class="cost-pill">{costLabel}</span>
    <span class="muted small">crop</span>
    <span class="status-pill" class:on={enabled} class:off={!enabled}>
      {enabled ? 'on' : 'off'}
    </span>
    <button type="button" class="hbtn" onclick={toggleEnabled}>
      {enabled ? 'Disable' : 'Enable'}
    </button>
    <button type="button" class="hbtn ghost" onclick={reset} disabled={!enabled && box.w >= 0.999 && box.h >= 0.999}>
      Reset
    </button>
  </header>

  {#if !previewUrl}
    <div class="placeholder">
      <p class="muted small">
        Run the pipeline to the stretch step first — the crop picker
        needs the stretched preview to drag a rectangle on.
      </p>
    </div>
  {:else}
    <div
      class="viewport"
      bind:this={viewport}
      onpointerdown={startNew}
      onpointermove={onPointerMove}
      onpointerup={onPointerUp}
      onpointercancel={onPointerUp}
      role="img"
      aria-label="Crop picker"
    >
      <img class="bg" src={previewUrl} alt="" draggable="false" />

      {#if showBox}
        <!-- Dim mask everywhere outside the box. Four rectangles placed
             around the crop rect so the inside stays bright. -->
        <div class="mask top" style:height="{box.y * 100}%"></div>
        <div
          class="mask bottom"
          style:height="{(1 - box.y - box.h) * 100}%"
        ></div>
        <div
          class="mask left"
          style:top="{box.y * 100}%"
          style:height="{box.h * 100}%"
          style:width="{box.x * 100}%"
        ></div>
        <div
          class="mask right"
          style:top="{box.y * 100}%"
          style:height="{box.h * 100}%"
          style:width="{(1 - box.x - box.w) * 100}%"
        ></div>

        <!-- The crop box itself. Drag the body to translate; corner/edge
             handles to resize. -->
        <div
          class="cropbox"
          class:dragging={mode !== 'idle'}
          style:left="{box.x * 100}%"
          style:top="{box.y * 100}%"
          style:width="{box.w * 100}%"
          style:height="{box.h * 100}%"
          onpointerdown={startMove}
          role="button"
          tabindex="0"
          aria-label="Crop region — drag to move"
        >
          <!-- 8 handles. North/east/etc. correspond to which corner moves. -->
          <span class="h nw" onpointerdown={(e) => startResize(e, 'nw')} role="button" tabindex="-1" aria-label="resize from nw"></span>
          <span class="h n" onpointerdown={(e) => startResize(e, 'n')} role="button" tabindex="-1" aria-label="resize from n"></span>
          <span class="h ne" onpointerdown={(e) => startResize(e, 'ne')} role="button" tabindex="-1" aria-label="resize from ne"></span>
          <span class="h e" onpointerdown={(e) => startResize(e, 'e')} role="button" tabindex="-1" aria-label="resize from e"></span>
          <span class="h se" onpointerdown={(e) => startResize(e, 'se')} role="button" tabindex="-1" aria-label="resize from se"></span>
          <span class="h s" onpointerdown={(e) => startResize(e, 's')} role="button" tabindex="-1" aria-label="resize from s"></span>
          <span class="h sw" onpointerdown={(e) => startResize(e, 'sw')} role="button" tabindex="-1" aria-label="resize from sw"></span>
          <span class="h w" onpointerdown={(e) => startResize(e, 'w')} role="button" tabindex="-1" aria-label="resize from w"></span>

          <!-- Rule-of-thirds gridlines so the user can compose. -->
          <span class="grid-v one"></span>
          <span class="grid-v two"></span>
          <span class="grid-h one"></span>
          <span class="grid-h two"></span>
        </div>
      {/if}
    </div>

    <p class="legend muted small">
      {#if showBox}
        x {pctX}% · y {pctY}% · {pctW}% × {pctH}%
        {#if !enabled}
          <span class="warn-pill">crop is off — enable to apply</span>
        {/if}
      {:else}
        Click and drag inside the preview to define a crop.
      {/if}
    </p>
  {/if}
</div>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    min-width: 0;
  }
  .form-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .small { font-size: 0.8rem; }
  .muted { color: var(--fg-mute); }

  .cost-pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    background: rgba(94, 211, 168, 0.18);
    color: var(--good);
    border: 1px solid var(--good);
  }
  .status-pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-pill.on {
    background: var(--accent-soft);
    color: var(--accent);
    border: 1px solid var(--accent);
  }
  .status-pill.off {
    background: rgba(255, 255, 255, 0.06);
    color: var(--fg-mute);
    border: 1px solid var(--border);
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
  .hbtn.ghost:hover:not(:disabled) {
    color: var(--fg);
    background: rgba(255, 255, 255, 0.04);
  }

  .placeholder {
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 1rem;
    text-align: center;
  }

  .viewport {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    cursor: crosshair;
    user-select: none;
    -webkit-user-select: none;
    touch-action: none;
  }
  .bg {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
    pointer-events: none;
  }

  /* Dim mask outside the crop box. ~55% darken keeps the cropped area
     legible while making it obvious what's getting trimmed. */
  .mask {
    position: absolute;
    background: rgba(0, 0, 0, 0.55);
    pointer-events: none;
  }
  .mask.top { top: 0; left: 0; right: 0; }
  .mask.bottom { bottom: 0; left: 0; right: 0; }
  .mask.left { left: 0; }
  .mask.right { right: 0; }

  .cropbox {
    position: absolute;
    box-shadow:
      0 0 0 1px var(--accent),
      0 0 0 2px rgba(0, 0, 0, 0.35);
    cursor: move;
  }
  .cropbox.dragging {
    box-shadow:
      0 0 0 1px var(--accent),
      0 0 12px rgba(94, 234, 212, 0.4);
  }

  /* Rule-of-thirds. Subtle white lines, only visible inside the box. */
  .grid-v, .grid-h {
    position: absolute;
    background: rgba(255, 255, 255, 0.25);
    pointer-events: none;
  }
  .grid-v { top: 0; bottom: 0; width: 1px; }
  .grid-v.one { left: 33.333%; }
  .grid-v.two { left: 66.666%; }
  .grid-h { left: 0; right: 0; height: 1px; }
  .grid-h.one { top: 33.333%; }
  .grid-h.two { top: 66.666%; }

  /* Resize handles — small accent squares at the 8 compass points. */
  .h {
    position: absolute;
    width: 12px;
    height: 12px;
    background: var(--accent);
    border: 1px solid rgba(0, 0, 0, 0.6);
    border-radius: 2px;
  }
  .h.nw { top: -6px; left: -6px; cursor: nwse-resize; }
  .h.n  { top: -6px; left: 50%; transform: translateX(-50%); cursor: ns-resize; }
  .h.ne { top: -6px; right: -6px; cursor: nesw-resize; }
  .h.e  { top: 50%; right: -6px; transform: translateY(-50%); cursor: ew-resize; }
  .h.se { bottom: -6px; right: -6px; cursor: nwse-resize; }
  .h.s  { bottom: -6px; left: 50%; transform: translateX(-50%); cursor: ns-resize; }
  .h.sw { bottom: -6px; left: -6px; cursor: nesw-resize; }
  .h.w  { top: 50%; left: -6px; transform: translateY(-50%); cursor: ew-resize; }

  .legend {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-variant-numeric: tabular-nums;
    flex-wrap: wrap;
  }
  .warn-pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    background: color-mix(in oklab, var(--bad) 18%, transparent);
    color: var(--bad);
    border: 1px solid color-mix(in oklab, var(--bad) 50%, transparent);
  }
</style>
