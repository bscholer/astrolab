<!--
  SessionRow: one capture session, rendered as a card.

  Mounted by the Library page (with all the interactive affordances:
  multi-select checkbox, reassign pencil, run pill) and by the project
  detail page (read-mostly: just notes + visible metadata). Toggles
  are exposed as named slots / prop callbacks so neither caller has to
  fork the markup.

  Keep this component visual + presentational: API calls live in the
  parent so they can update parent-owned state (target detail map on
  the Library, source-session list on the project page) without
  threading shared state into a child.
-->
<script lang="ts">
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import type { Snippet } from 'svelte';
  import type { CalibrationStatus, SessionSummary } from '$lib/api';
  import {
    formatBytes,
    formatFailPct,
    failPctClass,
    formatIntegrationTime,
  } from '$lib/format';

  type Props = {
    session: SessionSummary;
    /** Short date (yyyy-mm-dd) for the head row. Parent computes so
     *  callers can share their existing shortDate helper. */
    shortDate: string;
    /** Notes panel state: open/draft/saving lives in the parent so
     *  multiple rows can share a single open-id and the parent owns
     *  the autosave round-trip. */
    notesOpen: boolean;
    notesDraft: string;
    notesSaving: boolean;
    onToggleNotes: () => void;
    onNotesInput: (value: string) => void;
    onNotesBlur: () => void;
    /** Library-only affordances. Each parent supplies (or omits) the
     *  ones it needs. The project page leaves these undefined and
     *  the corresponding UI vanishes (no Run button, no reassign
     *  pencil, no checkbox). */
    selectable?: {
      checked: boolean;
      disabled: boolean;
      reason: string | null;
      onToggle: () => void;
    };
    runAffordance?: {
      open: boolean;
      onToggle: () => void;
      hidden: boolean;
    };
    reassign?: {
      open: boolean;
      onClick: (e: MouseEvent) => void;
    };
    /** Whether the close-state note snippet shows when notes are
     *  collapsed and a saved note exists. Both call sites want this on. */
    showCollapsedNote?: boolean;
    /** Suppress the Notes pill (and the inline textarea). Library uses
     *  this when multi-select is active so the row collapses to its
     *  essential metadata; project page leaves it false. */
    notesHidden?: boolean;
    /** Calibration tooltip + label helpers come from the parent so we
     *  don't duplicate the KIND_NAME / QUALITY_HELP maps. The class
     *  is derived inline from c.quality so SessionRow's scoped CSS
     *  hash sticks to the rendered button. */
    calLabel: (kind: string) => string;
    calTitle: (c: CalibrationStatus) => string;
    /** Extras stacked inside session-content between the body and the
     *  collapsed-note line. The Library uses this for the reassign
     *  popup, which the original layout slots right under the body. */
    extrasAfterBody?: Snippet;
    /** Extras stacked at the very bottom of session-content, after the
     *  notes panel. The Library uses this for the per-row Run panel. */
    extrasAfterNotes?: Snippet;
  };

  let {
    session: s,
    shortDate,
    notesOpen,
    notesDraft,
    notesSaving,
    onToggleNotes,
    onNotesInput,
    onNotesBlur,
    selectable,
    runAffordance,
    reassign,
    showCollapsedNote = true,
    notesHidden = false,
    calLabel,
    calTitle,
    extrasAfterBody,
    extrasAfterNotes,
  }: Props = $props();

  /** Classify a filter string into one of the three styled badge variants
   *  the design spec calls out. Anything unrecognized falls through to
   *  neutral (same treatment as VIS). */
  function filterBadgeClass(f: string | null | undefined): string {
    if (!f) return '';
    const k = f.toLowerCase();
    if (k.includes('duo') || k.includes('narrow') || k === 'ha' || k.includes('oiii') || k.includes('sii'))
      return 'filter-badge filter-duoband';
    if (k.includes('astro') || k.includes('broad') || k.includes('rgb') || k.includes('lum'))
      return 'filter-badge filter-astro';
    return 'filter-badge filter-neutral';
  }
