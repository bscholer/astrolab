<script lang="ts">
  import { goto } from '$app/navigation';
  import {
    api,
    type CalibrationMode,
    type CalibrationStatus,
    type Template,
    type TargetDetail,
    type TargetSummary
  } from '$lib/api';

  let targets = $state<TargetSummary[] | null>(null);
  let error = $state<string | null>(null);
  let openTargetId = $state<number | null>(null);
  let openTarget = $state<TargetDetail | null>(null);
  let openLoading = $state(false);
  let scanning = $state(false);
  let scanRoot = $state('');
  let scanResult = $state<string | null>(null);

  // Runs UI: which session's Run panel is open, plus its in-progress form state.
  let templates = $state<Template[] | null>(null);
  let runOpenSessionId = $state<number | null>(null);
  let runTemplateId = $state<string>('calibrate_register_stack');
  let runCalibrationMode = $state<CalibrationMode>('auto');
  let runError = $state<string | null>(null);
  let running = $state(false);

  async function load() {
    error = null;
    try {
      targets = await api.listTargets();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function openTarget_(id: number) {
    if (openTargetId === id) {
      openTargetId = null;
      openTarget = null;
      return;
    }
    openTargetId = id;
    openTarget = null;
    openLoading = true;
    try {
      openTarget = await api.getTarget(id);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      openLoading = false;
    }
  }

  async function rescan() {
    if (!scanRoot.trim()) return;
    scanning = true;
    scanResult = null;
    try {
      const r = await api.scan(scanRoot.trim(), 'dwarf3');
      scanResult = `+${r.inserted} frames, +${r.masters_inserted} masters, -${r.removed} orphans`;
      await load();
    } catch (e) {
      scanResult = `error: ${(e as Error).message}`;
    } finally {
      scanning = false;
    }
  }

  $effect(() => {
    load();
    // Templates rarely change; one fetch at mount is plenty.
    api.listTemplates()
      .then((t) => (templates = t))
      .catch((e) => console.error('listTemplates failed', e));
  });

  function toggleRun(sessionId: number) {
    runError = null;
    runOpenSessionId = runOpenSessionId === sessionId ? null : sessionId;
  }

  async function submitRun(sessionId: number) {
    runError = null;
    running = true;
    try {
      const res = await api.submitFromSession({
        session_id: sessionId,
        template_id: runTemplateId,
        calibration: { mode: runCalibrationMode },
      });
      goto(`/jobs/${res.job_id}`);
    } catch (e) {
      runError = (e as Error).message;
    } finally {
      running = false;
    }
  }

  function shortDate(iso: string | null): string {
    if (!iso) return '';
    return iso.slice(0, 10);
  }

  function calBadgeClass(c: CalibrationStatus): string {
    return `cal cal-${c.quality}`;
  }

  function calLabel(kind: string): string {
    return kind[0].toUpperCase();
  }

  const KIND_NAME: Record<string, string> = {
    dark: 'Dark',
    flat: 'Flat',
    bias: 'Bias'
  };

  const QUALITY_HELP: Record<string, string> = {
    exact: 'exact match',
    approx: 'approximate match (within tolerance)',
    none: 'no match found'
  };

  function calTitle(c: CalibrationStatus): string {
    return `${KIND_NAME[c.kind] ?? c.kind} · ${QUALITY_HELP[c.quality] ?? c.quality}`;
  }
</script>

<section class="scan-bar">
  <input
    type="text"
    placeholder="Capture root (e.g. ~/Pictures/Siril)"
    bind:value={scanRoot}
    autocomplete="off"
    spellcheck="false"
  />
  <button onclick={rescan} disabled={scanning || !scanRoot.trim()}>
    {scanning ? 'Scanning…' : 'Scan'}
  </button>
  {#if scanResult}
    <span class="muted scan-result">{scanResult}</span>
  {/if}
</section>

<details class="legend">
  <summary>
    <span class="legend-cluster" aria-hidden="true">
      <span class="cal cal-exact legend-swatch">D</span>
      <span class="cal cal-approx legend-swatch">F</span>
      <span class="cal cal-none legend-swatch">B</span>
    </span>
    <span class="legend-summary-text">Calibration legend</span>
  </summary>
  <div class="legend-body">
    <div class="legend-row">
      <span class="cal cal-exact legend-swatch">D</span>
      <span><strong>D</strong> dark, <strong>F</strong> flat, <strong>B</strong> bias</span>
    </div>
    <div class="legend-row">
      <span class="cal cal-exact legend-swatch">·</span>
      <span><strong>green</strong> exact match</span>
    </div>
    <div class="legend-row">
      <span class="cal cal-approx legend-swatch">·</span>
      <span><strong>amber</strong> approximate (within tolerance, e.g. &plusmn;3&deg;C for darks)</span>
    </div>
    <div class="legend-row">
      <span class="cal cal-none legend-swatch">·</span>
      <span><strong>red</strong> no match found</span>
    </div>
  </div>
</details>

{#if error}
  <p class="error">{error}</p>
{/if}

{#if targets === null}
  <p class="muted">Loading targets…</p>
{:else if targets.length === 0}
  <p class="muted">
    No targets yet. Point the scan field at a Dwarf 3 capture root and hit Scan.
  </p>
{:else}
  <ul class="target-list">
    {#each targets as t (t.id)}
      <li class="target" class:open={openTargetId === t.id}>
        <button class="target-row" onclick={() => openTarget_(t.id)}>
          <div class="target-name">
            {#if t.common_name}
              <span class="target-common">{t.common_name}</span>
              <span class="target-cat muted">{t.name}</span>
            {:else}
              {t.name}
            {/if}
          </div>
          <div class="target-meta muted">
            <span>{t.session_count} session{t.session_count === 1 ? '' : 's'}</span>
            <span aria-hidden="true">·</span>
            <span>
              {(t.frame_count - t.failed_count).toLocaleString()}<!--
              -->{#if t.failed_count > 0}<span class="failed-frac">/{t.frame_count.toLocaleString()}</span>{/if}
              frames
            </span>
            {#if t.last_session_at}
              <span aria-hidden="true">·</span>
              <span>last {shortDate(t.last_session_at)}</span>
            {/if}
          </div>
        </button>

        {#if openTargetId === t.id}
          <div class="target-detail">
            {#if openLoading}
              <p class="muted">Loading…</p>
            {:else if openTarget}
              <ul class="session-list">
                {#each openTarget.sessions as s (s.id)}
                  <li class="session">
                    <div class="session-head">
                      <span class="session-when">{shortDate(s.started_at)}</span>
                      <span class="session-tags muted">
                        {s.exptime ?? '?'}s · gain {s.gain ?? '?'} · {s.filter ?? '—'}
                      </span>
                    </div>
                    <div class="session-body">
                      <span>
                        {(s.frame_count - s.failed_count).toLocaleString()}<!--
                        -->{#if s.failed_count > 0}<span class="failed-frac" title="{s.failed_count} failed sub{s.failed_count === 1 ? '' : 's'}">/{s.frame_count.toLocaleString()}</span>{/if}
                        frame{s.frame_count === 1 ? '' : 's'}
                      </span>
                      <span class="cal-row">
                        {#each s.calibration as c (c.kind)}
                          <button
                            type="button"
                            class={calBadgeClass(c)}
                            title={calTitle(c)}
                            aria-label={calTitle(c)}
                            data-tip={calTitle(c)}
                          >
                            {calLabel(c.kind)}
                          </button>
                        {/each}
                      </span>
                      <button
                        type="button"
                        class="run-btn"
                        onclick={() => toggleRun(s.id)}
                        aria-expanded={runOpenSessionId === s.id}
                      >
                        {runOpenSessionId === s.id ? 'Cancel' : 'Run…'}
                      </button>
                    </div>
                    {#if runOpenSessionId === s.id}
                      <div class="run-panel">
                        <label class="run-row">
                          <span>Template</span>
                          <select bind:value={runTemplateId}>
                            {#if templates === null}
                              <option>loading…</option>
                            {:else}
                              {#each templates as t (t.id)}
                                <option value={t.id}>{t.id}</option>
                              {/each}
                            {/if}
                          </select>
                        </label>
                        <label class="run-row">
                          <span>Calibration</span>
                          <select bind:value={runCalibrationMode}>
                            <option value="auto">auto (use catalog match)</option>
                            <option value="none">none (skip masters)</option>
                          </select>
                        </label>
                        <div class="run-actions">
                          <button
                            type="button"
                            class="run-go"
                            onclick={() => submitRun(s.id)}
                            disabled={running}
                          >
                            {running ? 'Submitting…' : 'Run pipeline'}
                          </button>
                          {#if runError}
                            <span class="run-err">{runError}</span>
                          {/if}
                        </div>
                      </div>
                    {/if}
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .scan-bar {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin: 1rem 0 1.5rem;
    flex-wrap: wrap;
  }

  .scan-bar input {
    flex: 1 1 240px;
    min-width: 0;
    padding: 0.5rem 0.8rem;
    border-radius: 999px;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    font: inherit;
  }

  .scan-bar input:focus {
    outline: none;
    border-color: var(--accent);
  }

  .scan-result {
    font-size: 0.85rem;
    flex-basis: 100%;
  }

  .error {
    color: var(--bad);
    background: rgba(255, 122, 138, 0.1);
    border: 1px solid var(--bad);
    padding: 0.5rem 0.8rem;
    border-radius: var(--radius);
  }

  .target-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .target {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    transition: border-color 120ms ease;
  }

  .target.open {
    border-color: var(--accent);
  }

  .target-row {
    width: 100%;
    background: transparent;
    border: none;
    padding: 0.85rem 1rem;
    border-radius: 0;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    cursor: pointer;
  }

  .target-row:hover {
    background: rgba(122, 162, 255, 0.05);
  }

  .target-name {
    font-weight: 600;
    font-size: 1.05rem;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .target-common {
    font-weight: 600;
  }

  .target-cat {
    font-weight: 500;
    font-size: 0.85rem;
    font-variant-numeric: tabular-nums;
  }

  .target-meta {
    font-size: 0.85rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .bad {
    color: var(--bad);
  }

  .failed-frac {
    color: var(--fg-mute);
    opacity: 0.65;
  }

  .target-detail {
    border-top: 1px solid var(--border);
    padding: 0.5rem 1rem 1rem;
  }

  .session-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .session {
    padding: 0.55rem 0.6rem;
    border-radius: 8px;
    background: rgba(0, 0, 0, 0.2);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .session-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  .session-when {
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

  .cal-row {
    display: inline-flex;
    gap: 0.3rem;
    margin-left: auto;
  }

  .cal {
    /* Buttons inherit a lot of UA styling; reset what we don't want. */
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

  /* Custom tooltip via data-tip; works on hover (desktop) and focus (mobile tap). */
  .cal[data-tip]::after {
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    right: 0;
    white-space: nowrap;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    padding: 0.3rem 0.55rem;
    border-radius: 6px;
    font-size: 0.7rem;
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
    background: rgba(94, 211, 168, 0.18);
    border-color: var(--good);
    color: var(--good);
  }

  .cal-approx {
    background: rgba(240, 179, 94, 0.18);
    border-color: var(--warn);
    color: var(--warn);
  }

  .cal-none {
    background: rgba(255, 122, 138, 0.12);
    border-color: var(--bad);
    color: var(--bad);
  }

  .legend {
    margin: 0 0 1rem;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--bg-elev);
    overflow: hidden;
  }

  .legend > summary {
    list-style: none;
    cursor: pointer;
    padding: 0.55rem 0.8rem;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    user-select: none;
  }

  .legend > summary::-webkit-details-marker {
    display: none;
  }

  .legend-cluster {
    display: inline-flex;
    gap: 0.25rem;
  }

  .legend-summary-text {
    font-size: 0.85rem;
    color: var(--fg-mute);
  }

  .legend[open] > summary {
    border-bottom: 1px solid var(--border);
  }

  .legend-body {
    padding: 0.6rem 0.8rem 0.8rem;
    font-size: 0.85rem;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .legend-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .legend-swatch {
    /* Inline swatches inherit cal sizing; just disable cursor/tooltip. */
    cursor: default;
    width: 1.4rem;
    height: 1.4rem;
    font-size: 0.7rem;
  }

  .legend-swatch::after {
    display: none !important;
  }

  .run-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--accent, #7aa2ff);
    padding: 0.2rem 0.7rem;
    border-radius: 999px;
    font-size: 0.75rem;
    cursor: pointer;
    margin-left: 0.5rem;
  }
  .run-btn:hover {
    background: rgba(122, 162, 255, 0.1);
  }
  .run-btn[aria-expanded='true'] {
    background: rgba(122, 162, 255, 0.18);
  }
  .run-panel {
    margin-top: 0.4rem;
    padding: 0.5rem 0.6rem;
    background: rgba(122, 162, 255, 0.06);
    border: 1px solid var(--border);
    border-radius: 8px;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    font-size: 0.85rem;
  }
  .run-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .run-row > span {
    min-width: 5.5rem;
    color: var(--fg-mute);
  }
  .run-row select {
    flex: 1;
    padding: 0.25rem 0.4rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 4px;
    font: inherit;
  }
  .run-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .run-go {
    background: var(--accent, #7aa2ff);
    color: #0a0c10;
    border: none;
    padding: 0.3rem 0.9rem;
    border-radius: 999px;
    font-weight: 600;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .run-go:disabled {
    opacity: 0.6;
    cursor: progress;
  }
  .run-err {
    color: var(--bad, #f88);
    font-size: 0.8rem;
  }

  @media (max-width: 600px) {
    .target-row {
      padding: 0.7rem 0.8rem;
    }
    .scan-bar {
      gap: 0.4rem;
    }
  }
</style>
