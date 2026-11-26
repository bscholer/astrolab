<!--
  The Tonight page's list view: the ranked targets, one row each. The open
  row is the page's selection, the same object the map shows, and it
  carries the altitude curve and the actions.
-->
<script lang="ts">
  import { tick, untrack } from 'svelte';
  import { api, type TonightEntry, type TonightResponse } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { displayName, skylineArea, upWindow } from '$lib/tonight';
  import { fmtHours } from '$lib/atlas/atlas';

  let {
    data,
    entries,
    selected,
    visible = true,
    planned,
    mosaics,
    canQueue = false,
    busy = false,
    pointing = null,
    onselect,
    onshoot,
    ontoggleauto,
    onmap,
    onmosaic,
    onimagechanged,
  }: {
    data: TonightResponse;
    /** The rows to show, filtered and ranked. */
    entries: TonightEntry[];
    selected: string | null;
    visible?: boolean;
    /** Names in the plan. */
    planned: Set<string>;
    /** Panels in the plan per object name, for objects with a mosaic. */
    mosaics: Map<string, number>;
    canQueue?: boolean;
    busy?: boolean;
    /** The object being pointed at right now. */
    pointing?: string | null;
    onselect: (name: string | null) => void;
    onshoot: (e: TonightEntry) => void;
    ontoggleauto: (e: TonightEntry) => void;
    onmap: (e: TonightEntry) => void;
    onmosaic: (e: TonightEntry) => void;
    onimagechanged: () => Promise<void> | void;
  } = $props();

  const PAGE_SIZE = 40;
  let limit = $state(PAGE_SIZE);
  let failedThumbs = $state<Record<string, true | undefined>>({});
  let listEl = $state<HTMLUListElement | null>(null);

  // A new filter starts at the top again; opening a row deep down grows the page.
  $effect(() => {
    void entries;
    untrack(() => (limit = PAGE_SIZE));
  });
  $effect(() => {
    const at = entries.findIndex((e) => e.name === selected);
    if (at >= untrack(() => limit)) limit = at + 1;
  });
  // An object picked on the map or in the plan that the filters leave out
  // still shows, at the top.
  const pinned = $derived(
    selected && !entries.some((e) => e.name === selected)
      ? (data.entries.find((e) => e.name === selected) ?? null)
      : null,
  );
  const shownEntries = $derived(pinned ? [pinned, ...entries.slice(0, limit)] : entries.slice(0, limit));

  // Coming back from the map, scroll to the object picked there.
  let scrolledTo: string | null = null;
  $effect(() => {
    if (!visible || !selected || selected === scrolledTo) return;
    scrolledTo = selected;
    tick().then(() => {
      const el = listEl?.querySelector(`[data-name="${CSS.escape(selected)}"]`);
      el?.scrollIntoView({ block: 'center' });
    });
  });

  function toggle(name: string) {
    scrolledTo = selected === name ? null : name;
    onselect(selected === name ? null : name);
  }

  async function uploadImage(e: TonightEntry, input: HTMLInputElement) {
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    try {
      await api.uploadTonightImage(e.name, file);
      failedThumbs[e.name] = undefined;
      toast.success(`Saved your image for ${displayName(e)}.`);
      await onimagechanged();
    } catch (err) {
      toast.error((err as Error).message);
    }
  }

  async function clearImage(e: TonightEntry) {
    try {
      await api.deleteTonightImage(e.name);
      await onimagechanged();
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

  function fmtUp(e: TonightEntry): string {
    const w = upWindow(e.alt_curve_deg, data.min_alt_deg, data.dusk_utc, data.alt_curve_step_min, e.skyline_curve_deg);
    if (!w) {
      if (e.hours_above_min_alt > 0 && e.skyline_curve_deg) return 'Behind the trees or house tonight';
      return e.alt_now_deg >= data.min_alt_deg ? `${e.alt_now_deg.toFixed(0)}° now` : 'Not up tonight';
    }
    const peak = Math.max(...(e.alt_curve_deg ?? [0]));
    return `${fmtTime(w.start)} – ${fmtTime(w.end)} · peak ${peak.toFixed(0)}°`;
  }

  function fmtCaptured(e: TonightEntry): string {
    if (e.shot_s > 0) return `${fmtHours(e.shot_s)} shot`;
    if (e.session_count === 0) return 'Not yet';
    const last = e.last_session_at ? new Date(e.last_session_at).toLocaleDateString() : '?';
    return `${e.session_count} (last ${last})`;
  }

  const SPARK_W = 240;
  const SPARK_H = 44;
  const SPARK_PAD_Y = 2;

  function y(alt: number): number {
    const a = Math.max(0, Math.min(90, alt));
    return SPARK_H - SPARK_PAD_Y - (a / 90) * (SPARK_H - SPARK_PAD_Y * 2);
  }

  /** SVG points for the altitude polyline, clipped to 0..90. */
  function sparkPoints(curve: number[]): string {
    if (curve.length < 2) return '';
    const dx = SPARK_W / (curve.length - 1);
    return curve.map((alt, i) => `${(i * dx).toFixed(1)},${y(alt).toFixed(1)}`).join(' ');
  }

  /** Fraction along [dusk, dawn] for now; null outside the night. */
  const nowFrac = $derived.by(() => {
    if (!data.dusk_utc || !data.dawn_utc) return null;
    const dusk = new Date(data.dusk_utc).getTime();
    const dawn = new Date(data.dawn_utc).getTime();
    const f = (new Date(data.at_utc).getTime() - dusk) / (dawn - dusk);
    return dawn > dusk && f >= 0 && f <= 1 ? f : null;
  });
</script>

{#if shownEntries.length === 0}
  <p class="muted empty">No targets match.</p>
{:else}
  <ul class="targets" bind:this={listEl}>
    {#each shownEntries as entry (entry.name)}
      {@const open = selected === entry.name}
      {@const inPlan = planned.has(entry.name)}
      {@const panels = mosaics.get(entry.name) ?? 0}
      <li class="target" class:open data-name={entry.name}>
        <div class="row">
          <button type="button" class="row-main" aria-expanded={open} onclick={() => toggle(entry.name)}>
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
                <span class="name" class:captured={entry.session_count > 0 || entry.shot_s > 0}>
                  {displayName(entry)}
                </span>
                {#if entry.common_name}<span class="common">{entry.common_name}</span>{/if}
              </span>
              <span class="line muted">{fmtSummary(entry)}</span>
              <span class="line muted">
                {fmtUp(entry)}{#if panels}<span class="in-plan"> · {panels}-panel mosaic planned</span>{/if}
              </span>
            </span>
          </button>
          <div class="actions">
            {#if canQueue}
              <button type="button" class="btn compact" disabled={busy} onclick={() => onshoot(entry)}>
                {pointing === entry.name ? 'Pointing…' : 'Point'}
              </button>
            {/if}
            <button
              type="button"
              class="btn compact icon"
              class:on={inPlan}
              disabled={busy}
              title={inPlan ? 'Take out of the plan' : 'Add to the plan for Run all night'}
              aria-label="{inPlan ? 'Take' : 'Add'} {displayName(entry)} {inPlan ? 'out of' : 'to'} the plan"
              onclick={() => ontoggleauto(entry)}
            >
              {inPlan ? '✓' : '+'}
            </button>
          </div>
        </div>
        {#if open}
          <div class="details">
            {#if entry.alt_curve_deg && entry.alt_curve_deg.length >= 2}
              <svg class="spark" viewBox="0 0 {SPARK_W} {SPARK_H}" preserveAspectRatio="none" role="img"
                aria-label="peak {Math.max(...entry.alt_curve_deg).toFixed(0)}° tonight">
                <line class="spark-threshold" x1="0" x2={SPARK_W} y1={y(data.min_alt_deg)} y2={y(data.min_alt_deg)} />
                <polygon class="spark-fill" points="0,{SPARK_H} {sparkPoints(entry.alt_curve_deg)} {SPARK_W},{SPARK_H}" />
                {#if entry.skyline_curve_deg}
                  <polygon class="spark-blocked" points={skylineArea(entry.skyline_curve_deg, SPARK_W, SPARK_H, y)}>
                    <title>Houses and trees along its path, from the scope's panorama</title>
                  </polygon>
                {/if}
                <polyline class="spark-line" points={sparkPoints(entry.alt_curve_deg)} />
                {#if nowFrac !== null}
                  <line class="spark-now" x1={nowFrac * SPARK_W} x2={nowFrac * SPARK_W} y1="0" y2={SPARK_H} />
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
            <div class="more-actions">
              <button type="button" class="btn compact" onclick={() => onmap(entry)}>On the map</button>
              <button type="button" class="btn compact" disabled={busy} onclick={() => onmosaic(entry)}>
                {panels ? 'Edit mosaic' : 'Mosaic'}
              </button>
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
                <button type="button" class="btn compact" onclick={() => clearImage(entry)}>Use survey image</button>
              {/if}
            </div>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
  {#if entries.length > limit}
    <button type="button" class="btn more" onclick={() => (limit += PAGE_SIZE)}>
      Show more ({entries.length - limit} left)
    </button>
  {/if}
  <p class="credit muted">Survey images: Digitized Sky Survey, STScI/NASA. Colour by CDS, hosted by ESA.</p>
{/if}

<style>
  .muted {
    color: var(--fg-mute);
  }
  .empty {
    margin-top: 2rem;
    text-align: center;
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
    font-size: 0.8rem;
  }
  .in-plan {
    color: var(--accent);
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
  .more-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    grid-column: 1 / -1;
  }
  .more-actions label {
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
  .spark-blocked {
    fill: rgba(236, 72, 153, 0.28);
    stroke: rgba(236, 72, 153, 0.7);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
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
