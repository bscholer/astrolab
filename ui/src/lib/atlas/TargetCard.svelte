<!--
  What the atlas shows about the selected object: the survey picture, the
  facts, tonight's altitude curve, and the scope actions.
-->
<script lang="ts">
  import { api, type TonightEntry, type TonightResponse } from '$lib/api';
  import { displayName, upWindow } from '$lib/tonight';
  import { horizon, radec, toAltAz } from './sky';
  import { TELE_FOV_DEG } from './atlas';

  let {
    entry,
    data,
    timeMs,
    compact = false,
    canQueue = false,
    busy = false,
    onAuto = false,
    onshoot,
    ontoggleauto,
    onclose,
    onexpand,
    queuedMosaic = null,
    onmosaic,
    onremovemosaic,
  }: {
    entry: TonightEntry;
    data: TonightResponse;
    timeMs: number;
    compact?: boolean;
    canQueue?: boolean;
    busy?: boolean;
    onAuto?: boolean;
    onshoot: () => void;
    ontoggleauto: () => void;
    onclose?: () => void;
    onexpand?: () => void;
    /** Panels of this target's mosaic on the auto list, when there is one. */
    queuedMosaic?: number | null;
    onmosaic?: () => void;
    onremovemosaic?: () => void;
  } = $props();

  let thumbFailed = $state(false);
  $effect(() => {
    void entry.name;
    thumbFailed = false;
  });

  const altAz = $derived(
    toAltAz(horizon(timeMs, data.site_latitude, data.site_longitude), radec(entry.ra_deg, entry.dec_deg)),
  );

  const facts = $derived(
    [
      entry.object_type,
      entry.constellation,
      entry.magnitude !== null ? `mag ${entry.magnitude.toFixed(1)}` : null,
      entry.size_arcmin !== null
        ? entry.minor_arcmin
          ? `${fmtArcmin(entry.size_arcmin)} × ${fmtArcmin(entry.minor_arcmin)}`
          : fmtArcmin(entry.size_arcmin)
        : null,
    ].filter(Boolean),
  );

  function fmtArcmin(a: number): string {
    return a >= 10 ? `${Math.round(a)}′` : `${a.toFixed(1)}′`;
  }

  function fmtTime(d: Date): string {
    return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }

  const upWin = $derived(upWindow(entry.alt_curve_deg, data.min_alt_deg, data.dusk_utc, data.alt_curve_step_min));
  const peak = $derived(entry.alt_curve_deg?.length ? Math.max(...entry.alt_curve_deg) : null);

  /** How the object sits in the tele frame, against its short side. */
  const fit = $derived.by(() => {
    if (!entry.size_arcmin) return null;
    const share = entry.size_arcmin / 60 / TELE_FOV_DEG[1];
    if (share > 1.1) return `Larger than the tele frame (${share.toFixed(1)}× its height)`;
    if (share > 0.35) return 'Fills the tele frame well';
    if (share < 0.08) return 'Small in the tele frame';
    return null;
  });

  const W = 260;
  const H = 54;
  function y(alt: number): number {
    return H - 2 - (Math.max(0, Math.min(90, alt)) / 90) * (H - 4);
  }
  const curve = $derived(entry.alt_curve_deg ?? []);
  const line = $derived(
    curve.length > 1 ? curve.map((a, i) => `${((i * W) / (curve.length - 1)).toFixed(1)},${y(a).toFixed(1)}`).join(' ') : '',
  );
  const marker = $derived.by(() => {
    if (!data.dusk_utc || !data.dawn_utc) return null;
    const d0 = new Date(data.dusk_utc).getTime();
    const d1 = new Date(data.dawn_utc).getTime();
    const f = (timeMs - d0) / (d1 - d0);
    return f >= 0 && f <= 1 ? f * W : null;
  });
</script>

