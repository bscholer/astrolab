<!--
  Side-by-side compare with a slider wipe. URL-shareable:
    /compare?a=<project_id>:<seq>&b=<project_id>:<seq>

  We piggyback on /api/gallery for the lookup table — same backend
  shape used by /gallery, so we don't grow the API surface for one
  feature. The wipe overlays B on top of A and uses clip-path to
  reveal more of B as the divider moves right.
-->
<script lang="ts">
  import { page } from '$app/stores';
  import { api, type GalleryEntry } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { templateDisplayName } from '$lib/format';

  let entries = $state<GalleryEntry[] | null>(null);

  type Pick = { project_id: string; seq: number };

  function parsePick(raw: string | null): Pick | null {
    if (!raw) return null;
    const [project_id, seqStr] = raw.split(':');
    const seq = Number.parseInt(seqStr, 10);
    if (!project_id || Number.isNaN(seq)) return null;
    return { project_id, seq };
  }

  const aPick = $derived(parsePick($page.url.searchParams.get('a')));
  const bPick = $derived(parsePick($page.url.searchParams.get('b')));

  const aEntry = $derived.by(() =>
    entries && aPick
      ? entries.find((e) => e.project_id === aPick.project_id && e.seq === aPick.seq) ?? null
      : null
  );
  const bEntry = $derived.by(() =>
    entries && bPick
      ? entries.find((e) => e.project_id === bPick.project_id && e.seq === bPick.seq) ?? null
      : null
  );

  // Slider position in 0..100 (percent of viewport revealing B). 50 by
  // default so the user lands on a clean half-and-half view.
  let pos = $state(50);
  // Whether the user is currently dragging — drives the cursor + a
  // small grow animation on the divider handle.
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

  $effect(() => {
    api.listGallery()
      .then((es) => (entries = es))
      .catch((e) => toast.error(`Couldn't load gallery: ${(e as Error).message}`));
  });

  function entryLabel(e: GalleryEntry): string {
    const friendly = e.target_common_name && e.target_common_name !== e.project_name
      ? `${e.target_common_name} (${e.project_name})`
      : e.project_name;
    return `${friendly} · v${e.seq + 1}`;
  }
</script>

<div class="header">
  <h1>Compare</h1>
  <a href="/gallery" class="link small">← Gallery</a>
</div>

{#if entries === null}
  <p class="muted">Loading…</p>
{:else if !aPick || !bPick}
  <p class="muted">
    Pick two renders from the <a href="/gallery" class="link">Gallery</a>
    to compare. Each card has a 'compare' toggle; once two are
    selected, an Open compare button appears.
  </p>
{:else if !aEntry || !bEntry}
  <p class="muted">
    Couldn't resolve one of the picks. The render may have been
    deleted or its cache evicted.
  </p>
{:else}
  <div class="meta">
    <div class="side">
      <span class="dot a-dot" aria-hidden="true"></span>
      <span class="side-name">{entryLabel(aEntry)}</span>
      <span class="muted small">{templateDisplayName(aEntry.template_id)}</span>
    </div>
    <div class="side right">
      <span class="muted small">{templateDisplayName(bEntry.template_id)}</span>
      <span class="side-name">{entryLabel(bEntry)}</span>
      <span class="dot b-dot" aria-hidden="true"></span>
    </div>
  </div>

  <div
    class="viewport"
    bind:this={viewport}
    role="slider"
    tabindex="0"
    aria-label="Compare slider"
    aria-valuemin="0"
    aria-valuemax="100"
    aria-valuenow={Math.round(pos)}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    onkeydown={onKeyDown}
  >
    <!-- Base layer: A. Always fully painted. -->
    <img
      class="layer"
      src={api.previewUrl(aEntry.preview_hash, aEntry.preview_port)}
      alt="A: {entryLabel(aEntry)}"
      draggable="false"
    />
    <!-- Top layer: B, clipped from the right so dragging the divider
         right reveals more of A. inset(0 right 0 0) means 'hide this
         much of the right side'. -->
    <img
      class="layer top"
      style:clip-path="inset(0 {100 - pos}% 0 0)"
      src={api.previewUrl(bEntry.preview_hash, bEntry.preview_port)}
      alt="B: {entryLabel(bEntry)}"
      draggable="false"
    />
    <!-- Divider line + handle. Position-tied to the percentage so the
         line stays glued to the wipe edge. -->
    <div class="divider" class:dragging style:left="{pos}%">
      <div class="handle" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="15 18 9 12 15 6" />
          <polyline points="9 6 15 12 9 18" transform="translate(8,0)" />
        </svg>
      </div>
    </div>
    <!-- A/B corner markers — match the meta legend so users connect
         which side of the wipe is which without having to read. -->
    <span class="corner a">A</span>
    <span class="corner b">B</span>
  </div>
{/if}

<style>
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin: 0.5rem 0 1.25rem;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }
  .muted { color: var(--fg-mute); }
  .small { font-size: 0.85em; }
  .link {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent-soft);
    text-underline-offset: 2px;
  }
  .link:hover { text-decoration-color: var(--accent); }

  /* Caption above the viewport — A on the left, B on the right, so the
     reader's eyes track the wipe direction. */
  .meta {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-bottom: 0.6rem;
  }
  .side {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
    min-width: 0;
  }
  .side.right {
    justify-content: flex-end;
    text-align: right;
  }
  .side-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.05rem;
    letter-spacing: -0.01em;
  }
  .dot {
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 999px;
    display: inline-block;
    flex-shrink: 0;
  }
  .a-dot { background: var(--accent); }
  .b-dot { background: var(--bad); }

  /* The wipe viewport: positioned for the absolute layers, sized by
     aspect-ratio so it scales without us specifying a fixed height. */
  .viewport {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
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
  .layer.top {
    /* clip-path is set inline; transition on it is unreliable cross-
       browser, so the responsiveness comes from the pointer move
       firing every few ms. */
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
