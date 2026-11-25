<!--
  The wide-camera panorama, flat (equirectangular), with the sky mask, the horizon traced
  from it, and the patches Run all night has learned. Drag to look around; it wraps at
  north. Paint with the sky or not-sky brush to fix the mask, then save.

  Image: column 0 is azimuth 0, azimuth grows right; row 0 is altitude altTop.
  Masks: the whole sphere, MASK_W x MASK_W/2, row 0 is altitude 90.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type ScopeHorizon, type ScopePanoResult, type ScopeSkyPatch } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  let {
    scan,
    result,
  }: {
    scan: string;
    result: ScopePanoResult;
  } = $props();

  const MASK_W = 2048;
  const MASK_H = MASK_W / 2;
  const SKY = 255;
  const GROUND = 0;
  const UNSEEN = 128;
  const EDIT_NONE = 0;
  const EDIT_SKY = 1;
  const EDIT_GROUND = 2;

  type Tool = 'pan' | 'sky' | 'ground' | 'erase';

  let canvas: HTMLCanvasElement;
  let wrap: HTMLDivElement;
  let img: HTMLImageElement | null = null;
  let auto: Uint8Array | null = null;
  let edits: Uint8Array = new Uint8Array(MASK_W * MASK_H);
  let maskCanvas: HTMLCanvasElement | null = null;
  let patches = $state<Record<string, ScopeSkyPatch>>({});
  let horizon = $state<ScopeHorizon | null>(null);

  let tool = $state<Tool>('pan');
  let brushDeg = $state(3);
  let showMask = $state(true);
  let showPatches = $state(true);
  let showHorizon = $state(true);
  let dirty = $state(false);
  let saving = $state(false);
  let loading = $state(true);

  // View: centre azimuth and altitude, and zoom in canvas pixels per degree.
  let az0 = $state(180);
  let alt0 = $state(20);
  let ppd = $state(4);
  let width = $state(800);
  let height = $state(380);

  const altTop = $derived(result.alt_top);
  const altBottom = $derived(result.alt_bottom);

  function wrapAz(a: number) {
    return ((a % 360) + 360) % 360;
  }

  function toScreen(alt: number, az: number): [number, number] {
    let d = wrapAz(az - az0);
    if (d > 180) d -= 360;
    return [width / 2 + d * ppd, height / 2 + (alt0 - alt) * ppd];
  }

  function toSky(x: number, y: number): { alt: number; az: number } {
    return { alt: alt0 - (y - height / 2) / ppd, az: wrapAz(az0 + (x - width / 2) / ppd) };
  }

  function clampView() {
    const minPpd = width / 360;
    ppd = Math.max(minPpd, Math.min(40, ppd));
    const half = height / 2 / ppd;
    const hi = Math.max(altTop - half, altBottom + half);
    const lo = Math.min(altBottom + half, altTop - half);
    alt0 = Math.max(lo, Math.min(hi, alt0));
  }

  function loadImage(src: string): Promise<HTMLImageElement> {
    return new Promise((resolve, reject) => {
      const im = new Image();
      im.onload = () => resolve(im);
      im.onerror = () => reject(new Error(`could not load ${src}`));
      im.src = src;
    });
  }

  async function loadMask(layer: 'auto' | 'edits'): Promise<Uint8Array | null> {
    try {
      const im = await loadImage(`/api/scope/pano/sky?layer=${layer}&t=${scan}-${Date.now()}`);
      const c = document.createElement('canvas');
      c.width = MASK_W;
      c.height = MASK_H;
      const ctx = c.getContext('2d', { willReadFrequently: true })!;
      ctx.drawImage(im, 0, 0);
      const rgba = ctx.getImageData(0, 0, MASK_W, MASK_H).data;
      const out = new Uint8Array(MASK_W * MASK_H);
      for (let i = 0; i < out.length; i++) out[i] = rgba[i * 4];
      return out;
    } catch {
      return null;
    }
  }

  /** Paint the mask overlay: not-sky tinted, hand fixes a little stronger. */
  function paintMask(x0 = 0, y0 = 0, x1 = MASK_W, y1 = MASK_H) {
    if (!maskCanvas) {
      maskCanvas = document.createElement('canvas');
      maskCanvas.width = MASK_W;
      maskCanvas.height = MASK_H;
    }
    const ctx = maskCanvas.getContext('2d')!;
    const w = x1 - x0;
    const h = y1 - y0;
    if (w <= 0 || h <= 0) return;
    const data = ctx.createImageData(w, h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y + y0) * MASK_W + (x + x0);
        const e = edits[i];
        const a = auto ? auto[i] : UNSEEN;
        const value = e === EDIT_SKY ? SKY : e === EDIT_GROUND ? GROUND : a;
        const o = (y * w + x) * 4;
        if (value === GROUND) {
          data.data[o] = 236;
          data.data[o + 1] = 72;
          data.data[o + 2] = 153;
          data.data[o + 3] = e === EDIT_GROUND ? 150 : 95;
        } else if (e === EDIT_SKY) {
          data.data[o] = 56;
          data.data[o + 1] = 189;
          data.data[o + 2] = 248;
          data.data[o + 3] = 70;
        }
      }
    }
    ctx.putImageData(data, x0, y0);
  }

  function draw() {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext('2d')!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#0b0f14';
    ctx.fillRect(0, 0, width, height);
    const span = 360 * ppd;
    const left = width / 2 - wrapAz(az0) * ppd; // screen x of azimuth 0
    for (let k = -1; k <= 1; k++) {
      const x = left + k * span;
      if (x > width || x + span < 0) continue;
      if (img) {
        const y = height / 2 + (alt0 - altTop) * ppd;
        ctx.drawImage(img, x, y, span, (altTop - altBottom) * ppd);
      }
      if (showMask && maskCanvas) {
        ctx.drawImage(maskCanvas, x, height / 2 + (alt0 - 90) * ppd, span, 180 * ppd);
      }
    }
    if (showHorizon && horizon) drawHorizon(ctx);
    if (showPatches && result.compass) drawPatches(ctx);
    drawLabels(ctx);
  }

  function drawHorizon(ctx: CanvasRenderingContext2D) {
    if (!horizon) return;
    ctx.strokeStyle = '#fde047';
    ctx.lineWidth = 1.5;
    const step = horizon.step_deg;
    for (const shift of [-360, 0, 360]) {
      ctx.beginPath();
      let open = false;
      horizon.alt.forEach((alt, i) => {
        if (alt == null) {
          open = false;
          return;
        }
        const az = i * step + step / 2;
        let d = az - wrapAz(az0) + shift;
        const x = width / 2 + d * ppd;
        const y = height / 2 + (alt0 - alt) * ppd;
        if (open) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
        open = true;
      });
      ctx.stroke();
    }
  }

  function drawPatches(ctx: CanvasRenderingContext2D) {
    const colors = { clear: '#38bdf8', blocked: '#f97316', unknown: '#9ca3af' };
    for (const [key, p] of Object.entries(patches)) {
      const [a, z] = key.split('/').map(Number);
      const [x, y] = toScreen(a + 2.5, z + 2.5);
      if (x < -10 || x > width + 10 || y < -10 || y > height + 10) continue;
      ctx.beginPath();
      ctx.arc(x, y, Math.max(2.5, Math.min(6, ppd * 0.9)), 0, Math.PI * 2);
      ctx.fillStyle = colors[p.verdict] ?? colors.unknown;
      ctx.globalAlpha = 0.9;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = 'rgba(0,0,0,0.6)';
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }

  function drawLabels(ctx: CanvasRenderingContext2D) {
    ctx.font = '12px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    const names: [number, string][] = result.compass
      ? [
          [0, 'N'],
          [45, 'NE'],
          [90, 'E'],
          [135, 'SE'],
          [180, 'S'],
          [225, 'SW'],
          [270, 'W'],
          [315, 'NW'],
        ]
      : [0, 90, 180, 270].map((a) => [a, `${a}°`] as [number, string]);
    for (const [az, label] of names) {
      const [x] = toScreen(0, az);
      if (x < 0 || x > width) continue;
      ctx.fillStyle = 'rgba(0,0,0,0.55)';
      ctx.fillRect(x - 14, height - 20, 28, 18);
      ctx.fillStyle = '#e5e7eb';
      ctx.fillText(label, x, height - 4);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (const alt of [0, 30, 60]) {
      const y = height / 2 + (alt0 - alt) * ppd;
      if (y < 10 || y > height - 24) continue;
      ctx.strokeStyle = 'rgba(255,255,255,0.18)';
      ctx.setLineDash([4, 6]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(229,231,235,0.8)';
      ctx.fillText(`${alt}°`, 6, y - 8);
    }
  }

  // --- input ----------------------------------------------------------------

  let dragging = false;
  let lastX = 0;
  let lastY = 0;

  function paintAt(x: number, y: number) {
    const { alt, az } = toSky(x, y);
    const value = tool === 'sky' ? EDIT_SKY : tool === 'ground' ? EDIT_GROUND : EDIT_NONE;
    const pxPerDeg = MASK_W / 360;
    const r = brushDeg * pxPerDeg;
    const cx = (az / 360) * MASK_W;
    const cy = ((90 - alt) / 180) * MASK_H;
    const y0 = Math.max(0, Math.floor(cy - r));
    const y1 = Math.min(MASK_H, Math.ceil(cy + r + 1));
    let minX = Infinity;
    let maxX = -Infinity;
    for (let yy = y0; yy < y1; yy++) {
      for (let dx = -Math.ceil(r); dx <= Math.ceil(r); dx++) {
        if (dx * dx + (yy - cy) * (yy - cy) > r * r) continue;
        const xx = (((Math.round(cx + dx) % MASK_W) + MASK_W) % MASK_W);
        edits[yy * MASK_W + xx] = value;
        minX = Math.min(minX, xx);
        maxX = Math.max(maxX, xx);
      }
    }
    dirty = true;
    if (maxX - minX > MASK_W / 2) paintMask(0, y0, MASK_W, y1); // wrapped around north
    else if (Number.isFinite(minX)) paintMask(minX, y0, maxX + 1, y1);
    draw();
  }

  function pointerDown(e: PointerEvent) {
    dragging = true;
    lastX = e.offsetX;
    lastY = e.offsetY;
    canvas.setPointerCapture(e.pointerId);
    if (tool !== 'pan') paintAt(e.offsetX, e.offsetY);
  }

  function pointerMove(e: PointerEvent) {
    if (!dragging) return;
    if (tool === 'pan') {
      az0 = wrapAz(az0 - (e.offsetX - lastX) / ppd);
      alt0 += (e.offsetY - lastY) / ppd;
      clampView();
      draw();
    } else {
      paintAt(e.offsetX, e.offsetY);
    }
    lastX = e.offsetX;
    lastY = e.offsetY;
  }

  function pointerUp(e: PointerEvent) {
    dragging = false;
    canvas.releasePointerCapture(e.pointerId);
  }

  function wheel(e: WheelEvent) {
    e.preventDefault();
    const before = toSky(e.offsetX, e.offsetY);
    ppd *= Math.exp(-e.deltaY * 0.0015);
    clampView();
    // Keep the point under the cursor in place.
    const after = toSky(e.offsetX, e.offsetY);
    az0 = wrapAz(az0 + before.az - after.az);
    alt0 += before.alt - after.alt;
    clampView();
    draw();
  }

  function zoom(factor: number) {
    ppd *= factor;
    clampView();
    draw();
  }

  // --- saving ---------------------------------------------------------------

  async function save() {
    saving = true;
    try {
      const c = document.createElement('canvas');
      c.width = MASK_W;
      c.height = MASK_H;
      const ctx = c.getContext('2d')!;
      const data = ctx.createImageData(MASK_W, MASK_H);
      for (let i = 0; i < edits.length; i++) {
        const v = edits[i];
        data.data[i * 4] = v;
        data.data[i * 4 + 1] = v;
        data.data[i * 4 + 2] = v;
        data.data[i * 4 + 3] = 255;
      }
      ctx.putImageData(data, 0, 0);
      const blob = await new Promise<Blob>((resolve, reject) =>
        c.toBlob((b) => (b ? resolve(b) : reject(new Error('could not encode the mask'))), 'image/png'),
      );
      const r = await api.putScopePanoSky(blob);
      horizon = r.horizon;
      dirty = false;
      toast.success('Saved. The horizon follows your fixes.');
      draw();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      saving = false;
    }
  }

  async function revert() {
    edits = (await loadMask('edits')) ?? new Uint8Array(MASK_W * MASK_H);
    dirty = false;
    paintMask();
    draw();
  }

  async function redetect() {
    try {
      const r = await api.detectScopePanoSky();
      horizon = r.horizon;
      auto = await loadMask('auto');
      paintMask();
      draw();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  let observer: ResizeObserver | undefined;

  onMount(async () => {
    observer = new ResizeObserver(() => {
      width = wrap.clientWidth;
      height = Math.max(260, Math.min(520, Math.round(width * 0.42)));
      clampView();
      draw();
    });
    observer.observe(wrap);
    try {
      const [im, a, ed, sm, hz] = await Promise.all([
        loadImage(`/api/scope/pano/image?t=${scan}`),
        loadMask('auto'),
        loadMask('edits'),
        api.getScopeSkymapPatches().catch(() => null),
        api.getScopeHorizon(false).catch(() => null),
      ]);
      img = im;
      auto = a;
      if (ed) edits = ed;
      patches = sm?.patches ?? {};
      horizon = hz;
      ppd = Math.max(width / 360, 3);
      alt0 = Math.min(altTop, 25);
      clampView();
      paintMask();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      loading = false;
      draw();
    }
  });

  onDestroy(() => observer?.disconnect());

  $effect(() => {
    // Redraw when a toggle changes.
    void showMask;
    void showPatches;
    void showHorizon;
    draw();
  });
</script>

<div class="pano">
  <div class="toolbar">
    <div class="tools" role="group" aria-label="Tool">
      <button type="button" class:on={tool === 'pan'} onclick={() => (tool = 'pan')}>Look</button>
      <button type="button" class:on={tool === 'sky'} onclick={() => (tool = 'sky')}>Paint sky</button>
      <button type="button" class:on={tool === 'ground'} onclick={() => (tool = 'ground')}>Paint not sky</button>
      <button type="button" class:on={tool === 'erase'} onclick={() => (tool = 'erase')}>Erase fixes</button>
    </div>
    {#if tool !== 'pan'}
      <label class="brush">
        Brush {brushDeg}°
        <input type="range" min="1" max="15" step="1" bind:value={brushDeg} />
      </label>
    {/if}
    <div class="tools">
      <button type="button" onclick={() => zoom(1.4)} aria-label="Zoom in">+</button>
      <button type="button" onclick={() => zoom(1 / 1.4)} aria-label="Zoom out">−</button>
    </div>
  </div>
  <div class="view" bind:this={wrap}>
    <canvas
      bind:this={canvas}
      style="width: {width}px; height: {height}px; cursor: {tool === 'pan' ? 'grab' : 'crosshair'}"
      onpointerdown={pointerDown}
      onpointermove={pointerMove}
      onpointerup={pointerUp}
      onpointercancel={pointerUp}
      onwheel={wheel}
    ></canvas>
    {#if loading}<div class="loading">Loading the panorama…</div>{/if}
  </div>
  <div class="toolbar">
    <label class="check"><input type="checkbox" bind:checked={showMask} /> Not sky in pink</label>
    <label class="check"><input type="checkbox" bind:checked={showHorizon} /> Horizon</label>
    {#if result.compass}
      <label class="check"><input type="checkbox" bind:checked={showPatches} /> Learned patches</label>
    {/if}
    <span class="grow"></span>
    <button type="button" class="btn" onclick={redetect}>Find the sky again</button>
    <button type="button" class="btn" disabled={!dirty} onclick={revert}>Undo fixes</button>
    <button type="button" class="btn primary" disabled={!dirty || saving} onclick={save}>
      {saving ? 'Saving…' : 'Save fixes'}
    </button>
  </div>
</div>

<style>
  .pano {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .view {
    position: relative;
    width: 100%;
    border-radius: 8px;
    overflow: hidden;
    background: #0b0f14;
  }
  canvas {
    display: block;
    touch-action: none;
  }
  .loading {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    color: #9ca3af;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.75rem;
  }
  .tools {
    display: inline-flex;
    border: 1px solid var(--border, #374151);
    border-radius: 6px;
    overflow: hidden;
  }
  .tools button {
    background: transparent;
    color: inherit;
    border: 0;
    padding: 0.3rem 0.6rem;
    font: inherit;
    cursor: pointer;
  }
  .tools button + button {
    border-left: 1px solid var(--border, #374151);
  }
  .tools button.on {
    background: var(--accent-bg, #1e3a5f);
  }
  .brush {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .grow {
    flex: 1;
  }
  .check {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
</style>
