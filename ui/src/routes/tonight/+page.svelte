<!--
  Tonight: what is worth pointing at from the configured site. The server
  ranks the targets, and each row opens to show the altitude curve.
-->
<script lang="ts">
  import { api, type ScopeState, type TonightEntry, type TonightResponse } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { displayName, matchesSearch, typeGroup, upWindow, type TypeGroup } from '$lib/tonight';

  let data = $state<TonightResponse | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Clearing a number input gives null. load() skips the fetch until the
  // value is valid again.
  let minAlt = $state<number | null>(20);
  let maxMag = $state<number | null>(12);

  type View = 'recommended' | 'auto' | 'captured';
  const VIEWS: { id: View; label: string }[] = [
    { id: 'recommended', label: 'Recommended' },
    { id: 'auto', label: 'Auto list' },
    { id: 'captured', label: 'Captured' },
  ];
  const GROUPS: { id: TypeGroup; label: string }[] = [
    { id: 'nebulae', label: 'Nebulae' },
    { id: 'galaxies', label: 'Galaxies' },
    { id: 'clusters', label: 'Clusters' },
  ];
  const PAGE_SIZE = 40;

  let view = $state<View>('recommended');
  let group = $state<TypeGroup | null>(null);
  let search = $state('');
  let showFilters = $state(false);
  let limit = $state(PAGE_SIZE);
  let expanded = $state<string | null>(null);
  let failedThumbs = $state<Record<string, true | undefined>>({});

  let scope = $state<ScopeState | null>(null);
  let queuing = $state<string | null>(null);
  const canQueue = $derived(
    !!scope?.reachable &&
      !!scope.capabilities?.includes('goto') &&
      !!scope.capabilities?.includes('capture'),
  );

  async function queueTarget(e: TonightEntry) {
    queuing = e.name;
    try {
      const r = await api.observe({ name: e.name, ra_deg: e.ra_deg, dec_deg: e.dec_deg });
      toast.success(`Started ${r.target}. Watch the banner for each step.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      queuing = null;
    }
  }

  let adding = $state<string | null>(null);

  async function addToAuto(e: TonightEntry) {
    adding = e.name;
    try {
      const current = (await api.getScope()).targets ?? [];
      if (current.some((t) => t.name === e.name)) {
        toast.success(`${e.name} is already on the auto list.`);
        return;
      }
      const targets = [...current, { name: e.name, ra_deg: e.ra_deg, dec_deg: e.dec_deg }];
      await api.setScopeAuto({ enabled: !!scope?.scheduler?.enabled, targets });
      toast.success(`Added ${e.name} to the auto list.`);
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      adding = null;
    }
  }

  async function refreshScope() {
    try {
      scope = await api.getScope();
    } catch {
      // The next poll retries.
    }
  }
  refreshScope();

  const job = $derived(scope?.status?.job ?? null);
  const obs = $derived(scope?.observe ?? null);
  const observing = $derived(obs?.state === 'running');

  async function stopCapture() {
    try {
      await api.stopScope();
      toast.success('Stop sent.');
      await refreshScope();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  $effect(() => {
    const timer = setInterval(refreshScope, 5000);
    return () => clearInterval(timer);
  });

  async function load() {
    const min = minAlt;
    const max = maxMag;
    // Skip the refetch while either input is empty / mid-edit. The
    // effect that drives load() will fire again the moment the user
    // types a valid number, so this is intentionally silent.
    if (min == null || max == null || !Number.isFinite(min) || !Number.isFinite(max)) {
      return;
    }
    loading = true;
    error = null;
    try {
      const r = await api.getTonight({ min_alt: min, max_mag: max });
      data = r;
    } catch (e) {
      const msg = (e as Error).message;
      error = msg;
      // 400 from missing site config is the common case; let the toast
      // sit so the user notices the link to Settings.
      toast.error(msg);
    } finally {
      loading = false;
    }
  }

  // Only the altitude and magnitude limits change the server result.
  $effect(() => {
    void minAlt;
    void maxMag;
    load();
  });

  function matchesView(e: TonightEntry): boolean {
    if (view === 'captured') return e.session_count > 0;
    if (view === 'auto') return isOnAutoList(e);
    return true;
  }

  function isOnAutoList(e: TonightEntry): boolean {
    return !!scope?.targets?.some((t) => t.name === e.name);
  }

  const filteredEntries = $derived(
    (data?.entries ?? []).filter(
      (e) =>
        matchesView(e) &&
        (group === null || typeGroup(e.object_type) === group) &&
        matchesSearch(e, search),
    ),
  );
  const visibleEntries = $derived(filteredEntries.slice(0, limit));

  $effect(() => {
    void view;
    void group;
    void search;
    limit = PAGE_SIZE;
  });

  async function uploadImage(e: TonightEntry, input: HTMLInputElement) {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    try {
      await api.uploadTonightImage(e.name, file);
      failedThumbs[e.name] = undefined;
      toast.success(`Saved your image for ${displayName(e)}.`);
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function clearImage(e: TonightEntry) {
    try {
      await api.deleteTonightImage(e.name);
      await load();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  function fmtTime(d: Date): string {
    return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  function fmtLocalTime(iso: string | null): string {
    return iso ? fmtTime(new Date(iso)) : '—';
  }

  function fmtSummary(e: TonightEntry): string {
    const parts = [e.object_type];
    if (e.magnitude !== null) parts.push(`mag ${e.magnitude.toFixed(1)}`);
    if (e.size_arcmin !== null) parts.push(`${Math.round(e.size_arcmin)}′`);
    return parts.filter(Boolean).join(' · ');
  }

  function fmtUp(e: TonightEntry, d: TonightResponse): string {
    const w = upWindow(e.alt_curve_deg, d.min_alt_deg, d.dusk_utc, d.alt_curve_step_min);
    if (!w) return `${e.alt_now_deg.toFixed(0)}° now`;
    const peak = Math.max(...(e.alt_curve_deg ?? [0]));
    return `${fmtTime(w.start)} – ${fmtTime(w.end)} · peak ${peak.toFixed(0)}°`;
  }

  function fmtCaptured(entry: TonightEntry): string {
    if (entry.session_count === 0) return 'Not yet';
    const last = entry.last_session_at
      ? new Date(entry.last_session_at).toLocaleDateString()
      : '?';
    return `${entry.session_count} (last ${last})`;
  }

  const SPARK_W = 240;
  const SPARK_H = 44;
  const SPARK_PAD_Y = 2;

  /** Build SVG points for the altitude polyline (clipped to 0..90). */
  function sparkPoints(curve: number[]): string {
    if (curve.length < 2) return '';
    const dx = SPARK_W / (curve.length - 1);
    const yScale = (alt: number) => {
      // Clip below horizon to 0 so the line doesn't disappear off the
      // bottom of the box; alt > 90 is impossible but we clip anyway.
      const a = Math.max(0, Math.min(90, alt));
      return SPARK_H - SPARK_PAD_Y - (a / 90) * (SPARK_H - SPARK_PAD_Y * 2);
    };
    return curve.map((alt, i) => `${(i * dx).toFixed(1)},${yScale(alt).toFixed(1)}`).join(' ');
  }

  function sparkArea(curve: number[]): string {
    if (curve.length < 2) return '';
    const pts = sparkPoints(curve);
    return `0,${SPARK_H} ${pts} ${SPARK_W},${SPARK_H}`;
  }

  /** Y position of the min-alt threshold guide. */
  function thresholdY(min: number): number {
    const a = Math.max(0, Math.min(90, min));
    return SPARK_H - SPARK_PAD_Y - (a / 90) * (SPARK_H - SPARK_PAD_Y * 2);
  }

  /** Fraction along [dusk, dawn] for "now". Null when we're outside the
   *  window or the window itself is degenerate; the caller hides the
   *  marker in either case. */
  function nowFraction(d: TonightResponse): number | null {
    if (!d.dusk_utc || !d.dawn_utc) return null;
    const dusk = new Date(d.dusk_utc).getTime();
    const dawn = new Date(d.dawn_utc).getTime();
    const now = new Date(d.at_utc).getTime();
    if (dawn <= dusk) return null;
    const frac = (now - dusk) / (dawn - dusk);
    if (frac < 0 || frac > 1) return null;
    return frac;
  }

  function sparkTooltip(curve: number[]): string {
    const peak = Math.max(...curve);
    return `peak ${peak.toFixed(0)}° tonight`;
  }

  function fmtWindow(d: TonightResponse): string {
    if (!d.dusk_utc || !d.dawn_utc) return '24-hour daylight';
    return `Dark ${fmtLocalTime(d.dusk_utc)} – ${fmtLocalTime(d.dawn_utc)}`;
  }
</script>

<div
  class="header"
  class:has-pill={(obs && obs.state === 'running') || job?.state === 'running'}
>
  <h1>Tonight</h1>
  {#if (obs && obs.state === 'running') || job?.state === 'running'}
    <a class="pill" href="/scope">
      <span class="live-dot" aria-hidden="true"></span>
      {obs?.state === 'running' ? obs.target : 'Scope'}
      <span class="muted">
        {#if obs?.state === 'running'}{obs.step}{:else if job}{job.captured ?? 0}/{job.frames ?? 0}{/if}
      </span>
    </a>
  {/if}
  {#if data}
    <span class="meta muted small">
      {fmtWindow(data)}
    </span>
  {/if}
</div>

{#if error}
  <section class="panel error-panel">
    <p>
      Couldn't load Tonight: {error}
    </p>
    {#if /site location/i.test(error)}
      <p class="muted small">
        Set your latitude / longitude / elevation on the
        <a href="/settings">Settings</a> page, then come back.
      </p>
    {/if}
  </section>
{:else if loading && data === null}
  <p class="muted">Computing the sky…</p>
{:else if data}
  <input
    class="search"
    type="search"
    placeholder="Search"
    aria-label="Search targets"
    bind:value={search}
  />

  <div class="toolbar">
    <div class="tabs" role="tablist" aria-label="Target lists">
      {#each VIEWS as v (v.id)}
        <button
          type="button"
          role="tab"
          class="tab"
          class:active={view === v.id}
          aria-selected={view === v.id}
          onclick={() => (view = v.id)}
        >
          {v.label}
        </button>
      {/each}
    </div>
    <button
      type="button"
      class="filter-toggle"
      class:active={showFilters || group !== null}
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
          <input
            type="number"
            min={0}
            max={89}
            step={1}
            bind:value={minAlt}
            aria-label="Minimum altitude in degrees"
          />
          <span class="filter-unit">deg</span>
        </label>
        <label class="filter-num">
          <span class="filter-label">Max magnitude</span>
          <input
            type="number"
            min={-2}
            max={20}
            step={0.5}
            bind:value={maxMag}
            aria-label="Maximum magnitude (dimmer = larger)"
          />
        </label>
      </div>
      <div class="chips" role="group" aria-label="Object type">
        <button
          type="button"
          class="chip"
          class:active={group === null}
          onclick={() => (group = null)}
        >
          All types
        </button>
        {#each GROUPS as g (g.id)}
          <button
            type="button"
            class="chip"
            class:active={group === g.id}
            onclick={() => (group = group === g.id ? null : g.id)}
          >
            {g.label}
          </button>
        {/each}
      </div>
    </section>
  {/if}

  {#if filteredEntries.length === 0}
    <p class="muted empty">No targets match.</p>
  {:else}
    <ul class="targets">
      {#each visibleEntries as entry (entry.name)}
        {@const open = expanded === entry.name}
        <li class="target" class:open>
          <div class="row">
            <button
              type="button"
              class="row-main"
              aria-expanded={open}
              onclick={() => (expanded = open ? null : entry.name)}
            >
              <span class="thumb">
                {#if !failedThumbs[entry.name]}
                  <img
                    src={api.tonightThumbUrl(entry.name, entry.image_version)}
                    alt=""
                    width="64"
                    height="64"
                    loading="lazy"
                    decoding="async"
                    onerror={() => (failedThumbs[entry.name] = true)}
                  />
                {/if}
              </span>
              <span class="text">
                <span class="title">
                  <span class="name" class:captured={entry.session_count > 0}>
                    {displayName(entry)}
                  </span>
                  {#if entry.common_name}
                    <span class="common">{entry.common_name}</span>
                  {/if}
                </span>
                <span class="line muted">{fmtSummary(entry)}</span>
                <span class="line muted">{fmtUp(entry, data)}</span>
              </span>
            </button>
            {#if canQueue}
              <div class="actions">
                <button
                  type="button"
                  class="btn compact"
                  disabled={queuing !== null || observing}
                  onclick={() => queueTarget(entry)}
                >
                  {queuing === entry.name ? 'Pointing…' : 'Point'}
                </button>
                <button
                  type="button"
                  class="btn compact icon"
                  class:on={isOnAutoList(entry)}
                  disabled={adding !== null}
                  title="Add to the all-night auto list"
                  aria-label="Add {displayName(entry)} to the auto list"
                  onclick={() => addToAuto(entry)}
                >
                  {isOnAutoList(entry) ? '✓' : '+'}
                </button>
              </div>
            {/if}
          </div>
          {#if open}
            <div class="details">
              {#if entry.alt_curve_deg && entry.alt_curve_deg.length >= 2}
                <svg
                  class="spark"
                  viewBox="0 0 {SPARK_W} {SPARK_H}"
                  preserveAspectRatio="none"
                  role="img"
                  aria-label={sparkTooltip(entry.alt_curve_deg)}
                >
                  <line
                    class="spark-threshold"
                    x1="0"
                    x2={SPARK_W}
                    y1={thresholdY(data.min_alt_deg)}
                    y2={thresholdY(data.min_alt_deg)}
                  />
                  <polygon class="spark-fill" points={sparkArea(entry.alt_curve_deg)} />
                  <polyline class="spark-line" points={sparkPoints(entry.alt_curve_deg)} />
                  {#if nowFraction(data) !== null}
                    <line
                      class="spark-now"
                      x1={(nowFraction(data) ?? 0) * SPARK_W}
                      x2={(nowFraction(data) ?? 0) * SPARK_W}
                      y1="0"
                      y2={SPARK_H}
                    />
                  {/if}
                </svg>
              {/if}
              <dl>
                {#if entry.messier}<div><dt>Catalog</dt><dd>{entry.name}</dd></div>{/if}
                <div><dt>Alt now</dt><dd>{entry.alt_now_deg.toFixed(0)}°</dd></div>
                <div><dt>Transit</dt><dd>{fmtLocalTime(entry.transit_utc)}</dd></div>
                <div><dt>Hours up</dt><dd>{entry.hours_above_min_alt.toFixed(1)}h</dd></div>
                <div><dt>Constellation</dt><dd>{entry.constellation ?? '—'}</dd></div>
                <div><dt>Captured</dt><dd>{fmtCaptured(entry)}</dd></div>
              </dl>
              <div class="image-actions">
                <label class="btn compact">
                  Upload image
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    hidden
                    onchange={(ev) => uploadImage(entry, ev.currentTarget)}
                  />
                </label>
                {#if entry.image_version}
                  <button type="button" class="btn compact" onclick={() => clearImage(entry)}>
                    Use survey image
                  </button>
                {/if}
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
    {#if filteredEntries.length > limit}
      <button type="button" class="btn more" onclick={() => (limit += PAGE_SIZE)}>
        Show more ({filteredEntries.length - limit} left)
      </button>
    {/if}
    <p class="credit muted">Survey images: Digitized Sky Survey, STScI/NASA. Colour by CDS, hosted by ESA.</p>
  {/if}
{/if}

<style>
  .header {
    position: relative;
  }
  .header.has-pill .meta {
    padding-right: 8.5rem;
  }
  .pill {
    position: absolute;
    top: 0.25rem;
    right: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    max-width: 55%;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    padding: 0.25rem 0.7rem;
    font-size: 0.78rem;
    color: var(--fg);
    text-decoration: none;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elev);
  }
  .live-dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--good);
    box-shadow: 0 0 8px var(--good);
  }
  .btn.compact {
    min-height: 32px;
    padding: 0 0.7rem;
    font-size: 0.8rem;
  }
  .btn.icon {
    width: 32px;
    padding: 0;
    margin-left: 0.3rem;
    border-radius: 50%;
    font-size: 1rem;
  }
  .btn.icon.on {
    color: var(--accent);
    border-color: var(--accent);
  }
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
  .meta {
    font-family: var(--font-mono);
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
    transition: border-color 160ms ease, color 160ms ease;
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

  .search {
    width: 100%;
    box-sizing: border-box;
    margin-bottom: 0.9rem;
    padding: 0.55rem 0.9rem;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 999px;
    font: inherit;
  }
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.9rem;
  }
  .tabs {
    display: flex;
    gap: 1.1rem;
    min-width: 0;
  }
  .tab,
  .filter-toggle {
    appearance: none;
    padding: 0.25rem 0;
    background: none;
    border: 0;
    color: var(--fg-mute);
    font: inherit;
    cursor: pointer;
    white-space: nowrap;
  }
  .tab.active {
    color: var(--fg);
    font-weight: 600;
  }
  .filter-toggle {
    font-size: 0.85rem;
  }
  .filter-toggle.active {
    color: var(--accent);
  }
  .empty {
    margin-top: 2rem;
    text-align: center;
  }

  .targets {
    margin: 0;
    padding: 0;
    list-style: none;
  }
  .target {
    border-bottom: 1px solid var(--border-soft, rgba(255, 255, 255, 0.06));
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .row-main {
    appearance: none;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.8rem;
    padding: 0.65rem 0;
    background: none;
    border: 0;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  .thumb {
    flex: none;
    width: 64px;
    height: 64px;
    overflow: hidden;
    border-radius: 10px;
    background: radial-gradient(circle at 50% 45%, var(--bg-elev-2), var(--bg));
  }
  .thumb img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .text {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .line {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }
  .name {
    font-weight: 600;
  }
  .name.captured {
    color: var(--accent);
  }
  .common {
    margin-left: 0.3rem;
    color: var(--fg);
  }
  .line {
    font-size: 0.8rem;
  }
  .actions {
    flex: none;
    display: flex;
    align-items: center;
  }
  .details {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0.75rem;
    padding: 0 0 0.9rem;
  }
  .details dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(6.5rem, 1fr));
    gap: 0.6rem 1rem;
    margin: 0;
  }
  .details dt {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .details dd {
    margin: 0.1rem 0 0;
    font-size: 0.85rem;
  }
  .image-actions {
    display: flex;
    gap: 0.5rem;
    grid-column: 1 / -1;
  }
  .image-actions label {
    display: inline-flex;
    align-items: center;
    cursor: pointer;
  }
  .spark {
    display: block;
    width: 100%;
    height: 44px;
    overflow: visible;
  }
  .spark-threshold {
    stroke: var(--fg-mute);
    stroke-width: 0.5;
    stroke-dasharray: 2 2;
    opacity: 0.5;
    vector-effect: non-scaling-stroke;
  }
  .spark-fill {
    fill: var(--accent-soft, rgba(94, 234, 212, 0.18));
    stroke: none;
  }
  .spark-line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 1.25;
    stroke-linecap: round;
    stroke-linejoin: round;
    vector-effect: non-scaling-stroke;
  }
  .spark-now {
    stroke: var(--fg);
    stroke-width: 1;
    opacity: 0.6;
    vector-effect: non-scaling-stroke;
  }
  .more {
    display: block;
    margin: 1rem auto 0;
  }
  .credit {
    margin-top: 1.25rem;
    font-size: 0.7rem;
    text-align: center;
  }

  @media (min-width: 769px) {
    .details {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      align-items: center;
    }
  }
  @media (max-width: 420px) {
    .thumb {
      width: 56px;
      height: 56px;
    }
    .row-main {
      gap: 0.65rem;
    }
    .btn.compact {
      padding: 0 0.55rem;
    }
    .btn.icon {
      margin-left: 0.2rem;
    }
  }
</style>
