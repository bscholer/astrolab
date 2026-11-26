<!--
  The Tonight page's map view: pan the sky over the DSS color survey, find
  something to shoot, lay out a mosaic. The page owns the data, the
  selection and the actions; this draws them. Desktop keeps a side panel
  next to the map; on a phone the map fills the screen and details come up
  from below.
-->
<script lang="ts">
  import { onMount, tick, untrack } from 'svelte';
  import { api, type ScopeState, type TonightEntry, type TonightResponse } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { displayName, matchesSearch, typeGroup, upWindow, type TypeGroup } from '$lib/tonight';
  import SkyView from './SkyView.svelte';
  import TargetCard from './TargetCard.svelte';
  import MosaicEditor from './MosaicEditor.svelte';
  import {
    framingFov,
    importance,
    isFeatured,
    inBox,
    mosaicName,
    mosaicSize,
    planMosaic,
    suggestGrid,
    TELE_FOV_DEG,
    type Constellations,
    type Grid,
    type Layers,
  } from './atlas';
  import type { CoverageEntry } from '$lib/api';

  let {
    data,
    scope,
    selected,
    group = $bindable(),
    search = $bindable(),
    visible = true,
    canQueue = false,
    busy = false,
    onselect,
    onshoot,
    ontoggleauto,
    onlist,
    onplanchanged,
  }: {
    data: TonightResponse | null;
    scope: ScopeState | null;
    selected: string | null;
    group: TypeGroup | null;
    search: string;
    /** False while another view of the page is showing. */
    visible?: boolean;
    canQueue?: boolean;
    busy?: boolean;
    onselect: (name: string | null) => void;
    onshoot: (e: TonightEntry) => void;
    ontoggleauto: (e: TonightEntry) => void;
    onlist: (e: TonightEntry) => void;
    /** Called after a mosaic is saved or removed. */
    onplanchanged: () => Promise<void> | void;
  } = $props();

  const GROUPS: { id: TypeGroup; label: string }[] = [
    { id: 'nebulae', label: 'Nebulae' },
    { id: 'galaxies', label: 'Galaxies' },
    { id: 'clusters', label: 'Clusters' },
  ];

  let constellations = $state<Constellations | null>(null);
  let sky = $state<SkyView | null>(null);

  let searchOpen = $state(false);
  let frame = $state<'horizon' | 'equatorial'>('horizon');
  let layers = $state<Layers>({
    captured: true,
    grid: false,
    constellations: true,
    labels: true,
    frame: true,
    ground: true,
  });
  let coverage = $state<CoverageEntry[]>([]);
  // What blocks the sky at the scope, from its panorama; null without one.
  let skyline = $state<(number | null)[] | null>(null);
  // The mosaic being laid out for the selected object, or null.
  let mosaicGrid = $state<Grid | null>(null);
  let saving = $state(false);
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
    return () => mq.removeEventListener('change', set);
  });

  const minAlt = $derived(data?.min_alt_deg ?? 20);
  const entries = $derived((data?.entries ?? []).filter(isFeatured));
  const mapEntries = $derived(group ? entries.filter((e) => typeGroup(e.object_type) === group) : entries);
  const selectedEntry = $derived(
    selected ? (entries.find((e) => e.name === selected) ?? data?.entries.find((e) => e.name === selected) ?? null) : null,
  );
  const site = $derived(data ? { lat: data.site_latitude, lon: data.site_longitude } : null);
  const autoNames = $derived(new Set((scope?.targets ?? []).map((t) => t.name)));
  const covered = $derived(
    coverage.map((c) => ({ ra: c.ra_deg, dec: c.dec_deg, w: c.width_deg, h: c.height_deg, hours: c.usable_s / 3600 })),
  );
  const planned = $derived(
    (scope?.targets ?? [])
      // The mosaic being edited shows as the new grid only, not its old panels too.
      .filter((t) => t.mosaic && !(mosaicGrid && t.mosaic === selectedMosaic))
      .map((t) => ({
        ra: t.ra_deg,
        dec: t.dec_deg,
        w: TELE_FOV_DEG[0],
        h: TELE_FOV_DEG[1],
        label: (t.name.split(' panel ')[1] ?? '').split(' of ')[0],
      })),
  );
  const selectedMosaic = $derived(selectedEntry ? mosaicName(displayName(selectedEntry)) : null);
  const queuedPanels = $derived(
    selectedMosaic ? (scope?.targets ?? []).filter((t) => t.mosaic === selectedMosaic).length : 0,
  );

  const best = $derived(mapEntries.filter((e) => e.score > 0).slice(0, mobile ? 16 : 30));
  const results = $derived(
    search.trim()
      ? entries
          .filter((e) => matchesSearch(e, search))
          .sort((a, b) => importance(b) - importance(a))
          .slice(0, 8)
      : [],
  );

  const working = $derived(busy || saving);

  onMount(() => {
    api.getScopeHorizon().then(
      (h) => (skyline = h.alt),
      () => {},
    );
    api.getSkyCoverage().then(
      (c) => (coverage = c),
      () => {},
    );
    fetch('/atlas/constellations.json')
      .then((r) => (r.ok ? r.json() : null))
      .then((c) => (constellations = c))
      .catch(() => {});
  });

  // The object the map last brought into view. Selecting from elsewhere on
  // the page (the list, the plan) flies there the next time the map shows.
  let shown: string | null = null;
  let aimed = false;
  $effect(() => {
    if (!visible || !sky || !data) return;
    const e = selectedEntry;
    if (!aimed) {
      aimed = true;
      // Wait a tick so the map has the site before it is pointed anywhere.
      // A mosaic started on the way in has already aimed the map.
      tick().then(() => {
        if (mosaicGrid) return;
        if (e) show(e, true);
        else lookSouth();
      });
      return;
    }
    if (e && e.name !== shown) tick().then(() => show(e));
  });

  // The object the mosaic being laid out is for. Selecting another ends it.
  let mosaicFor: string | null = null;
  $effect(() => {
    const name = selected;
    untrack(() => {
      if (mosaicGrid && mosaicFor !== name) endMosaic();
      sheetOpen = false;
    });
  });

  function pick(name: string | null) {
    shown = name;
    onselect(name);
  }

  /** Bring an object into view. */
  export function show(e: TonightEntry, instant = false) {
    shown = e.name;
    // On a phone the sheet covers the bottom of the map.
    const lift = mobile ? Math.round(window.innerHeight * 0.16) : 0;
    if (instant) sky?.lookAtSky(e.ra_deg, e.dec_deg, framingFov(e), lift);
    else sky?.flyTo(e.ra_deg, e.dec_deg, framingFov(e), lift);
  }

  /** Select an object and bring it into view. */
  function choose(e: TonightEntry) {
    pick(e.name);
    search = '';
    searchOpen = false;
    show(e);
  }

  function onSearchKey(ev: KeyboardEvent) {
    if (ev.key === 'Enter' && results[0]) choose(results[0]);
    if (ev.key === 'Escape') {
      search = '';
      searchOpen = false;
    }
  }

  // ----- mosaics

  /** Lay out a mosaic over `e`, or edit the one already planned for it. */
  export function startMosaic(e: TonightEntry) {
    const name = mosaicName(displayName(e));
    const grid = gridOfQueued(name) ?? suggestGrid(e);
    mosaicFor = e.name;
    mosaicGrid = grid;
    sheetOpen = false;
    const [w, h] = mosaicSize(grid);
    const fov = Math.min(140, Math.max(w, h * 1.6) * 1.9);
    const center = queuedCenter(name) ?? { ra: e.ra_deg, dec: e.dec_deg };
    // North up is how the scope's frames come out, so the panels sit square.
    frameBeforeMosaic ??= frame;
    frame = 'equatorial';
    shown = e.name;
    aimed = true;
    sky?.flyTo(center.ra, center.dec, fov);
  }

  let frameBeforeMosaic: 'horizon' | 'equatorial' | null = null;

  function endMosaic() {
    mosaicGrid = null;
    mosaicFor = null;
    if (frameBeforeMosaic) frame = frameBeforeMosaic;
    frameBeforeMosaic = null;
  }

  /** Named objects whose centres fall inside the grid, with the time shot on each. */
  const insideMosaic = $derived.by(() => {
    if (!mosaicGrid || !readout) return [];
    const panels = planMosaic(readout.ra, readout.dec, mosaicGrid);
    return entries
      .filter((e) => (e.messier || e.common_name || e.name === selected) && panels.some((b) => inBox(b, e.ra_deg, e.dec_deg)))
      .sort((a, b) => importance(b) - importance(a))
      .slice(0, 4)
      .map((e) => ({ name: displayName(e), common: e.common_name, shot_s: e.shot_s }));
  });

  function queuedPanelsOf(name: string | null) {
    return (scope?.targets ?? []).filter((t) => name && t.mosaic === name);
  }

  /** Grid of the queued mosaic: panels in one row share a declination. */
  function gridOfQueued(name: string | null): Grid | null {
    const panels = queuedPanelsOf(name);
    if (panels.length < 2) return null;
    const rows = new Set(panels.map((p) => p.dec_deg.toFixed(4))).size;
    return { cols: Math.round(panels.length / rows), rows };
  }

  function queuedCenter(name: string | null): { ra: number; dec: number } | null {
    const panels = queuedPanelsOf(name);
    if (!panels.length) return null;
    const ref = panels[0].ra_deg;
    const dra = panels.map((p) => ((p.ra_deg - ref + 540) % 360) - 180);
    const ra = (ref + (Math.min(...dra) + Math.max(...dra)) / 2 + 360) % 360;
    const decs = panels.map((p) => p.dec_deg);
    return { ra, dec: (Math.min(...decs) + Math.max(...decs)) / 2 };
  }

  async function saveMosaic(e: TonightEntry) {
    if (!mosaicGrid || !sky) return;
    saving = true;
    try {
      const c = sky.center();
      await api.putScopeMosaic({
        name: displayName(e),
        ra_deg: c.ra,
        dec_deg: c.dec,
        cols: mosaicGrid.cols,
        rows: mosaicGrid.rows,
        object_type: e.object_type,
        common_name: e.common_name,
      });
      const n = mosaicGrid.cols * mosaicGrid.rows;
      toast.success(
        scope?.scheduler?.enabled
          ? `Added ${n} panels of ${displayName(e)} to the plan.`
          : `Added ${n} panels of ${displayName(e)} to the plan. Run all night is off; turn it on in Plan.`,
      );
      endMosaic();
      await onplanchanged();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      saving = false;
    }
  }

  async function removeMosaic() {
    if (!selectedMosaic) return;
    saving = true;
    try {
      const r = await api.deleteScopeMosaic(selectedMosaic);
      toast.success(`Took ${r.removed} panels off the plan.`);
      await onplanchanged();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      saving = false;
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
    const w = upWindow(e.alt_curve_deg, data.min_alt_deg, data.dusk_utc, data.alt_curve_step_min, e.skyline_curve_deg);
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
    ['captured', 'Already shot'],
    ['constellations', 'Constellations'],
    ['labels', 'Labels'],
    ['frame', 'Scope frame'],
    ['grid', 'Grid'],
    ['ground', 'Ground'],
  ];
</script>

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
    <button type="button" class:active={frame === 'horizon'} onclick={() => (frame = 'horizon')} title="Straight up is the top of the screen, as you see the sky from the yard">
      As seen
    </button>
    <button type="button" class:active={frame === 'equatorial'} onclick={() => (frame = 'equatorial')} title="North at the top, east to the left: how the scope's frames come out, so mosaic panels sit square">
      North up
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
      <span class="b-name">{displayName(e)}{#if autoNames.has(e.name)}<span class="b-auto" title="In the plan"> ●</span>{/if}</span>
      {#if e.common_name}<span class="b-common">{e.common_name}</span>{/if}
      <span class="b-up">{fmtUp(e)}</span>
    </span>
  </button>
{/snippet}

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
      {minAlt}
      {autoNames}
      {constellations}
      {covered}
      {planned}
      {mosaicGrid}
      {skyline}
      onselect={pick}
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
        {#if selectedEntry && mosaicGrid}
          <div class="sheet">
            <MosaicEditor
              name={displayName(selectedEntry)}
              grid={mosaicGrid}
              compact
              busy={working}
              replacing={queuedPanels > 0}
              onchange={(g) => (mosaicGrid = g)}
              onsave={() => saveMosaic(selectedEntry)}
              oncancel={endMosaic}
              inside={insideMosaic}
            />
          </div>
        {:else if selectedEntry && data}
          <div class="sheet" class:open={sheetOpen}>
            <button type="button" class="grip" aria-label={sheetOpen ? 'Show less' : 'Show more'} onclick={() => (sheetOpen = !sheetOpen)}></button>
            <TargetCard
              entry={selectedEntry}
              {data}
              {timeMs}
              compact={!sheetOpen}
              {canQueue}
              busy={working}
              onAuto={autoNames.has(selectedEntry.name)}
              onshoot={() => onshoot(selectedEntry)}
              ontoggleauto={() => ontoggleauto(selectedEntry)}
              onclose={() => pick(null)}
              onlist={() => onlist(selectedEntry)}
              onexpand={() => (sheetOpen = !sheetOpen)}
              queuedMosaic={queuedPanels || null}
              onmosaic={() => startMosaic(selectedEntry)}
              onremovemosaic={removeMosaic}
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
      {#if selectedEntry && mosaicGrid}
        <section class="panel">
          <MosaicEditor
            name={displayName(selectedEntry)}
            grid={mosaicGrid}
            busy={working}
            replacing={queuedPanels > 0}
            onchange={(g) => (mosaicGrid = g)}
            onsave={() => saveMosaic(selectedEntry)}
            oncancel={endMosaic}
            inside={insideMosaic}
          />
        </section>
      {:else if selectedEntry && data}
        <section class="panel">
          <TargetCard
            entry={selectedEntry}
            {data}
            {timeMs}
            {canQueue}
            busy={working}
            onAuto={autoNames.has(selectedEntry.name)}
            onshoot={() => onshoot(selectedEntry)}
            ontoggleauto={() => ontoggleauto(selectedEntry)}
            onclose={() => pick(null)}
              onlist={() => onlist(selectedEntry)}
            queuedMosaic={queuedPanels || null}
            onmosaic={() => startMosaic(selectedEntry)}
            onremovemosaic={removeMosaic}
          />
        </section>
      {/if}
      <h3 class="best-h">Best tonight</h3>
      {#if data && best.length === 0}
        <p class="muted small">Nothing of this type clears {minAlt.toFixed(0)}° tonight.</p>
      {/if}
      <div class="best">
        {#each best as e (e.name)}{@render bestItem(e)}{/each}
      </div>
      <p class="credit muted">
        Imagery: DSS color survey (STScI / Caltech) via ESA Sky HiPS. Constellation lines: d3-celestial.
      </p>
    </aside>
  {/if}
</div>

<style>
  .atlas {
    height: 100%;
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
