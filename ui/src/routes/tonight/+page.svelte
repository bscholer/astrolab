<!--
  Tonight: what's worth pointing at right now from the configured site.

  Combines the OpenNGC catalog with the user's existing library so each
  visible target shows session count + last-captured date inline. The
  table is sorted by descending altitude (highest in the sky first); a
  filter rail trims by object type, magnitude, and minimum altitude.

  Tonight is read-only: it doesn't queue captures or jobs. The page is
  for sitting on the couch with a coffee at 7pm and deciding what's
  worth taking the scope out for.
-->
<script lang="ts">
  import { api, type TonightEntry, type TonightResponse } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  let data = $state<TonightResponse | null>(null);
  let loading = $state(false);
  let error = $state<string | null>(null);

  // Filter state. Initial values mirror the server defaults so a fresh
  // page load and a no-filter API call land on the same row set.
  let minAlt = $state(20);
  let maxMag = $state(12);
  // Type chip: null = "all". Single-select keeps the chip rail simple.
  let typeFilter = $state<string | null>(null);
  // Captured-only toggle: when on, hide rows the user has never imaged.
  let capturedOnly = $state(false);

  async function load() {
    loading = true;
    error = null;
    try {
      const r = await api.getTonight({ min_alt: minAlt, max_mag: maxMag });
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

  // Single effect: refetch whenever a server-affecting filter changes.
  // The reactive reads of minAlt/maxMag inside this block register them
  // as dependencies, so editing either input triggers the call. Type
  // and captured-only filters are pure client-side (just re-derive
  // filteredEntries) and don't need a roundtrip.
  $effect(() => {
    void minAlt;
    void maxMag;
    load();
  });

  // Distinct object_type values across the current row set, for the
  // chip rail. Sorted alphabetically, "(none)" rolled into "Other" so
  // weird OpenNGC type-code escapees aren't a separate chip.
  const types = $derived.by(() => {
    if (!data) return [];
    const set = new Set<string>();
    for (const e of data.entries) {
      set.add(e.object_type ?? 'Other');
    }
    return Array.from(set).sort();
  });

  const filteredEntries = $derived.by(() => {
    if (!data) return [] as TonightEntry[];
    return data.entries.filter((e) => {
      if (typeFilter !== null && (e.object_type ?? 'Other') !== typeFilter) {
        return false;
      }
      if (capturedOnly && e.session_count === 0) return false;
      return true;
    });
  });

  // Local-time ISO formatting for the transit column. The server returns
  // UTC and we render in the user's wall-clock; saves a per-target tz
  // conversion server-side and is more honest about who owns "local".
  function fmtLocalTime(iso: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function fmtMag(mag: number | null): string {
    if (mag === null) return '—';
    return mag.toFixed(1);
  }

  function fmtAlt(deg: number): string {
    return `${deg.toFixed(0)}°`;
  }

  function fmtHours(h: number): string {
    if (h === 0) return '—';
    return `${h.toFixed(1)}h`;
  }

  function fmtCaptured(entry: TonightEntry): string {
    if (entry.session_count === 0) return '—';
    const last = entry.last_session_at
      ? new Date(entry.last_session_at).toLocaleDateString()
      : '?';
    return `${entry.session_count} (last ${last})`;
  }

  // Sparkline geometry. Kept module-local so the SVG path math stays out
  // of the markup. Width is intentionally narrow because the column has
  // to fit a 5-column-wide table without forcing horizontal scroll.
  const SPARK_W = 110;
  const SPARK_H = 28;
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
    const dusk = new Date(d.dusk_utc).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    });
    const dawn = new Date(d.dawn_utc).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    });
    return `${dusk} – ${dawn}`;
  }
</script>