<article class="card" class:compact>
  <div class="head">
    <button type="button" class="thumb" onclick={onexpand} tabindex={onexpand ? 0 : -1} aria-label="More about {displayName(entry)}">
      {#if !thumbFailed}
        <img
          src={api.tonightThumbUrl(entry.name, entry.image_version)}
          alt="Survey image of {displayName(entry)}"
          onerror={() => (thumbFailed = true)}
        />
      {/if}
    </button>
    <div class="title">
      <h2>{displayName(entry)}</h2>
      {#if entry.common_name}<p class="common">{entry.common_name}</p>{/if}
      {#if entry.messier && entry.name !== entry.messier}<p class="canon">{entry.name}</p>{/if}
      <p class="facts">{facts.join(' · ')}</p>
      <p class="now" class:down={altAz.alt < data.min_alt_deg}>
        {#if altAz.alt < 0}
          Below the horizon
        {:else}
          Alt {altAz.alt.toFixed(0)}° · Az {altAz.az.toFixed(0)}°
        {/if}
      </p>
    </div>
    {#if onclose}
      <button type="button" class="close" aria-label="Close" onclick={onclose}>×</button>
    {/if}
  </div>

  <div class="actions">
    {#if canQueue}
      <button type="button" class="primary" disabled={busy} onclick={onshoot}>Point and shoot</button>
    {/if}
    <button type="button" class:on={onAuto} disabled={busy} onclick={ontoggleauto}>
      {onAuto ? '✓ On the auto list' : '+ Auto list'}
    </button>
    {#if onmosaic}
      <button type="button" disabled={busy} onclick={onmosaic}>
        {queuedMosaic ? 'Edit mosaic' : 'Mosaic'}
      </button>
    {/if}
    <a class="link" href="/tonight?q={encodeURIComponent(entry.messier ?? entry.name)}">In Tonight</a>
  </div>
  {#if queuedMosaic}
    <p class="queued">
      Mosaic of {queuedMosaic} panels on the auto list.
      {#if onremovemosaic}
        <button type="button" class="text" disabled={busy} onclick={onremovemosaic}>Remove</button>
      {/if}
    </p>
  {/if}

  {#if !compact}
    <div class="tonight">
      <p class="when">
        {#if upWin}
          Up {fmtTime(upWin.start)} – {fmtTime(upWin.end)}{#if peak !== null}, peak {peak.toFixed(0)}°{/if}
        {:else}
          Stays below {data.min_alt_deg.toFixed(0)}° tonight
        {/if}
        {#if entry.session_count > 0}
          <span class="captured">· captured {entry.session_count}×</span>
        {/if}
      </p>
      {#if line}
        <svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">
          <line class="limit" x1="0" x2={W} y1={y(data.min_alt_deg)} y2={y(data.min_alt_deg)} />
          <polygon class="fill" points="0,{H} {line} {W},{H}" />
          <polyline class="curve" points={line} />
          {#if marker !== null}<line class="mark" x1={marker} x2={marker} y1="0" y2={H} />{/if}
        </svg>
        <div class="axis muted">
          <span>{data.dusk_utc ? fmtTime(new Date(data.dusk_utc)) : ''}</span>
          <span>{data.dawn_utc ? fmtTime(new Date(data.dawn_utc)) : ''}</span>
        </div>
      {/if}
      {#if fit}<p class="fit muted">{fit}</p>{/if}
    </div>
  {/if}
</article>

<style>
  .card {
    display: grid;
    gap: 0.8rem;
  }
  .head {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 0.85rem;
    align-items: start;
  }
  .thumb {
    width: 112px;
    height: 112px;
    padding: 0;
    border-radius: var(--radius);
    overflow: hidden;
    background: var(--bg);
    border: 1px solid var(--border);
    cursor: default;
  }
  .compact .thumb {
    width: 72px;
    height: 72px;
    cursor: pointer;
  }
  .thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .title {
    min-width: 0;
  }
  h2 {
    margin: 0;
    font-size: 1.2rem;
    line-height: 1.2;
  }
  .compact h2 {
    font-size: 1.05rem;
  }
  .title p {
    margin: 0.15rem 0 0;
  }
  .common {
    color: var(--fg);
    opacity: 0.85;
  }
  .canon {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--fg-mute);
  }
  .facts {
    color: var(--fg-mute);
    font-size: 0.82rem;
  }
  .now {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    color: var(--accent);
  }
  .now.down {
    color: var(--warn);
  }
  .close {
    width: 32px;
    height: 32px;
    padding: 0;
    font-size: 1.2rem;
    line-height: 1;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }
  .actions button {
    font-size: 0.85rem;
    padding: 0.4rem 0.85rem;
  }
  .actions .primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: var(--accent);
    font-weight: 600;
  }
  .actions .on {
    color: var(--accent);
    border-color: var(--accent);
  }
  .queued {
    margin: -0.3rem 0 0;
    font-size: 0.82rem;
    color: var(--warn);
  }
  .text {
    border: none;
    background: none;
    padding: 0 0.2rem;
    font-size: 0.82rem;
    color: var(--fg-mute);
    text-decoration: underline;
  }
  .link {
    font-size: 0.85rem;
    color: var(--fg-mute);
    margin-left: auto;
  }
  .link:hover {
    color: var(--fg);
  }
  .tonight p {
    margin: 0;
  }
  .when {
    font-size: 0.88rem;
    margin-bottom: 0.4rem !important;
  }
  .captured {
    color: var(--good);
  }
  .spark {
    width: 100%;
    height: 54px;
    display: block;
  }
  .spark .fill {
    fill: var(--accent-soft);
  }
  .spark .curve {
    fill: none;
    stroke: var(--accent);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
  }
  .spark .limit {
    stroke: var(--warn);
    stroke-dasharray: 3 3;
    opacity: 0.6;
    vector-effect: non-scaling-stroke;
  }
  .spark .mark {
    stroke: var(--fg);
    opacity: 0.7;
    vector-effect: non-scaling-stroke;
  }
  .axis {
    display: flex;
    justify-content: space-between;
    font-size: 0.72rem;
    font-family: var(--font-mono);
  }
  .fit {
    margin-top: 0.4rem !important;
    font-size: 0.82rem;
  }
</style>
