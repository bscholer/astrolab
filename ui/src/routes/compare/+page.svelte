<!--
  Side-by-side compare with a slider wipe. URL-shareable:
    /compare?a=<project_id>:<seq>&b=<project_id>:<seq>

  We piggyback on /api/gallery for the lookup table, same backend
  shape used by /gallery, so we don't grow the API surface for one
  feature. The actual wipe is delegated to <CompareSlider/>; this page
  just owns gallery loading + URL-pick parsing.
-->
<script lang="ts">
  import { tick } from 'svelte';
  import { page } from '$app/stores';
  import { api, type GalleryEntry, type Project } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { templateDisplayName } from '$lib/format';
  import CompareSlider from '$lib/CompareSlider.svelte';

  let entries = $state<GalleryEntry[] | null>(null);
  // Per-project cache; gallery entries don't carry overrides, so the
  // param-diff line below has to fetch the project record for each side.
  let projectCache = $state<Record<string, Project>>({});
  // Bound from <CompareSlider/> so we can focus it without scraping the
  // DOM by class. CropEditor also uses .viewport, so querySelector would
  // be the wrong handle on any page that mounts both.
  let sliderEl = $state<HTMLDivElement | null>(null);

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

  // Load Project records for both picks so we can read history[seq].overrides
  // and compute the param diff. Same project on both sides? Fetch once.
  $effect(() => {
    const ids = new Set<string>();
    if (aPick) ids.add(aPick.project_id);
    if (bPick) ids.add(bPick.project_id);
    for (const id of ids) {
      if (projectCache[id]) continue;
      api.getProject(id)
        .then((p) => {
          projectCache = { ...projectCache, [id]: p };
        })
        .catch((e) => {
          // Param diff is a nicety; failing it shouldn't toast at the user,
          // but a silent swallow is opaque during debugging.
          console.debug(`compare: couldn't fetch project ${id}:`, e);
        });
    }
  });

  // Auto-focus the slider's viewport once both renders have resolved so
  // arrow-key/Home/End wipe works without an extra Tab press. The
  // bound `sliderEl` is the single source of truth; tick() ensures the
  // child has mounted and assigned the binding before we focus.
  $effect(() => {
    if (!aEntry || !bEntry) return;
    tick().then(() => {
      sliderEl?.focus();
    });
  });

  function entryLabel(e: GalleryEntry): string {
    const friendly = e.target_common_name && e.target_common_name !== e.project_name
      ? `${e.target_common_name} (${e.project_name})`
      : e.project_name;
    return `${friendly} · v${e.seq + 1}`;
  }

  function overridesFor(pick: Pick | null): Record<string, Record<string, unknown>> {
    if (!pick) return {};
    const proj = projectCache[pick.project_id];
    if (!proj) return {};
    return proj.history.find((h) => h.seq === pick.seq)?.overrides ?? {};
  }

  type ParamDiff = { key: string; a: unknown; b: unknown };

  function computeDiff(
    aOv: Record<string, Record<string, unknown>>,
    bOv: Record<string, Record<string, unknown>>
  ): ParamDiff[] {
    const out: ParamDiff[] = [];
    const nodes = new Set([...Object.keys(aOv), ...Object.keys(bOv)]);
    for (const node of nodes) {
      const aParams = aOv[node] ?? {};
      const bParams = bOv[node] ?? {};
      const params = new Set([...Object.keys(aParams), ...Object.keys(bParams)]);
      for (const p of params) {
        const av = aParams[p];
        const bv = bParams[p];
        // JSON-equality is good enough here (params are scalars or small
        // arrays); avoids dragging in a deep-equal helper.
        if (JSON.stringify(av) !== JSON.stringify(bv)) {
          out.push({ key: `${node}.${p}`, a: av, b: bv });
        }
      }
    }
    return out;
  }

  const paramDiff = $derived.by(() => computeDiff(overridesFor(aPick), overridesFor(bPick)));

  function fmtVal(v: unknown): string {
    if (v === undefined) return '∅';
    if (typeof v === 'string') return v;
    return JSON.stringify(v);
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

  {#if paramDiff.length > 0}
    <p class="param-diff muted small">
      {#each paramDiff.slice(0, 4) as d, i (d.key)}
        {#if i > 0}<span aria-hidden="true"> · </span>{/if}
        <span class="diff-key">{d.key}</span>:
        <code>{fmtVal(d.a)}</code>
        <span aria-hidden="true">→</span>
        <code>{fmtVal(d.b)}</code>
      {/each}
      {#if paramDiff.length > 4}
        <span class="muted"> (+{paramDiff.length - 4} more)</span>
      {/if}
    </p>
  {/if}

  <CompareSlider
    bind:el={sliderEl}
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

  .param-diff {
    margin: 0 0 0.6rem;
    line-height: 1.5;
  }
  .param-diff code {
    font-family: var(--font-mono);
    font-size: 0.82em;
    background: var(--bg-elev);
    padding: 0.05rem 0.35rem;
    border-radius: 4px;
  }
  .diff-key {
    font-family: var(--font-mono);
    font-size: 0.85em;
    color: var(--fg);
  }
</style>
