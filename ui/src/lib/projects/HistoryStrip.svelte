<!--
  HistoryStrip: horizontal scroll rail of versioned history entries.

  Each entry is a revert button plus a publish star and a compare-slot
  toggle. The strip auto-scrolls to the right edge (newest) on mount
  and whenever history grows.

  Compare state is threaded through via callback props so CompareController
  remains the single source of truth.
-->
<script lang="ts">
  import { shortAgo } from '$lib/format';
  import type { Project, ProjectHistoryEntry, HistoryQualitySummary } from '$lib/api';

  type Props = {
    project: Project;
    compareA: number | null;
    compareB: number | null;
    compareLoading: boolean;
    publishBusy: Record<number, boolean>;
    onRevert: (seq: number) => void;
    onToggleCompareSlot: (seq: number) => void;
    onTogglePublished: (seq: number, currentlyPublished: boolean) => void;
    onOpenCompare: () => void;
    onClearCompare: () => void;
  };

  let {
    project,
    compareA,
    compareB,
    compareLoading,
    publishBusy,
    onRevert,
    onToggleCompareSlot,
    onTogglePublished,
    onOpenCompare,
    onClearCompare,
  }: Props = $props();

  let historyEl = $state<HTMLOListElement | null>(null);

  // Active popover: anchored to the dot row's viewport rect. Rendered as a
  // section-level fixed-position panel so the strip's overflow-x:auto
  // (which forces overflow-y clip per spec) can't crop it.
  type DotPop = {
    seq: number;
    rect: DOMRect;
    q: HistoryQualitySummary | null;
  };
  let activeDotPop = $state<DotPop | null>(null);
  function openDotPop(e: MouseEvent | FocusEvent, h: ProjectHistoryEntry) {
    const target = e.currentTarget as HTMLElement | null;
    if (!target) return;
    activeDotPop = {
      seq: h.seq,
      rect: target.getBoundingClientRect(),
      q: h.quality_summary ?? null,
    };
  }
  function closeDotPop() {
    activeDotPop = null;
  }

  // Auto-scroll to right edge on mount and on each new entry.
  $effect(() => {
    const len = project.history.length;
    if (len === 0 || !historyEl) return;
    // Defer one frame so the new <li> is in the DOM before measuring scrollWidth.
    requestAnimationFrame(() => {
      if (historyEl) historyEl.scrollLeft = historyEl.scrollWidth;
    });
  });

  function shortHistoryLabel(label: string | null): string {
    if (!label) return '';
    return label.length > 80 ? label.slice(0, 77) + '...' : label;
  }

  function slotFor(seq: number): 'A' | 'B' | null {
    if (compareA === seq) return 'A';
    if (compareB === seq) return 'B';
    return null;
  }

  function fmtIntegration(s: number): string {
    return s >= 3600 ? (s / 3600).toFixed(1) + 'h' : (s / 60).toFixed(0) + 'm';
  }
  function fmtSmall(n: number | null | undefined): string {
    // Decimal with trimmed trailing zeros. Pipeline values live in [0, 1]
    // (normalized) and tend to be very small; scientific notation reads
    // poorly to most users, so stretch the precision to 6 decimals and
    // strip the noise.
    if (n === null || n === undefined || !Number.isFinite(n)) return '—';
    if (n === 0) return '0';
    return n.toFixed(6).replace(/\.?0+$/, '');
  }
  function fmtFwhm(n: number | null): string {
    if (n === null) return '—';
    return n.toFixed(2) + ' px';
  }

  // Compute per-metric medians from entries that have quality_summary.
  const qualityMedians = $derived.by(() => {
    const noises: number[] = [];
    const sharpnesses: number[] = [];
    const integrations: number[] = [];
    for (const h of project.history) {
      const q = h.quality_summary;
      if (!q) continue;
      noises.push(q.noise);
      sharpnesses.push(q.sharpness);
      if (q.integration_s !== null) integrations.push(q.integration_s);
    }
    function median(arr: number[]): number | null {
      if (arr.length === 0) return null;
      const sorted = [...arr].sort((a, b) => a - b);
      const mid = Math.floor(sorted.length / 2);
      return sorted.length % 2 === 0
        ? (sorted[mid - 1] + sorted[mid]) / 2
        : sorted[mid];
    }
    return {
      noise: median(noises),
      sharpness: median(sharpnesses),
      integration: median(integrations),
      noiseCount: noises.length,
      sharpnessCount: sharpnesses.length,
      integrationCount: integrations.length,
    };
  });

  type DotColor = 'good' | 'warn' | 'bad' | 'none';

  function noiseDotColor(q: HistoryQualitySummary, m: number | null, count: number): DotColor {
    if (m === null || count < 2) return 'none';
    // Lower noise is better.
    if (q.noise <= m * 0.9) return 'good';
    if (q.noise <= m * 1.1) return 'warn';
    return 'bad';
  }
  function sharpnessDotColor(q: HistoryQualitySummary, m: number | null, count: number): DotColor {
    if (m === null || count < 2) return 'none';
    // Higher sharpness is better.
    if (q.sharpness >= m * 1.1) return 'good';
    if (q.sharpness >= m * 0.9) return 'warn';
    return 'bad';
  }
  function integrationDotColor(q: HistoryQualitySummary, m: number | null, count: number): DotColor {
    if (q.integration_s === null || m === null || count < 2) return 'none';
    // Higher integration is better.
    if (q.integration_s >= m * 1.1) return 'good';
    if (q.integration_s >= m * 0.9) return 'warn';
    return 'bad';
  }

