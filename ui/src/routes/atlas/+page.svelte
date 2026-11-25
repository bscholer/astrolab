<!--
  Atlas: pan around the sky over the DSS colour survey, find something to
  shoot, and send the scope to it. Desktop keeps a side panel next to the
  map; on a phone the map fills the screen and details come up from below.
-->
<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { page } from '$app/stores';
  import { replaceState } from '$app/navigation';
  import { api, type ScopeState, type TonightEntry, type TonightResponse } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { displayName, matchesSearch, typeGroup, upWindow, type TypeGroup } from '$lib/tonight';
  import SkyView from '$lib/atlas/SkyView.svelte';
  import TargetCard from '$lib/atlas/TargetCard.svelte';
  import { framingFov, importance, isFeatured, type Constellations, type Layers } from '$lib/atlas/atlas';

  const GROUPS: { id: TypeGroup; label: string }[] = [
    { id: 'nebulae', label: 'Nebulae' },
    { id: 'galaxies', label: 'Galaxies' },
    { id: 'clusters', label: 'Clusters' },
  ];
  const MIN_ALT = 20;

  let data = $state<TonightResponse | null>(null);
  let error = $state<string | null>(null);
  let constellations = $state<Constellations | null>(null);
  let scope = $state<ScopeState | null>(null);
  let sky = $state<SkyView | null>(null);

  let selected = $state<string | null>(null);
  let group = $state<TypeGroup | null>(null);
  let search = $state('');
  let searchOpen = $state(false);
  let frame = $state<'horizon' | 'equatorial'>('horizon');
  let layers = $state<Layers>({ grid: false, constellations: true, labels: true, frame: true, ground: true });
  let busy = $state(false);
  let sheetOpen = $state(false);
  let menuOpen = $state(false);
  let readout = $state<{ altDeg: number | null; azDeg: number | null; ra: number; dec: number; fov: number } | null>(
    null,
  );

  // Time shown on the map. Follows the clock until the slider moves it.
  let followNow = $state(true);
  let timeMs = $state(Date.now());
  $effect(() => {
    if (!followNow) return;
    timeMs = Date.now();
    const t = setInterval(() => (timeMs = Date.now()), 30_000);
    return () => clearInterval(t);
  });

  let mobile = $state(false);
  onMount(() => {
    const mq = window.matchMedia('(max-width: 899px)');
    const set = () => (mobile = mq.matches);
    set();
    mq.addEventListener('change', set);
    document.body.classList.add('atlas-page');
    return () => {
      mq.removeEventListener('change', set);
      document.body.classList.remove('atlas-page');
    };
  });

  const entries = $derived((data?.entries ?? []).filter(isFeatured));
  const mapEntries = $derived(group ? entries.filter((e) => typeGroup(e.object_type) === group) : entries);
  const selectedEntry = $derived(selected ? (entries.find((e) => e.name === selected) ?? null) : null);
  const site = $derived(data ? { lat: data.site_latitude, lon: data.site_longitude } : null);
  const autoNames = $derived(new Set((scope?.targets ?? []).map((t) => t.name)));

  const best = $derived(
    mapEntries.filter((e) => e.score > 0).slice(0, mobile ? 16 : 30),
  );
  const results = $derived(
    search.trim()
      ? entries
          .filter((e) => matchesSearch(e, search))
          .sort((a, b) => importance(b) - importance(a))
          .slice(0, 8)
      : [],
  );

  const canQueue = $derived(
    !!scope?.reachable && !!scope.capabilities?.includes('goto') && !!scope.capabilities?.includes('capture'),
  );
  const observing = $derived(scope?.observe?.state === 'running');

  async function load() {
    try {
      data = await api.getTonight({ all_sky: true, min_alt: MIN_ALT, max_mag: 12 });
      error = null;
      const want = $page.url.searchParams.get('target');
      const hit = want
        ? (data.entries.find((e) => e.name === want) ?? data.entries.find((e) => matchesSearch(e, want)))
        : null;
      // Wait a tick so the map has the site before it is pointed anywhere.
      tick().then(() => (hit ? choose(hit, true) : lookSouth()));
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function refreshScope() {
    try {
      scope = await api.getScope();
    } catch {
      // The next poll retries.
    }
  }

  onMount(() => {
    load();
    refreshScope();
    fetch('/atlas/constellations.json')
      .then((r) => (r.ok ? r.json() : null))
      .then((c) => (constellations = c))
      .catch(() => {});
    const t = setInterval(refreshScope, 10_000);
    return () => clearInterval(t);
  });

  function setUrl(name: string | null) {
    const url = new URL($page.url);
    if (name) url.searchParams.set('target', name);
    else url.searchParams.delete('target');
    try {
      replaceState(url, {});
    } catch {
      // Before the router is ready; the URL is cosmetic.
    }
  }

  function select(name: string | null) {
    selected = name;
    sheetOpen = false;
    setUrl(name);
  }

  /** Select an object and bring it into view. */
  function choose(e: TonightEntry, instant = false) {
    select(e.name);
    search = '';
    searchOpen = false;
    // On a phone the sheet covers the bottom of the map.
    const lift = mobile ? Math.round(window.innerHeight * 0.16) : 0;
    if (instant) sky?.lookAtSky(e.ra_deg, e.dec_deg, framingFov(e), lift);
    else sky?.flyTo(e.ra_deg, e.dec_deg, framingFov(e), lift);
  }

  function onSearchKey(ev: KeyboardEvent) {
    if (ev.key === 'Enter' && results[0]) choose(results[0]);
    if (ev.key === 'Escape') {
      search = '';
      searchOpen = false;
    }
  }

  async function shoot(e: TonightEntry) {
    busy = true;
    try {
      const r = await api.observe({ name: e.name, ra_deg: e.ra_deg, dec_deg: e.dec_deg, object_type: e.object_type });
      toast.success(`Started ${r.target}. Watch the Scope page for each step.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      busy = false;
    }
  }

  async function toggleAuto(e: TonightEntry) {
    busy = true;
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
      toast.success(on ? `Removed ${displayName(e)} from the auto list.` : `Added ${displayName(e)} to the auto list.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      busy = false;
    }
  }

  // ----- time slider: minutes from an hour before dusk to an hour after dawn

  const span = $derived.by(() => {
    if (!data?.dusk_utc || !data?.dawn_utc) return null;
    return {
      start: new Date(data.dusk_utc).getTime() - 3_600_000,
      end: new Date(data.dawn_utc).getTime() + 3_600_000,
    };
  });
  const sliderMin = $derived(span ? Math.round((timeMs - span.start) / 60_000) : 0);
  function onSlide(ev: Event) {
    if (!span) return;
    followNow = false;
    timeMs = span.start + Number((ev.target as HTMLInputElement).value) * 60_000;
  }

  function fmtTime(ms: number): string {
    return new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  function fmtUp(e: TonightEntry): string {
    if (!data) return '';
    const w = upWindow(e.alt_curve_deg, data.min_alt_deg, data.dusk_utc, data.alt_curve_step_min);
    if (!w) return 'Not up tonight';
    return `${fmtTime(w.start.getTime())} – ${fmtTime(w.end.getTime())}`;
  }

  function fmtReadout(r: NonNullable<typeof readout>): string {
    const f = r.fov >= 10 ? r.fov.toFixed(0) : r.fov.toFixed(1);
    if (frame === 'horizon' && r.altDeg !== null && r.azDeg !== null) {
      return `Alt ${r.altDeg.toFixed(0)}° · Az ${r.azDeg.toFixed(0)}° · ${f}° field`;
    }
    const h = r.ra / 15;
    return `RA ${Math.floor(h)}h${String(Math.floor((h % 1) * 60)).padStart(2, '0')}m · Dec ${r.dec.toFixed(0)}° · ${f}° field`;
  }

  function lookSouth() {
    sky?.lookAt(40, 180, mobile ? 110 : 100);
  }

  const LAYER_LABELS: [keyof Layers, string][] = [
    ['constellations', 'Constellations'],
    ['labels', 'Labels'],
    ['frame', 'Scope frame'],
    ['grid', 'Grid'],
    ['ground', 'Ground'],
  ];
</script>

<svelte:head><title>Atlas · astrolab</title></svelte:head>

{#snippet searchBox()}
  <div class="search-wrap">
    <input
      class="search"
      type="search"
      placeholder="Find M 31, Orion, Veil…"
      aria-label="Find an object"
      bind:value={search}
      onfocus={() => (searchOpen = true)}
      onkeydown={onSearchKey}
    />
    {#if searchOpen && results.length}
      <ul class="results" role="listbox">
        {#each results as e (e.name)}
          <li>
            <button type="button" onclick={() => choose(e)}>
              <span class="r-name">{displayName(e)}</span>
              {#if e.common_name}<span class="r-common">{e.common_name}</span>{/if}
              <span class="r-meta">{e.object_type ?? ''}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/snippet}

{#snippet chips()}
  <div class="chips" role="group" aria-label="Object type">
    <button type="button" class="chip" class:active={group === null} onclick={() => (group = null)}>All</button>
    {#each GROUPS as g (g.id)}
      <button type="button" class="chip" class:active={group === g.id} onclick={() => (group = group === g.id ? null : g.id)}>
        {g.label}
      </button>
    {/each}
  </div>
{/snippet}

{#snippet viewControls()}
  <div class="seg" role="group" aria-label="Map orientation">
    <button type="button" class:active={frame === 'horizon'} onclick={() => (frame = 'horizon')} title="Zenith up, as you see it from the yard">
      Horizon
    </button>
    <button type="button" class:active={frame === 'equatorial'} onclick={() => (frame = 'equatorial')} title="North up, like a star chart">
      Star chart
    </button>
  </div>
  <div class="layer-list">
    {#each LAYER_LABELS as [key, label] (key)}
      <label class="layer">
        <input type="checkbox" bind:checked={layers[key]} />
        {label}
      </label>
    {/each}
  </div>
{/snippet}

{#snippet timeBar()}
  {#if span}
    <div class="timebar">
      <button type="button" class="now-btn" class:active={followNow} onclick={() => (followNow = true)}>Now</button>
      <input
        type="range"
        min="0"
        max={Math.round((span.end - span.start) / 60_000)}
        step="5"
        value={sliderMin}
        oninput={onSlide}
        aria-label="Time of night"
      />
      <span class="time mono">{fmtTime(timeMs)}</span>
    </div>
  {/if}
{/snippet}

{#snippet bestItem(e: TonightEntry)}
  <button type="button" class="best-item" class:active={selected === e.name} onclick={() => choose(e)}>
    <img src={api.tonightThumbUrl(e.name, e.image_version)} alt="" loading="lazy" decoding="async" />
    <span class="b-text">
      <span class="b-name">{displayName(e)}{#if autoNames.has(e.name)}<span class="b-auto" title="On the auto list"> ●</span>{/if}</span>
      {#if e.common_name}<span class="b-common">{e.common_name}</span>{/if}
      <span class="b-up">{fmtUp(e)}</span>
    </span>
  </button>
{/snippet}

{#if error}
  <section class="error">
    <p>Couldn't load the atlas: {error}</p>
    {#if /site location/i.test(error)}
      <p class="muted">Set your latitude, longitude and elevation on the <a href="/settings">Settings</a> page first.</p>
    {/if}
  </section>
{:else}
  <div class="atlas" class:mobile class:desktop={!mobile}>
    <div class="map">
      <SkyView
        bind:this={sky}
        entries={mapEntries}
        {selected}
        {site}
        {timeMs}
        {frame}
        {layers}
        minAlt={MIN_ALT}
        {autoNames}
        {constellations}
        onselect={select}
        onviewchange={(r) => (readout = r)}
      />
      {#if !data}<p class="loading">Loading the sky…</p>{/if}

      {#if mobile}
        <div class="m-top">
          {@render searchBox()}
          <button type="button" class="round" aria-label="Map options" aria-expanded={menuOpen} onclick={() => (menuOpen = !menuOpen)}>☰</button>
        </div>
        {#if menuOpen}
          <div class="m-menu">
            {@render viewControls()}
            {@render chips()}
            {@render timeBar()}
            <button type="button" onclick={() => { lookSouth(); menuOpen = false; }}>Look south</button>
          </div>
        {/if}
        <div class="m-bottom">
          {#if selectedEntry && data}
            <div class="sheet" class:open={sheetOpen}>
              <button type="button" class="grip" aria-label={sheetOpen ? 'Show less' : 'Show more'} onclick={() => (sheetOpen = !sheetOpen)}></button>
              <TargetCard
                entry={selectedEntry}
                {data}
                {timeMs}
                compact={!sheetOpen}
                {canQueue}
                busy={busy || observing}
                onAuto={autoNames.has(selectedEntry.name)}
                onshoot={() => shoot(selectedEntry)}
                ontoggleauto={() => toggleAuto(selectedEntry)}
                onclose={() => select(null)}
                onexpand={() => (sheetOpen = !sheetOpen)}
              />
            </div>
          {:else if best.length}
            <div class="strip" aria-label="Best tonight">
              {#each best as e (e.name)}{@render bestItem(e)}{/each}
            </div>
          {/if}
        </div>
      {:else}
        <div class="d-tools">{@render viewControls()}</div>
        <div class="d-zoom">
          <button type="button" class="round" aria-label="Zoom in" onclick={() => sky?.zoomBy(1 / 1.6)}>+</button>
          <button type="button" class="round" aria-label="Zoom out" onclick={() => sky?.zoomBy(1.6)}>−</button>
          <button type="button" class="round small-txt" aria-label="Look south" title="Look south" onclick={lookSouth}>S</button>
        </div>
        <div class="d-bottom">
          {#if readout}<span class="readout mono">{fmtReadout(readout)}</span>{/if}
          {@render timeBar()}
        </div>
      {/if}
    </div>

    {#if !mobile}
      <aside class="side">
        {@render searchBox()}
        {@render chips()}
        {#if selectedEntry && data}
          <section class="panel">
            <TargetCard
              entry={selectedEntry}
              {data}
              {timeMs}
              {canQueue}
              busy={busy || observing}
              onAuto={autoNames.has(selectedEntry.name)}
              onshoot={() => shoot(selectedEntry)}
              ontoggleauto={() => toggleAuto(selectedEntry)}
              onclose={() => select(null)}
            />
          </section>
        {/if}
        <h3 class="best-h">Best tonight</h3>
        {#if data && best.length === 0}
          <p class="muted small">Nothing of this type clears {MIN_ALT}° tonight.</p>
        {/if}
        <div class="best">
          {#each best as e (e.name)}{@render bestItem(e)}{/each}
        </div>
        <p class="credit muted">
          Imagery: DSS colour survey (STScI / Caltech) via ESA Sky HiPS. Constellation lines: d3-celestial.
        </p>
      </aside>
    {/if}
  </div>
{/if}

<style>
  :global(body.atlas-page main.container) {
    max-width: none;
    padding: 0;
  }
  :global(body.atlas-page footer.footer) {
    display: none;
  }

  .atlas {
    --atlas-h: calc(100dvh - 3.4rem);
    height: var(--atlas-h);
    position: relative;
  }
  .desktop {
    display: grid;
    grid-template-columns: 1fr 380px;
  }
  .map {
    position: relative;
    height: 100%;
    min-width: 0;
  }
  .loading {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    color: var(--fg-mute);
    pointer-events: none;
  }
  .error {
    padding: 2rem 1.25rem;
    max-width: 640px;
    margin: 0 auto;
  }

  /* Floating controls on the map */
  .round {
    width: 40px;
    height: 40px;
    padding: 0;
    border-radius: 50%;
    font-size: 1.1rem;
    background: rgba(10, 15, 28, 0.85);
    backdrop-filter: blur(6px);
  }
  .small-txt {
    font-size: 0.8rem;
    font-weight: 600;
  }
  .d-tools {
    position: absolute;
    top: 0.75rem;
    left: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    align-items: flex-start;
  }
  .d-zoom {
    position: absolute;
    right: 0.75rem;
    top: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .d-bottom {
    position: absolute;
    left: 0.75rem;
    right: 0.75rem;
    bottom: 0.75rem;
    display: flex;
    gap: 0.75rem;
    align-items: center;
    pointer-events: none;
  }
  .d-bottom > :global(*) {
    pointer-events: auto;
  }
  .readout {
    font-size: 0.75rem;
    color: var(--fg-mute);
    background: rgba(2, 3, 10, 0.7);
    padding: 0.3rem 0.55rem;
    border-radius: var(--radius);
    white-space: nowrap;
  }
  .seg {
    display: inline-flex;
    border: 1px solid var(--border);
    border-radius: 999px;
    overflow: hidden;
    background: rgba(10, 15, 28, 0.85);
    backdrop-filter: blur(6px);
  }
  .seg button {
    border: none;
    border-radius: 0;
    background: transparent;
    font-size: 0.82rem;
    padding: 0.4rem 0.8rem;
  }
  .seg button.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .layer-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.75rem;
    font-size: 0.8rem;
    padding: 0.35rem 0.65rem;
    border-radius: var(--radius);
    background: rgba(10, 15, 28, 0.75);
    backdrop-filter: blur(6px);
  }
  .desktop .layer-list {
    flex-direction: column;
  }
  .layer {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    cursor: pointer;
    color: var(--fg-mute);
  }
  .layer input {
    accent-color: var(--accent);
    margin: 0;
  }
  .timebar {
    flex: 1;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.3rem 0.7rem 0.3rem 0.35rem;
    border-radius: 999px;
    background: rgba(10, 15, 28, 0.85);
    backdrop-filter: blur(6px);
    border: 1px solid var(--border);
    max-width: 560px;
    margin-left: auto;
  }
  .timebar input {
    flex: 1;
    min-width: 0;
  }
  .now-btn {
    font-size: 0.78rem;
    padding: 0.25rem 0.65rem;
  }
  .now-btn.active {
    color: var(--accent);
    border-color: var(--accent);
  }
  .time {
    font-size: 0.8rem;
    min-width: 4.8rem;
    text-align: right;
  }
  .mono {
    font-family: var(--font-mono);
  }

  /* Search */
  .search-wrap {
    position: relative;
    flex: 1;
  }
  .search {
    width: 100%;
  }
  .results {
    position: absolute;
    z-index: 5;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    margin: 0;
    padding: 0.3rem;
    list-style: none;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
  }
  .results button {
    width: 100%;
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 0.5rem;
    text-align: left;
    border: none;
    border-radius: 6px;
    background: transparent;
    padding: 0.45rem 0.55rem;
    font-size: 0.88rem;
  }
  .results button:hover {
    background: var(--bg-elev-2);
  }
  .r-name {
    font-weight: 600;
  }
  .r-common {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .r-meta {
    color: var(--fg-mute);
    font-size: 0.78rem;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .chip {
    font-size: 0.8rem;
    padding: 0.3rem 0.75rem;
  }
  .chip.active {
    color: var(--accent);
    border-color: var(--accent);
    background: var(--accent-soft);
  }

  /* Desktop side panel */
  .side {
    height: 100%;
    overflow-y: auto;
    padding: 0.9rem 1rem 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.85rem;
    border-left: 1px solid var(--border);
    background: rgba(5, 8, 18, 0.75);
  }
  .panel {
    padding: 0.9rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
  }
  .best-h {
    margin: 0.25rem 0 0;
    font-size: 0.95rem;
  }
  .best {
    display: grid;
    gap: 0.35rem;
  }
  .best-item {
    display: grid;
    grid-template-columns: 48px 1fr;
    gap: 0.7rem;
    align-items: center;
    text-align: left;
    padding: 0.35rem;
    border-radius: var(--radius);
    border: 1px solid transparent;
    background: transparent;
  }
  .best-item:hover,
  .best-item.active {
    background: var(--bg-elev);
    border-color: var(--border);
  }
  .best-item img {
    width: 48px;
    height: 48px;
    border-radius: 6px;
    object-fit: cover;
    background: var(--bg);
  }
  .b-text {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .b-name {
    font-weight: 600;
    font-size: 0.9rem;
  }
  .b-auto {
    color: var(--accent);
    font-size: 0.7rem;
  }
  .b-common,
  .b-up {
    font-size: 0.78rem;
    color: var(--fg-mute);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .credit {
    margin-top: auto;
    font-size: 0.7rem;
  }
  .small {
    font-size: 0.85rem;
  }

  /* Mobile */
  .mobile .map {
    height: 100%;
  }
  .m-top {
    position: absolute;
    top: 0.6rem;
    left: 0.6rem;
    right: 0.6rem;
    display: flex;
    gap: 0.5rem;
    align-items: flex-start;
  }
  .m-top .search {
    height: 40px;
    border-radius: 999px;
    padding-left: 1rem;
    background: rgba(10, 15, 28, 0.88);
    backdrop-filter: blur(6px);
  }
  .m-menu {
    position: absolute;
    top: 3.4rem;
    left: 0.6rem;
    right: 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
    padding: 0.8rem;
    border-radius: var(--radius-card);
    background: rgba(10, 15, 28, 0.94);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
  }
  .m-menu .timebar {
    margin-left: 0;
    max-width: none;
  }
  .m-menu .layer-list {
    background: none;
    padding: 0;
  }
  .m-bottom {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    padding-bottom: env(safe-area-inset-bottom);
  }
  .sheet {
    margin: 0 0.5rem 0.5rem;
    padding: 0.4rem 0.9rem 0.9rem;
    border-radius: var(--radius-card);
    background: rgba(10, 15, 28, 0.95);
    backdrop-filter: blur(8px);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    max-height: 70dvh;
    overflow-y: auto;
  }
  .grip {
    display: block;
    width: 44px;
    height: 14px;
    margin: 0 auto 0.35rem;
    padding: 0;
    border: none;
    background: transparent;
    position: relative;
  }
  .grip::after {
    content: '';
    position: absolute;
    left: 6px;
    right: 6px;
    top: 5px;
    height: 4px;
    border-radius: 2px;
    background: var(--fg-mute);
    opacity: 0.6;
  }
  .strip {
    display: flex;
    gap: 0.5rem;
    overflow-x: auto;
    padding: 0 0.6rem 0.6rem;
    scroll-snap-type: x mandatory;
    scrollbar-width: none;
  }
  .strip .best-item {
    flex: 0 0 auto;
    width: 220px;
    scroll-snap-align: start;
    background: rgba(10, 15, 28, 0.9);
    backdrop-filter: blur(6px);
    border-color: var(--border);
  }
</style>
