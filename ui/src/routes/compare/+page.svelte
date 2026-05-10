<!--
  Side-by-side compare with a slider wipe. URL-shareable:
    /compare?a=<project_id>:<seq>&b=<project_id>:<seq>

  We piggyback on /api/gallery for the lookup table, same backend
  shape used by /gallery, so we don't grow the API surface for one
  feature. The actual wipe is delegated to <CompareSlider/>; this page
  just owns gallery loading + URL-pick parsing.
-->
<script lang="ts">
  import { page } from '$app/stores';
  import { api, type GalleryEntry } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { templateDisplayName } from '$lib/format';
  import CompareSlider from '$lib/CompareSlider.svelte';

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

  <CompareSlider
    aSrc={api.previewUrl(aEntry.preview_hash, aEntry.preview_port)}
    bSrc={api.previewUrl(bEntry.preview_hash, bEntry.preview_port)}
    aLabel={entryLabel(aEntry)}
    bLabel={entryLabel(bEntry)}
  />
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
</style>