</script>

<section class="history">
  <div class="history-head">
    <h2 class="section-h">
      History <span class="muted small">({project.history.length})</span>
    </h2>
    {#if compareA !== null || compareB !== null}
      <span class="compare-status muted small">
        Compare:
        {#if compareA !== null}<span class="slot a">A=v{compareA + 1}</span>{/if}
        {#if compareB !== null}<span class="slot b">B=v{compareB + 1}</span>{/if}
        {#if compareA !== null && compareB !== null}
          <button type="button" class="ghost-btn" onclick={onOpenCompare}>Open</button>
        {/if}
        <button type="button" class="ghost-btn" onclick={onClearCompare}>Clear</button>
      </span>
    {/if}
  </div>
  <ol class="history-strip" bind:this={historyEl}>
    {#each project.history as h (h.seq)}
      {@const slot = slotFor(h.seq)}
      {@const q = h.quality_summary ?? null}
      {@const meds = qualityMedians}
      {@const iColor = q ? integrationDotColor(q, meds.integration, meds.integrationCount) : 'none'}
      {@const nColor = q ? noiseDotColor(q, meds.noise, meds.noiseCount) : 'none'}
      {@const sColor = q ? sharpnessDotColor(q, meds.sharpness, meds.sharpnessCount) : 'none'}
      <li
        class="hist-entry"
        class:active={h.seq === project.current_seq}
        class:slot-a={slot === 'A'}
        class:slot-b={slot === 'B'}
        class:failed={h.failed}
      >
        <button type="button" onclick={() => onRevert(h.seq)} title={h.label ?? ''}>
          <span class="hist-seq muted">v{h.seq + 1}</span>
          <span class="hist-label">{shortHistoryLabel(h.label)}</span>
          <span class="hist-time muted small">{shortAgo(h.created_at)}</span>
          <span
            class="dot-row"
            aria-label="Quality summary"
            onmouseenter={(e) => openDotPop(e, h)}
            onmouseleave={closeDotPop}
            onfocusin={(e) => openDotPop(e, h)}
            onfocusout={closeDotPop}
            tabindex="0"
            role="button"
          >
            <span class="qdot qdot-{iColor}"></span>
            <span class="qdot qdot-{nColor}"></span>
            <span class="qdot qdot-{sColor}"></span>
          </span>
        </button>
        <button
          type="button"
          class="publish-toggle"
          class:on={h.published}
          disabled={publishBusy[h.seq]}
          aria-pressed={h.published}
          aria-label={h.published
            ? `Unpublish v${h.seq + 1} from gallery`
            : `Publish v${h.seq + 1} to gallery`}
          title={h.published ? 'In gallery - click to unpublish' : 'Publish to gallery'}
          onclick={() => onTogglePublished(h.seq, h.published)}
        >
          {#if h.published}
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
              <path
                fill="currentColor"
                d="M12 2.5l2.95 5.98 6.6.96-4.78 4.66 1.13 6.58L12 17.6l-5.9 3.1 1.13-6.58L2.45 9.44l6.6-.96L12 2.5z"
              />
            </svg>
          {:else}
            <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
              <path
                fill="none"
                stroke="currentColor"
                stroke-width="1.6"
                stroke-linejoin="round"
                d="M12 2.5l2.95 5.98 6.6.96-4.78 4.66 1.13 6.58L12 17.6l-5.9 3.1 1.13-6.58L2.45 9.44l6.6-.96L12 2.5z"
              />
            </svg>
          {/if}
        </button>
        <button
          type="button"
          class="compare-toggle"
          class:armed={slot !== null}
          disabled={compareLoading}
          aria-pressed={slot !== null}
          aria-label={slot
            ? `Clear compare slot ${slot} (v${h.seq + 1})`
            : compareA === null
              ? `Pick v${h.seq + 1} as compare A`
              : `Pick v${h.seq + 1} as compare B`}
          title={slot
            ? `Compare slot ${slot} - click to clear`
            : compareA === null
              ? 'Pick as compare A'
              : 'Pick as compare B'}
          onclick={() => onToggleCompareSlot(h.seq)}
        >
          {#if slot}
            <span class="compare-slot-letter">{slot}</span>
          {:else}
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <rect x="1.5" y="2.5" width="13" height="11" rx="2"
                fill="none" stroke="currentColor" stroke-width="1.4"/>
              <line x1="8" y1="3" x2="8" y2="13" stroke="currentColor" stroke-width="1.4"/>
            </svg>
          {/if}
        </button>
      </li>
    {/each}
  </ol>

  {#if activeDotPop}
    <!-- Section-level so the strip's overflow-x:auto (which clips both
         axes per spec) can't crop us. position:fixed anchors to the dot
         row's viewport rect; on horizontal scroll mouseleave will close. -->
    <div
      class="dot-popover"
      role="tooltip"
      style:top="{activeDotPop.rect.bottom + 8}px"
      style:left="{Math.max(8, activeDotPop.rect.left - 4)}px"
    >
      {#if activeDotPop.q}
        {@const qp = activeDotPop.q}
        <div class="dp-row">
          <span class="dp-label">Integration</span>
          <span class="dp-val">{qp.integration_s !== null ? fmtIntegration(qp.integration_s) : '—'}</span>
        </div>
        <div class="dp-def">Total useful exposure across sessions; longer = more depth</div>
        <div class="dp-row">
          <span class="dp-label">Noise</span>
          <span class="dp-val">{fmtSmall(qp.noise)}</span>
        </div>
        <div class="dp-def">Background standard deviation; lower = cleaner</div>
        <div class="dp-row">
          <span class="dp-label">Sharpness</span>
          <span class="dp-val">{fmtSmall(qp.sharpness)}{qp.fwhm_px !== null ? ' · ' + fmtFwhm(qp.fwhm_px) : ''}</span>
        </div>
        <div class="dp-def">Laplacian variance + Siril findstar FWHM; higher = sharper</div>
      {:else}
        <div class="dp-def">No quality data for this version</div>
      {/if}
    </div>
  {/if}
</section>

<style>
  .section-h {
    margin: 1.25rem 0 0.5rem;
    font-size: 1rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }
  .muted { color: var(--fg-mute, #888); }
  .small { font-size: 0.85em; }

  .history-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .compare-status {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.1rem 0.4rem;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.03);
  }
  .compare-status .slot {
    font-family: var(--font-mono, monospace);
    font-size: 0.75rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    border: 1px solid var(--border, #333);
  }
  .compare-status .slot.a {
    color: var(--accent, #5eead4);
    border-color: var(--accent, #5eead4);
  }
  .compare-status .slot.b {
    color: var(--bad, #ef4444);
    border-color: var(--bad, #ef4444);
  }
  .ghost-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .ghost-btn:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--accent, #5eead4);
    color: var(--accent, #5eead4);
  }

  .history-strip {
    list-style: none;
    padding: 0;
    margin: 0.5rem 0 0;
    display: flex;
    gap: 0.4rem;
    overflow-x: auto;
    overflow-y: hidden;
    padding-bottom: 0.4rem;
  }
  .hist-entry {
    position: relative;
  }
  .hist-entry > button:first-child {
    appearance: none;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    border-radius: 6px;
    padding: 0.4rem 1.6rem 0.4rem 0.6rem;
    font: inherit;
    text-align: left;
    min-width: 14rem;
    max-width: 22rem;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    cursor: pointer;
  }
  .hist-entry > button:first-child:hover {
    background: rgba(94, 234, 212, 0.06);
  }
  .hist-entry.active > button:first-child {
    border-color: var(--accent, #5eead4);
    background: rgba(94, 234, 212, 0.12);
  }
  .hist-entry.slot-a > button:first-child {
    border-color: var(--accent, #5eead4);
    box-shadow: inset 3px 0 0 var(--accent, #5eead4);
  }
  .hist-entry.slot-b > button:first-child {
    border-color: var(--bad, #ef4444);
    box-shadow: inset 3px 0 0 var(--bad, #ef4444);
  }
  /* Matches the failed-node outline in PipelineRow so a failed version
     reads as the same kind of error in both panes. Compare slots win
     when set — they layer their inset stripe on top. */
  .hist-entry.failed > button:first-child {
    border-color: rgba(236, 72, 153, 0.55);
  }
  .hist-entry.failed > button:first-child:hover {
    border-color: rgba(236, 72, 153, 0.75);
  }

  .publish-toggle {
    position: absolute;
    top: 0.3rem;
    right: 0.3rem;
    appearance: none;
    background: transparent;
    border: 0;
    color: var(--fg-mute, #777);
    padding: 0.2rem;
    border-radius: 4px;
    cursor: pointer;
    line-height: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  /* Invisible 44x44 hit area for phone tap targets. */
  .publish-toggle::before {
    content: '';
    position: absolute;
    inset: -12px;
  }
  .publish-toggle:hover:not(:disabled) {
    color: var(--accent, #5eead4);
    background: rgba(255, 255, 255, 0.04);
  }
  .publish-toggle.on {
    color: var(--accent, #5eead4);
  }
  .publish-toggle:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .hist-seq {
    font-variant-numeric: tabular-nums;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .hist-label {
    font-size: 0.85rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .hist-time {
    font-size: 0.7rem;
  }

  .compare-toggle {
    position: absolute;
    bottom: 0.3rem;
    right: 0.3rem;
    appearance: none;
    background: transparent;
    border: 0;
    color: var(--fg-mute, #777);
    padding: 0.2rem;
    border-radius: 4px;
    cursor: pointer;
    line-height: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.4rem;
    min-height: 1.4rem;
  }
  .compare-toggle::before {
    content: '';
    position: absolute;
    inset: -12px;
  }
  .compare-toggle:hover:not(:disabled) {
    color: var(--accent, #5eead4);
    background: rgba(255, 255, 255, 0.04);
  }
  .compare-toggle.armed {
    color: var(--accent, #5eead4);
    background: var(--accent-soft, rgba(94, 234, 212, 0.14));
  }
  .compare-toggle:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .compare-slot-letter {
    font-family: var(--font-mono, monospace);
    font-weight: 700;
    font-size: 0.8rem;
    line-height: 1;
  }

  /* ---------- Quality dots ---------- */

  .dot-row {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    margin-top: 0.2rem;
    position: relative;
    cursor: default;
    /* Contain the absolutely-positioned tooltip. */
  }
  .qdot {
    display: inline-block;
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    border: 1px solid var(--border);
    flex-shrink: 0;
  }
  /* Traffic-light fills; 'none' is transparent with muted border. */
  .qdot-good {
    background: color-mix(in oklab, var(--good) 60%, transparent);
    border-color: var(--good);
  }
  .qdot-warn {
    background: color-mix(in oklab, var(--warn) 60%, transparent);
    border-color: var(--warn);
  }
  .qdot-bad {
    background: color-mix(in oklab, var(--bad) 60%, transparent);
    border-color: var(--bad);
  }
  .qdot-none {
    background: transparent;
    border-color: var(--fg-mute, #666);
  }

  .dot-popover {
    position: fixed;
    z-index: 50;
    min-width: 16rem;
    max-width: 22rem;
    padding: 0.55rem 0.7rem;
    background: var(--bg-elev, #1a1d22);
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.35);
    pointer-events: none;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .dp-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }
  .dp-label {
    color: var(--fg-mute, #888);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-family: var(--font-mono, monospace);
  }
  .dp-val {
    font-family: var(--font-mono, monospace);
    font-variant-numeric: tabular-nums;
    font-size: 0.78rem;
    color: var(--fg, #ddd);
    text-align: right;
    white-space: nowrap;
  }
  .dp-def {
    font-size: 0.68rem;
    color: var(--fg-mute, #888);
    line-height: 1.3;
    margin-bottom: 0.1rem;
  }

</style>
