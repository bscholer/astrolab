<!--
  Tonight: what is worth pointing at from the configured site, and the plan
  Run all night works through. One set of targets, one selection, three
  views of it: the ranked list, the sky map, and the plan. Picking an object
  in one view picks it in the others. The URL keeps the view and the object
  (?view=map&target=NGC 224), so links and reloads land in the same place.
-->
<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { replaceState } from '$app/navigation';
  import {
    api,
    type MasterAdviceRow,
    type ScopeAutoTarget,
    type ScopeState,
    type TonightEntry,
    type TonightResponse,
  } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import MoreData from '$lib/MoreData.svelte';
  import TonightList from '$lib/TonightList.svelte';
  import AutoPlan from '$lib/AutoPlan.svelte';
  import AtlasMap from '$lib/atlas/AtlasMap.svelte';
  import { mosaicName } from '$lib/atlas/atlas';
  import { displayName, matchesSearch, typeGroup, type TypeGroup } from '$lib/tonight';

  type View = 'list' | 'map' | 'plan';
  const VIEWS: View[] = ['list', 'map', 'plan'];
  const GROUPS: { id: TypeGroup; label: string }[] = [
    { id: 'nebulae', label: 'Nebulae' },
    { id: 'galaxies', label: 'Galaxies' },
    { id: 'clusters', label: 'Clusters' },
  ];

  const params = $page.url.searchParams;
  let view = $state<View>(VIEWS.includes(params.get('view') as View) ? (params.get('view') as View) : 'list');
  let selected = $state<string | null>(params.get('target'));
  // Older links put the object in ?q=.
  let search = $state(params.get('q') ?? '');
  let group = $state<TypeGroup | null>(null);
  let shotOnly = $state(false);
  let showFilters = $state(false);
  // Clearing a number input gives null. load() skips the fetch until the
  // value is valid again.
  let minAlt = $state<number | null>(20);
  let maxMag = $state<number | null>(12);

  let data = $state<TonightResponse | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);
  let scope = $state<ScopeState | null>(null);
  // Masters that gain the most from more time (server/capture_loop.py).
  let worth = $state<MasterAdviceRow[]>([]);

  let map = $state<AtlasMap | null>(null);
  // The map is heavy; it mounts the first time it is shown, then stays.
  let mapMounted = $state(false);
  $effect(() => {
    if (view === 'map') mapMounted = true;
  });

  // The map goes edge to edge under the top bar.
  $effect(() => {
    document.body.classList.toggle('atlas-page', view === 'map');
    return () => document.body.classList.remove('atlas-page');
  });

  // Keep the view and the object in the URL.
  $effect(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete('q');
    if (view === 'list') url.searchParams.delete('view');
    else url.searchParams.set('view', view);
    if (selected) url.searchParams.set('target', selected);
    else url.searchParams.delete('target');
    if (url.href === window.location.href) return;
    try {
      replaceState(url, {});
    } catch {
      // Before the router is ready; the URL is cosmetic.
    }
  });

  async function load() {
    const min = minAlt;
    const max = maxMag;
    // Skip the refetch while either input is empty or mid-edit; the effect
    // fires again once the value is a number.
    if (min == null || max == null || !Number.isFinite(min) || !Number.isFinite(max)) return;
    loading = true;
    try {
      // The whole sky, so the map can show what is down too. The list keeps
      // what clears the altitude limit.
      data = await api.getTonight({ all_sky: true, min_alt: min, max_mag: max });
      error = null;
      // A ?target= that is not a catalog id: find it by name.
      if (selected && !data.entries.some((e) => e.name === selected)) {
        const want = selected;
        selected = data.entries.find((e) => matchesSearch(e, want))?.name ?? null;
      }
    } catch (e) {
      error = (e as Error).message;
      toast.error(error);
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void minAlt;
    void maxMag;
    load();
  });

  async function refreshScope() {
    try {
      scope = await api.getScope();
    } catch {
      // The next poll retries.
    }
  }

  onMount(() => {
    refreshScope();
    api.getMasterAdvice(true).then(
      (rows) => (worth = rows.slice(0, 4)),
      () => (worth = []),
    );
    const t = setInterval(refreshScope, 5000);
    return () => clearInterval(t);
  });

  const canQueue = $derived(
    !!scope?.reachable && !!scope.capabilities?.includes('goto') && !!scope.capabilities?.includes('capture'),
  );
  const obs = $derived(scope?.observe ?? null);
  const job = $derived(scope?.status?.job ?? null);
  const observing = $derived(obs?.state === 'running');
  const planned = $derived(new Set((scope?.targets ?? []).map((t) => t.name)));

  /** Rows in the plan: one per target, one per mosaic. */
  const planCount = $derived(
    new Set((scope?.targets ?? []).map((t) => (t.mosaic ? `m:${t.mosaic}` : t.name))).size,
  );

  /** Panels in the plan per object, for objects with a mosaic planned. */
  const mosaics = $derived.by(() => {
    const counts = new Map<string, number>();
    for (const t of scope?.targets ?? []) if (t.mosaic) counts.set(t.mosaic, (counts.get(t.mosaic) ?? 0) + 1);
    const out = new Map<string, number>();
    if (!counts.size || !data) return out;
    for (const e of data.entries) {
      const n = counts.get(mosaicName(displayName(e)));
      if (n) out.set(e.name, n);
    }
    return out;
  });

  function upTonight(e: TonightEntry, d: TonightResponse): boolean {
    return d.dusk_utc && d.dawn_utc ? e.hours_above_min_alt > 0 : e.alt_now_deg >= d.min_alt_deg;
  }

  const listEntries = $derived(
    data
      ? data.entries.filter(
          (e) =>
            upTonight(e, data!) &&
            (!shotOnly || e.session_count > 0 || e.shot_s > 0) &&
            (group === null || typeGroup(e.object_type) === group) &&
            matchesSearch(e, search),
        )
      : [],
  );

  function select(name: string | null) {
    selected = name;
  }

  // ----- actions, the same from every view

  let pointing = $state<string | null>(null);
  let saving = $state(false);

  async function shoot(e: TonightEntry) {
    pointing = e.name;
    try {
      const r = await api.observe({ name: e.name, ra_deg: e.ra_deg, dec_deg: e.dec_deg, object_type: e.object_type });
      toast.success(`Started ${r.target}. The Scope page shows each step.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      pointing = null;
    }
  }

  async function togglePlan(e: TonightEntry) {
    saving = true;
    try {
      const current = (await api.getScope()).targets ?? [];
      const on = current.some((t) => t.name === e.name);
      const targets = on
        ? current.filter((t) => t.name !== e.name)
        : [
            ...current,
            {
              name: e.name,
              ra_deg: e.ra_deg,
              dec_deg: e.dec_deg,
              object_type: e.object_type,
              display_name: displayName(e),
              common_name: e.common_name,
            },
          ];
      await api.setScopeAuto({ enabled: !!scope?.scheduler?.enabled, targets });
      toast.success(on ? `Took ${displayName(e)} out of the plan.` : `Added ${displayName(e)} to the plan.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      saving = false;
    }
  }

  function showOnMap(e: TonightEntry) {
    select(e.name);
    view = 'map';
  }

  function showInList(e: TonightEntry) {
    select(e.name);
    view = 'list';
  }

  async function mosaic(e: TonightEntry) {
    select(e.name);
    view = 'map';
    await tick();
    await tick();
    map?.startMosaic(e);
  }

  /** A plan row's object on the map; a mosaic shows its panels. */
  function showPlanned(t: ScopeAutoTarget) {
    const entries = data?.entries ?? [];
    const e = t.mosaic
      ? entries.find((x) => mosaicName(displayName(x)) === t.mosaic)
      : entries.find((x) => x.name === t.name);
    if (e) showOnMap(e);
    else toast.error(`${t.display_name ?? t.name} is not in the catalog the map draws.`);
  }

  function fmtTime(iso: string): string {
    return new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
</script>

<svelte:head><title>Tonight · astrolab</title></svelte:head>

<div class="page" class:map-mode={view === 'map'}>
  <div class="header">
    <h1>Tonight</h1>
    {#if data}
      <span class="meta muted">
        {data.dusk_utc && data.dawn_utc ? `Dark ${fmtTime(data.dusk_utc)} – ${fmtTime(data.dawn_utc)}` : '24-hour daylight'}
      </span>
    {/if}
    {#if observing || job?.state === 'running'}
      <a class="pill" href="/scope">
        <span class="live-dot" aria-hidden="true"></span>
        {observing ? obs?.target : 'Scope'}
        <span class="muted">
          {#if observing}{obs?.step}{:else if job}{job.captured ?? 0}/{job.frames ?? 0}{/if}
        </span>
      </a>
    {/if}
    <div class="tabs" role="tablist" aria-label="Views of tonight">
      <button type="button" role="tab" class="tab" class:active={view === 'list'} aria-selected={view === 'list'} onclick={() => (view = 'list')}>List</button>
      <button type="button" role="tab" class="tab" class:active={view === 'map'} aria-selected={view === 'map'} onclick={() => (view = 'map')}>Map</button>
      <button type="button" role="tab" class="tab" class:active={view === 'plan'} aria-selected={view === 'plan'} onclick={() => (view = 'plan')}>
        Plan{#if planCount}<span class="count">{planCount}</span>{/if}
      </button>
    </div>
  </div>

  {#if error && view !== 'plan'}
    <section class="panel error-panel">
      <p>Couldn't load Tonight: {error}</p>
      {#if /site location/i.test(error)}
        <p class="muted small">
          Set your latitude, longitude and elevation on the <a href="/settings">Settings</a> page, then come back.
        </p>
      {/if}
    </section>
  {/if}

  {#if view === 'list' && !error}
    <div class="toolbar">
      <input class="search" type="search" placeholder="Search" aria-label="Search targets" bind:value={search} />
      <button
        type="button"
        class="filter-toggle"
        class:active={showFilters || group !== null || shotOnly}
        aria-expanded={showFilters}
        onclick={() => (showFilters = !showFilters)}
      >
        Filter
      </button>
    </div>
    {#if showFilters}
      <section class="filters">
        <div class="filter-row">
          <label class="filter-num">
            <span class="filter-label">Min altitude</span>
            <input type="number" min={0} max={89} step={1} bind:value={minAlt} aria-label="Minimum altitude in degrees" />
            <span class="filter-unit">deg</span>
          </label>
          <label class="filter-num">
            <span class="filter-label">Max magnitude</span>
            <input type="number" min={-2} max={20} step={0.5} bind:value={maxMag} aria-label="Maximum magnitude (dimmer = larger)" />
          </label>
        </div>
        <div class="chips" role="group" aria-label="Object type">
          <button type="button" class="chip" class:active={group === null} onclick={() => (group = null)}>All types</button>
          {#each GROUPS as g (g.id)}
            <button type="button" class="chip" class:active={group === g.id} onclick={() => (group = group === g.id ? null : g.id)}>
              {g.label}
            </button>
          {/each}
          <button type="button" class="chip" class:active={shotOnly} onclick={() => (shotOnly = !shotOnly)}>Shot before</button>
        </div>
        <p class="muted small hint">The altitude and magnitude limits apply to the map too.</p>
      </section>
    {/if}
    {#if data}
      <TonightList
        {data}
        entries={listEntries}
        {selected}
        visible={view === 'list'}
        {planned}
        {mosaics}
        {canQueue}
        busy={saving || pointing !== null || observing}
        {pointing}
        onselect={select}
        onshoot={shoot}
        ontoggleauto={togglePlan}
        onmap={showOnMap}
        onmosaic={mosaic}
        onimagechanged={load}
      />
    {:else if loading}
      <p class="muted">Computing the sky…</p>
    {/if}
  {/if}

  {#if view === 'plan'}
    <section class="panel">
      <AutoPlan {scope} onchanged={refreshScope} onshow={showPlanned} />
    </section>
    {#if worth.length}
      <section class="panel worth" aria-label="Worth more time">
        <h2 class="worth-h">Worth more time</h2>
        <ul>
          {#each worth as row, i (row.project_id)}
            <li>
              <a class="worth-name" href="/projects/{row.project_id}">
                {row.common_name ?? row.name}
                {#if row.common_name}<span class="worth-canon">{row.target}</span>{/if}
              </a>
              <MoreData
                {row}
                compact
                onUpdated={(r) => {
                  worth[i] = r;
                  refreshScope();
                }}
              />
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}

  {#if mapMounted && !error}
    <div class="map-wrap" class:hidden={view !== 'map'}>
      <AtlasMap
        bind:this={map}
        bind:group
        bind:search
        {data}
        {scope}
        {selected}
        visible={view === 'map'}
        {canQueue}
        busy={saving || pointing !== null || observing}
        onselect={select}
        onshoot={shoot}
        ontoggleauto={togglePlan}
        onlist={showInList}
        onplanchanged={refreshScope}
      />
    </div>
  {/if}
</div>

<style>
  :global(body.atlas-page main.container) {
    max-width: none;
    padding: 0;
  }
  :global(body.atlas-page footer.footer) {
    display: none;
  }
  .map-mode {
    height: calc(100dvh - 3.4rem);
    display: flex;
    flex-direction: column;
  }
  .map-mode .header {
    margin: 0;
    padding: 0.35rem 1rem 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  .map-wrap {
    flex: 1;
    min-height: 0;
  }
  .map-wrap.hidden {
    display: none;
  }

  .header {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.4rem 0.9rem;
    margin: 0.5rem 0 1rem;
  }
  .header h1 {
    margin: 0;
    font-size: 1.5rem;
  }
  .map-mode .header h1 {
    font-size: 1.15rem;
  }
  .meta {
    font-family: var(--font-mono);
    font-size: 0.85em;
  }
  .tabs {
    display: flex;
    gap: 0.25rem;
    margin-left: auto;
    padding: 0.15rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elev);
  }
  .tab {
    appearance: none;
    border: 0;
    border-radius: 999px;
    background: none;
    padding: 0.3rem 0.85rem;
    min-height: 0;
    font: inherit;
    font-size: 0.85rem;
    color: var(--fg-mute);
    cursor: pointer;
    white-space: nowrap;
  }
  .tab.active {
    color: var(--accent);
    background: var(--accent-soft);
    font-weight: 600;
  }
  .count {
    margin-left: 0.35rem;
    font-family: var(--font-mono);
    font-size: 0.72rem;
    opacity: 0.8;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    max-width: 14rem;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    padding: 0.2rem 0.7rem;
    font-size: 0.78rem;
    color: var(--fg);
    text-decoration: none;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elev);
    align-self: center;
  }
  .live-dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--good);
    box-shadow: 0 0 8px var(--good);
  }
  @media (max-width: 560px) {
    .tabs {
      margin-left: 0;
      order: 10;
      flex-basis: 100%;
      justify-content: space-between;
    }
    .tab {
      flex: 1;
    }
  }

  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute);
  }
  .panel {
    margin-bottom: 1.25rem;
    padding: 1rem 1.2rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
  }
  .error-panel {
    border-color: var(--bad);
  }
  .worth-h {
    margin: 0 0 0.6rem;
    font-size: 1.05rem;
  }
  .worth ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.9rem;
  }
  .worth li {
    display: grid;
    gap: 0.2rem;
  }
  .worth-canon {
    font-weight: 400;
    font-size: 0.78rem;
    color: var(--fg-mute);
    font-family: var(--font-mono);
    margin-left: 0.4rem;
  }
  .worth-name {
    font-weight: 600;
    color: var(--fg);
    text-decoration: none;
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.9rem;
  }
  .search {
    flex: 1;
    min-width: 0;
    box-sizing: border-box;
    padding: 0.55rem 0.9rem;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 999px;
    font: inherit;
  }
  .filter-toggle {
    appearance: none;
    padding: 0.25rem 0;
    background: none;
    border: 0;
    color: var(--fg-mute);
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .filter-toggle.active {
    color: var(--accent);
  }
  .filters {
    margin-bottom: 1.1rem;
  }
  .filter-row {
    display: flex;
    gap: 1.25rem;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 0.75rem;
  }
  .filter-num {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }
  .filter-num input {
    width: 4.5rem;
    padding: 0.3rem 0.55rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font: inherit;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .filter-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .filter-unit {
    color: var(--fg-mute);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }
  .hint {
    margin: 0.6rem 0 0;
  }
  .chips {
    display: flex;
    gap: 0.4rem;
    flex-wrap: nowrap;
    overflow-x: auto;
    scrollbar-width: none;
    align-items: center;
    padding-bottom: 2px;
  }
  .chips > * {
    flex-shrink: 0;
  }
  .chip {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    min-height: 34px;
    padding: 0 0.8rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .chip:hover {
    color: var(--fg);
    border-color: var(--accent);
  }
  .chip.active {
    color: var(--accent-ink);
    background: var(--accent);
    border-color: transparent;
    font-weight: 600;
  }
</style>