</script>

<li class="session" class:dim={selectable?.disabled && !selectable.checked}>
  {#if selectable}
    <label class="session-pick" title={selectable.reason ?? ''}>
      <input
        type="checkbox"
        checked={selectable.checked}
        disabled={selectable.disabled && !selectable.checked}
        onchange={selectable.onToggle}
      />
    </label>
  {/if}
  <div class="session-content">
    <div class="session-head">
      <span class="session-when">{shortDate}</span>
      <span class="session-tags muted">
        {s.exptime ?? '?'}s · gain {s.gain ?? '?'}
      </span>
      {#if s.filter}
        <span class={filterBadgeClass(s.filter)}>{s.filter}</span>
      {/if}
      {#if selectable?.reason && !selectable.checked}
        <span class="incompat-chip" title={selectable.reason}>
          {selectable.reason}
        </span>
      {/if}
      {#if reassign}
        <button
          type="button"
          class="reassign-btn"
          aria-label="Reassign session to another target"
          aria-expanded={reassign.open}
          title="Reassign to another target"
          onclick={reassign.onClick}
        >
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M11.5 1.5l3 3-9 9H2.5v-3l9-9z" />
            <path d="M10 3l3 3" />
          </svg>
        </button>
      {/if}
    </div>
    <div class="session-body">
      <span class="num">
        {s.frame_count.toLocaleString()} frame{s.frame_count === 1 ? '' : 's'}
      </span>
      {#if s.frame_count > 0}
        <span aria-hidden="true" class="muted">·</span>
        <span
          class="fail-pct {failPctClass(s.failed_count, s.frame_count)}"
          title="{s.failed_count} of {s.frame_count} failed"
        >
          {formatFailPct(s.failed_count, s.frame_count)}
        </span>
      {/if}
      {#if s.integration_seconds && s.integration_seconds > 0}
        <span aria-hidden="true" class="muted">·</span>
        <span
          class="num muted"
          title="Useful integration: ({s.frame_count} - {s.failed_count}) × {s.exptime ?? '?'}s"
        >
          {formatIntegrationTime(s.integration_seconds)} integ
        </span>
      {/if}
      {#if s.bytes_on_disk > 0}
        <span aria-hidden="true" class="muted">·</span>
        <span class="num muted" title="On-disk size of this session's frames">
          {formatBytes(s.bytes_on_disk)}
        </span>
      {/if}
      <span class="cal-row">
        {#each s.calibration as c (c.kind)}
          <button
            type="button"
            class="cal cal-{c.quality}"
            title={calTitle(c)}
            aria-label={calTitle(c)}
            data-tip={calTitle(c)}
          >
            {calLabel(c.kind)}
          </button>
        {/each}
      </span>
      {#if !notesHidden}
        <button
          type="button"
          class="notes-btn"
          onclick={onToggleNotes}
          aria-expanded={notesOpen}
          title={s.description ?? 'Add notes'}
        >
          {s.description ? 'Notes ●' : 'Notes'}
        </button>
      {/if}
      {#if runAffordance && !runAffordance.hidden}
        <button
          type="button"
          class="run-btn"
          onclick={runAffordance.onToggle}
          aria-expanded={runAffordance.open}
        >
          {runAffordance.open ? 'Cancel' : 'Run…'}
        </button>
      {/if}
    </div>
    {#if extrasAfterBody}
      {@render extrasAfterBody()}
    {/if}
    {#if showCollapsedNote && s.description && !notesOpen}
      <p class="session-note muted small" title={s.description}>
        {s.description}
      </p>
    {/if}
    {#if notesOpen && !notesHidden}
      <div class="notes-panel" transition:slide={{ duration: 140, easing: cubicOut }}>
        <label class="notes-label" for={`session-notes-${s.id}`}>
          Session notes
          {#if notesSaving}
            <span class="muted small">· saving…</span>
          {/if}
        </label>
        <textarea
          id={`session-notes-${s.id}`}
          class="session-notes-area"
          rows="2"
          placeholder="Add notes (full moon, dew heater on, etc.). Unfocus to save."
          value={notesDraft}
          oninput={(e) => onNotesInput((e.currentTarget as HTMLTextAreaElement).value)}
          onblur={onNotesBlur}
        ></textarea>
      </div>
    {/if}
    {#if extrasAfterNotes}
      {@render extrasAfterNotes()}
    {/if}
  </div>
</li>

<style>
  /* The styles below are copied from the Library page's session row so
     SessionRow.svelte is a drop-in visual replacement. Anything callers
     stack on top (reassign popup, run panel) still lives in the parent
     and inherits .session-content as its container. */
  .session {
    padding: 0.55rem 0.7rem;
    border-radius: var(--radius);
    background: var(--bg-elev-2);
    border: 1px solid var(--hairline);
    display: flex;
    align-items: flex-start;
    gap: 0.55rem;
    transition: opacity 160ms ease;
    list-style: none;
  }
  .session.dim {
    opacity: 0.45;
  }
  .session-pick {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.35rem;
    margin: -0.35rem 0 -0.35rem -0.2rem;
    cursor: pointer;
  }
  .session-pick input[type='checkbox'] {
    appearance: none;
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    margin: 0;
    border: 1.5px solid var(--border-strong);
    border-radius: 4px;
    background: var(--bg);
    cursor: inherit;
    display: inline-grid;
    place-content: center;
    transition: background-color 140ms ease, border-color 140ms ease;
  }
  .session-pick input[type='checkbox']::before {
    content: '';
    width: 10px;
    height: 10px;
    transform: scale(0);
    background-color: var(--accent-ink);
    clip-path: polygon(14% 44%, 0 60%, 40% 100%, 100% 20%, 80% 6%, 38% 70%);
    transition: transform 140ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .session-pick input[type='checkbox']:checked {
    background: var(--accent);
    border-color: var(--accent);
  }
  .session-pick input[type='checkbox']:checked::before {
    transform: scale(1);
  }
  .session-pick input[type='checkbox']:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .session-pick input[type='checkbox']:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .session-pick input[disabled] {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .session-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
    container-type: inline-size;
  }
  .session-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .session-when {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }
  .session-tags {
    font-size: 0.8rem;
  }
  .session-body {
    font-size: 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  /* Filter badge: small uppercase pill, color-coded by broad family.
     Astro -> cool blue, Duo-Band -> green tint, anything else -> neutral
     grey. Renders only when the parent passes a non-empty filter
     string (we short-circuit above). */
  .filter-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1.2;
    font-weight: 600;
  }
  .filter-badge.filter-astro {
    background: rgba(95, 175, 250, 0.18);
    color: #9ec8ff;
  }
  .filter-badge.filter-duoband {
    background: rgba(94, 234, 212, 0.18);
    color: #7fd8c4;
  }
  .filter-badge.filter-neutral {
    background: var(--bg-soft, rgba(255, 255, 255, 0.06));
    color: var(--fg-mute);
  }

  /* incompat-chip / reassign-btn / cal-row are copied verbatim so the
     library row still looks identical after extraction. */
  .incompat-chip {
    font-size: 0.72rem;
    color: var(--warn);
    border: 1px solid var(--warn);
    background: color-mix(in oklab, var(--warn) 10%, transparent);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    margin-left: auto;
  }
  .reassign-btn {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    padding: 0.15rem 0.35rem;
    margin-left: auto;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    transition: color 140ms ease, background-color 140ms ease;
  }
  .reassign-btn:hover,
  .reassign-btn:focus-visible {
    color: var(--fg);
    background: rgba(255, 255, 255, 0.03);
  }
  .reassign-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .reassign-btn[aria-expanded='true'] {
    color: var(--accent);
  }
  .incompat-chip + .reassign-btn {
    margin-left: 0.4rem;
  }

  .fail-pct {
    color: var(--warn);
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .fail-pct.fail-zero {
    color: var(--good);
    opacity: 0.85;
  }
  .fail-pct.fail-high {
    color: var(--bad);
    font-weight: 500;
  }

  .cal-row {
    display: inline-flex;
    gap: 0.3rem;
    margin-left: auto;
  }

  /* Calibration disc styles are duplicated here because Svelte scopes
     CSS per-component. The library page also defines `.cal` for its
     legend (those buttons render inside the page so they get the
     page's scope hash); these apply to buttons rendered inside
     SessionRow. Same visual, different scope. */
  .cal {
    appearance: none;
    -webkit-appearance: none;
    background: transparent;
    padding: 0;
    margin: 0;
    font: inherit;
    line-height: 1;
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.6rem;
    height: 1.6rem;
    border-radius: 50%;
    font-size: 0.75rem;
    font-weight: 600;
    border: 1px solid var(--border);
    cursor: help;
  }
  .cal:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }
  .cal[data-tip]::after {
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    right: 0;
    white-space: pre-line;
    max-width: min(280px, 75vw);
    width: max-content;
    text-align: left;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    padding: 0.35rem 0.55rem;
    border-radius: 6px;
    font-size: 0.75rem;
    line-height: 1.35;
    font-weight: 500;
    pointer-events: none;
    opacity: 0;
    transform: translateY(2px);
    transition: opacity 120ms ease, transform 120ms ease;
    z-index: 5;
  }
  .cal[data-tip]:hover::after,
  .cal[data-tip]:focus-visible::after,
  .cal[data-tip]:focus::after {
    opacity: 1;
    transform: translateY(0);
  }
  .cal-exact {
    background: color-mix(in oklab, var(--good) 14%, transparent);
    border-color: var(--good);
    color: var(--good);
  }
  .cal-approx {
    background: color-mix(in oklab, var(--warn) 14%, transparent);
    border-color: var(--warn);
    color: var(--warn);
  }
  .cal-none {
    background: color-mix(in oklab, var(--bad) 12%, transparent);
    border-color: var(--bad);
    color: var(--bad);
  }

  .run-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--accent);
    padding: 0.2rem 0.7rem;
    border-radius: 999px;
    font-size: 0.75rem;
    cursor: pointer;
    margin-left: 0.5rem;
  }
  .run-btn:hover {
    background: var(--accent-soft);
    border-color: var(--accent);
  }
  .run-btn[aria-expanded='true'] {
    background: var(--accent-soft);
    border-color: var(--accent);
  }

  .notes-btn {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    padding: 0.2rem 0.5rem;
    border-radius: 6px;
    font-size: 0.75rem;
    cursor: pointer;
    margin-left: 0.25rem;
  }
  .notes-btn:hover {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg);
  }
  .notes-btn[aria-expanded='true'] {
    background: rgba(255, 255, 255, 0.06);
    color: var(--fg);
  }
  .session-note {
    margin: 0.2rem 0 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-style: italic;
    max-width: 100%;
  }
  .notes-panel {
    margin-top: 0.4rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .notes-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute);
  }
  .session-notes-area {
    width: 100%;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.3rem 0.5rem;
    font-size: 0.85rem;
    line-height: 1.35;
    resize: vertical;
    font-family: inherit;
  }
  .session-notes-area:focus {
    outline: none;
    border-color: var(--accent);
  }

  /* At 540px+ container width there's enough room to show the notes
     panel beside the row metadata rather than below it. The grid
     approach works with dynamic snippet children (reassign popup, run
     panel): unplaced items auto-flow into column 1, the notes panel
     is pinned to column 2 and spans all rows so it floats beside the
     metadata. min-content for the second track means the column
     collapses to 0 when the panel is absent (notes closed), avoiding
     a dead whitespace gap. */
  @container (min-width: 540px) {
    .session-content {
      display: grid;
      grid-template-columns: 1fr min-content;
      column-gap: 0.75rem;
      align-items: start;
    }
    .notes-panel {
      grid-column: 2;
      grid-row: 1 / -1;
      margin-top: 0;
      align-self: start;
      width: 220px;
    }
  }
</style>
