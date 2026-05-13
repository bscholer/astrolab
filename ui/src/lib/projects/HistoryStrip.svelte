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
  import type { Project } from '$lib/api';

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
</style>
