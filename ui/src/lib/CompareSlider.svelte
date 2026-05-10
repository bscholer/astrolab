<!--
  Reusable A/B wipe slider. The /compare route uses it for full-page
  comparisons; the project detail page mounts it inside a modal for
  in-place history-version compares.

  Both images render absolutely-positioned at object-fit: contain so they
  share the same painted box regardless of source dimensions. The B
  layer is clipped from the right via inline clip-path, so dragging the
  divider right reveals more of A.

  Pointer handlers cover mouse + touch (PointerEvent unifies both);
  arrow keys nudge by 2% (or 10% with shift); Home/End jump to the
  edges. The viewport itself is the focus target so keyboard users can
  drag immediately after tabbing in.
-->
<script lang="ts">
  type Props = {
    aSrc: string;
    bSrc: string;
    aLabel?: string;
    bLabel?: string;
    /** Initial wipe position in 0..100. Defaults to 50 (clean half/half). */
    initialPos?: number;
    /** Override the default 16:9 viewport aspect. */
    aspect?: string;
  };

  let {
    aSrc,
    bSrc,
    aLabel = 'A',
    bLabel = 'B',
    initialPos = 50,
    aspect = '16 / 9',
  }: Props = $props();

  // initialPos is a one-shot seed; the warning's heuristic is wrong here.
  // svelte-ignore state_referenced_locally
  let pos = $state(initialPos);
  let dragging = $state(false);
  let viewport: HTMLDivElement | null = $state(null);

  function clamp(n: number, lo: number, hi: number): number {
    return Math.max(lo, Math.min(hi, n));
  }

  function setPosFromClientX(clientX: number) {
    if (!viewport) return;
    const rect = viewport.getBoundingClientRect();
    pos = clamp(((clientX - rect.left) / rect.width) * 100, 0, 100);
  }

  function onPointerDown(e: PointerEvent) {
    dragging = true;
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setPosFromClientX(e.clientX);
  }
  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    setPosFromClientX(e.clientX);
  }
  function onPointerUp() {
    dragging = false;
  }
  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'ArrowLeft') {
      pos = clamp(pos - (e.shiftKey ? 10 : 2), 0, 100);
      e.preventDefault();
    } else if (e.key === 'ArrowRight') {
      pos = clamp(pos + (e.shiftKey ? 10 : 2), 0, 100);
      e.preventDefault();
    } else if (e.key === 'Home') {
      pos = 0;
      e.preventDefault();
    } else if (e.key === 'End') {
      pos = 100;
      e.preventDefault();
    }
  }
</script>

<div
  class="viewport"
  bind:this={viewport}
  role="slider"
  tabindex="0"
  aria-label="Compare slider"
  aria-valuemin="0"
  aria-valuemax="100"
  aria-valuenow={Math.round(pos)}
  style:aspect-ratio={aspect}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
  onkeydown={onKeyDown}
>
  <img class="layer" src={aSrc} alt="A: {aLabel}" draggable="false" />
  <img
    class="layer top"
    style:clip-path="inset(0 {100 - pos}% 0 0)"
    src={bSrc}
    alt="B: {bLabel}"
    draggable="false"
  />
  <div class="divider" class:dragging style:left="{pos}%">
    <div class="handle" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="15 18 9 12 15 6" />
        <polyline points="9 6 15 12 9 18" transform="translate(8,0)" />
      </svg>
    </div>
  </div>
  <span class="corner a">A</span>
  <span class="corner b">B</span>
</div>

<style>
  .viewport {
    position: relative;
    width: 100%;
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    overflow: hidden;
    cursor: ew-resize;
    user-select: none;
    -webkit-user-select: none;
    box-shadow: var(--shadow);
  }
  .viewport:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .layer {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }

  .divider {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: linear-gradient(180deg, var(--accent), var(--bad));
    box-shadow: 0 0 12px rgba(94, 234, 212, 0.4);
    pointer-events: none;
  }
  .handle {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 32px;
    height: 32px;
    border-radius: 999px;
    background: linear-gradient(135deg, var(--accent), var(--bad));
    color: var(--accent-ink);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-shadow:
      0 0 0 2px var(--bg),
      0 0 16px rgba(94, 234, 212, 0.4);
    transition: transform 160ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .divider.dragging .handle {
    transform: translate(-50%, -50%) scale(1.12);
  }

  .corner {
    position: absolute;
    top: 8px;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.92);
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    pointer-events: none;
  }
  .corner.a { left: 8px; border: 1px solid var(--accent); }
  .corner.b { right: 8px; border: 1px solid var(--bad); }
</style>