<div class="header">
  <h1>Tonight</h1>
  {#if data}
    <span class="meta muted small">
      {data.entries.length} visible · night {fmtWindow(data)}
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
      <label class="check">
        <input type="checkbox" bind:checked={capturedOnly} />
        <span>Captured only</span>
      </label>
    </div>
    {#if types.length > 0}
      <div class="chips" role="tablist" aria-label="Filter by type">
        <button
          type="button"
          class="chip"
          class:active={typeFilter === null}
          onclick={() => (typeFilter = null)}
        >
          All
        </button>
        {#each types as t (t)}
          <button
            type="button"
            class="chip"
            class:active={typeFilter === t}
            onclick={() => (typeFilter = typeFilter === t ? null : t)}
          >
            {t}
          </button>
        {/each}
      </div>
    {/if}
  </section>

  {#if filteredEntries.length === 0}
    <p class="muted">No targets match the current filters.</p>
  {:else}
    <section class="panel table-panel">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Common name</th>
            <th>Type</th>
            <th class="num">Mag</th>
            <th class="num">Alt now</th>
            <th class="num">Transit</th>
            <th class="num">Hours up</th>
            <th>Tonight</th>
            <th>Captured</th>
          </tr>
        </thead>
        <tbody>
          {#each filteredEntries as entry (entry.name)}
            <tr class:captured={entry.session_count > 0}>
              <td class="name">{entry.name}</td>
              <td>{entry.common_name ?? '—'}</td>
              <td class="muted">{entry.object_type ?? '—'}</td>
              <td class="num">{fmtMag(entry.magnitude)}</td>
              <td class="num">{fmtAlt(entry.alt_now_deg)}</td>
              <td class="num">{fmtLocalTime(entry.transit_utc)}</td>
              <td class="num">{fmtHours(entry.hours_above_min_alt)}</td>
              <td class="spark-cell">
                {#if entry.alt_curve_deg && entry.alt_curve_deg.length >= 2 && data}
                  <svg
                    class="spark"
                    width={SPARK_W}
                    height={SPARK_H}
                    viewBox="0 0 {SPARK_W} {SPARK_H}"
                    role="img"
                    aria-label={sparkTooltip(entry.alt_curve_deg)}
                  >
                    <title>{sparkTooltip(entry.alt_curve_deg)}</title>
                    <line
                      class="spark-threshold"
                      x1="0"
                      x2={SPARK_W}
                      y1={thresholdY(data.min_alt_deg)}
                      y2={thresholdY(data.min_alt_deg)}
                    />
                    <polygon
                      class="spark-fill"
                      points={sparkArea(entry.alt_curve_deg)}
                    />
                    <polyline
                      class="spark-line"
                      points={sparkPoints(entry.alt_curve_deg)}
                    />
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
                {:else}
                  <span class="muted">—</span>
                {/if}
              </td>
              <td class="captured-cell">{fmtCaptured(entry)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/if}
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
  .check {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--fg-mute);
    font-size: 0.85rem;
  }
  .check input {
    accent-color: var(--accent);
  }

  .chips {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .chip {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.7rem;
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

  .table-panel {
    padding: 0;
    overflow: hidden;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.88rem;
  }
  thead th {
    text-align: left;
    padding: 0.6rem 0.85rem;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
    background: var(--bg-elev);
    position: sticky;
    top: 0;
  }
  tbody td {
    padding: 0.5rem 0.85rem;
    border-top: 1px solid var(--border-soft, rgba(255, 255, 255, 0.04));
  }
  tbody tr:hover {
    background: var(--bg-elev-2);
  }
  /* Captured rows pop just enough to read at a glance, without
     overwhelming the rest of the table. */
  tbody tr.captured td.name {
    color: var(--accent);
    font-weight: 600;
  }
  tbody tr.captured .captured-cell {
    color: var(--accent);
  }
  td.name {
    font-family: var(--font-mono);
  }
  th.num,
  td.num {
    text-align: right;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  /* Sparkline cell: keep the cell tight and the SVG vertically aligned
     with the numeric columns above. block display drops the inline-svg
     baseline gap so the curve sits where the eye expects. */
  .spark-cell {
    width: 1%;
    white-space: nowrap;
  }
  .spark {
    display: block;
    overflow: visible;
  }
  /* Threshold guide (min-alt): dim, dashed, behind the curve. Helps the
     eye gauge how much of the night the target spends above the
     filter cutoff without needing the Hours-up column to confirm. */
  .spark-threshold {
    stroke: var(--fg-mute);
    stroke-width: 0.5;
    stroke-dasharray: 2 2;
    opacity: 0.5;
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
  }
  /* Vertical "now" tick lets the user place the moment within the curve
     at a glance: still rising, near peak, on the way down. */
  .spark-now {
    stroke: var(--fg);
    stroke-width: 1;
    opacity: 0.6;
  }
  /* Captured rows pull the curve toward the captured-row color so it
     reads as part of the same row group instead of a separate accent. */
  tbody tr.captured .spark-line {
    stroke: var(--accent);
  }
</style>
