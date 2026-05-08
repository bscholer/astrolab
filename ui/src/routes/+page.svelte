<script lang="ts">
  import { goto } from '$app/navigation';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import {
    api,
    type CalibrationMode,
    type CalibrationStatus,
    type Template,
    type TargetDetail,
    type TargetSummary
  } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo, formatFailPct, failPctClass } from '$lib/format';

  let targets = $state<TargetSummary[] | null>(null);
  let openTargetId = $state<number | null>(null);
  // Detail-per-target, populated in parallel after the targets list lands.
  // Keeping every target's sessions in hand makes expand/collapse feel
  // free — no spinner, no API roundtrip when the user clicks.
  let targetDetails = $state<Map<number, TargetDetail>>(new Map());
  let scanning = $state(false);
  let lastScanAt = $state<string | null>(null);
  let nowTick = $state(Date.now());

  // Capture root used to live in a top-of-page text input. It now lives
  // in Settings; the Library only exposes a Refresh action that scans
  // the saved root.
  const CAPTURE_ROOT_KEY = 'astrolab.capture_root';
  const LAST_SCAN_KEY = 'astrolab.last_scan_at';
  function getCaptureRoot(): string {
    if (typeof localStorage === 'undefined') return '';
    return localStorage.getItem(CAPTURE_ROOT_KEY) ?? '';
  }

  // Runs UI: which session's Run panel is open, plus its in-progress form state.
  let templates = $state<Template[] | null>(null);
  let runOpenSessionId = $state<number | null>(null);
  let runTemplateId = $state<string>('calibrate_register_stack');
  let runCalibrationMode = $state<CalibrationMode>('auto');
  let running = $state(false);

  async function load() {
    try {
      const list = await api.listTargets();
      targets = list;
      // Fan out detail fetches in parallel so the expand animation
      // never has to wait on the network. Settled-not-rejected so one
      // bad row doesn't poison the others.
      const results = await Promise.allSettled(
        list.map((t) => api.getTarget(t.id))
      );
      const next = new Map<number, TargetDetail>();
      results.forEach((r, i) => {
        if (r.status === 'fulfilled') next.set(list[i].id, r.value);
      });
      targetDetails = next;
    } catch (e) {
      toast.error(`Failed to load targets: ${(e as Error).message}`);
    }
  }

  function openTarget_(id: number) {
    openTargetId = openTargetId === id ? null : id;
  }

  async function rescan() {
    const root = getCaptureRoot();
    if (!root.trim()) {
      toast.error('No capture root set — open Settings and add one.');
      return;
    }
    scanning = true;
    try {
      const r = await api.scan(root.trim(), 'dwarf3');
      const stamp = new Date().toISOString();
      localStorage.setItem(LAST_SCAN_KEY, stamp);
      lastScanAt = stamp;
      toast.success(
        `Scanned: +${r.inserted} frames, +${r.masters_inserted} masters, -${r.removed} orphans`
      );
      await load();
    } catch (e) {
      toast.error(`Scan failed: ${(e as Error).message}`);
    } finally {
      scanning = false;
    }
  }

  $effect(() => {
    load();
    // Templates rarely change; one fetch at mount is plenty.
    api.listTemplates()
      .then((t) => (templates = t))
      .catch((e) => toast.error(`listTemplates failed: ${(e as Error).message}`));
    // Hydrate last-scan timestamp from previous sessions.
    if (typeof localStorage !== 'undefined') {
      lastScanAt = localStorage.getItem(LAST_SCAN_KEY);
    }
    // Tick the clock so the muted "scanned 3m ago" line stays fresh
    // without the user reloading.
    const handle = setInterval(() => (nowTick = Date.now()), 30_000);
    return () => clearInterval(handle);
  });

  // Read of nowTick keeps this derived reactive to the 30s ticker.
  const lastScanLabel = $derived.by(() => {
    void nowTick;
    if (!lastScanAt) return null;
    return shortAgo(lastScanAt);
  });

  function toggleRun(sessionId: number) {
    runOpenSessionId = runOpenSessionId === sessionId ? null : sessionId;
  }

  async function submitRun(sessionId: number) {
    running = true;
    try {
      // Projects are the editable wrapper around jobs: each tweak appends
      // a fresh job to the project's history, with cache hits keeping
      // upstream cheap. The standalone job page still exists for debugging.
      const r = await api.createProjectFromSession({
        session_id: sessionId,
        template_id: runTemplateId,
        calibration: { mode: runCalibrationMode },
      });
      goto(`/projects/${r.id}`);
    } catch (e) {
      toast.error(`Couldn't start project: ${(e as Error).message}`);
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
    const base = `${KIND_NAME[c.kind] ?? c.kind} · ${QUALITY_HELP[c.quality] ?? c.quality}`;
    // Only append the matcher's reason when it adds real info beyond the
    // quality label (it's redundant for 'exact', sometimes useful for
    // 'approx', always useful for 'none').
    return c.reason && c.quality !== 'exact' ? `${base}\n${c.reason}` : base;
  }
</script>

<div class="lib-header">
  <h1>Library</h1>
  <button
    type="button"
    class="refresh-btn"
    class:spinning={scanning}
    onclick={rescan}
    disabled={scanning}
    aria-label={scanning ? 'Scanning…' : 'Refresh library'}
    title={scanning ? 'Scanning…' : 'Refresh — re-scan the capture root'}
  >
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12a9 9 0 0 1-15.36 6.36L3 16" />
      <path d="M3 12a9 9 0 0 1 15.36-6.36L21 8" />
      <path d="M21 4v4h-4" />
      <path d="M3 20v-4h4" />
    </svg>
  </button>
  {#if lastScanLabel}
    <span class="muted small last-scan" title={lastScanAt ?? ''}>
      scanned {lastScanLabel}
    </span>
  {:else}
    <span class="muted small last-scan">never scanned</span>
  {/if}
</div>

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

{#if targets === null}
  <p class="muted">Loading targets…</p>
{:else if targets.length === 0}
  <p class="muted">
    No targets yet. Open <a href="/settings" class="link">Settings</a> to set
    a capture root, then come back and hit refresh.
  </p>
{:else}
  <ul class="target-list">
    {#each targets as t, i (t.id)}
      <li class="target" class:open={openTargetId === t.id} style="--stagger: {i}">
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
            <span class="num">{t.frame_count.toLocaleString()} frames</span>
            {#if t.frame_count > 0}
              <span aria-hidden="true">·</span>
              <span class="fail-pct {failPctClass(t.failed_count, t.frame_count)}">
                {formatFailPct(t.failed_count, t.frame_count)}
              </span>
            {/if}
            {#if t.last_session_at}
              <span aria-hidden="true">·</span>
              <span class="num">last {shortDate(t.last_session_at)}</span>
            {/if}
          </div>
        </button>

        {#if openTargetId === t.id}
          {@const detail = targetDetails.get(t.id)}
          <div class="target-detail" transition:slide={{ duration: 220, easing: cubicOut }}>
            {#if detail === undefined}
              <p class="muted">Loading…</p>
            {:else if detail.sessions.length === 0}
              <p class="muted">No sessions yet for this target.</p>
            {:else}
              <ul class="session-list">
                {#each detail.sessions as s (s.id)}
                  <li class="session">
                    <div class="session-head">
                      <span class="session-when">{shortDate(s.started_at)}</span>
                      <span class="session-tags muted">
                        {s.exptime ?? '?'}s · gain {s.gain ?? '?'} · {s.filter ?? '—'}
                      </span>
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
  .lib-header {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    margin: 0.5rem 0 1.25rem;
  }
  .lib-header h1 {
    margin: 0;
    font-size: 1.5rem;
    flex: 0 0 auto;
  }
  .refresh-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    width: 30px;
    height: 30px;
    padding: 0;
    border-radius: 999px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: color 160ms ease, border-color 160ms ease;
  }
  .refresh-btn:hover:not(:disabled) {
    color: var(--accent);
    border-color: var(--accent);
  }
  .refresh-btn:disabled { opacity: 0.6; cursor: progress; }
  .refresh-btn svg { transition: transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1); }
  @keyframes refresh-spin {
    to { transform: rotate(360deg); }
  }
  .refresh-btn.spinning svg { animation: refresh-spin 900ms linear infinite; }
  .last-scan { font-variant-numeric: tabular-nums; }

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
    border-radius: var(--radius-card);
    overflow: hidden;
    box-shadow: var(--shadow);
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 60ms + 80ms);
    transition: border-color 160ms ease, transform 160ms ease;
  }

  .target:hover {
    border-color: var(--border-strong);
    transform: translateY(-1px);
  }
  .target.open {
    border-color: var(--border-strong);
  }

  .target-row {
    width: 100%;
    background: transparent;
    border: none;
    padding: 0.95rem 1.15rem;
    border-radius: 0;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    cursor: pointer;
  }

  .target-row:hover {
    background: rgba(94, 234, 212, 0.04);
  }

  /* Target/project names get the serif treatment per design.md. */
  .target-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.35rem;
    letter-spacing: -0.01em;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .target-common {
    font-weight: 500;
  }

  .target-cat {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    /* Mono digits sit lower than the serif baseline; nudge up. */
    position: relative;
    top: -0.1em;
  }

  .target-meta {
    font-size: 0.85rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: baseline;
  }

  /* Failure percentage — semantic color, mono numerals. */
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
    padding: 0.55rem 0.7rem;
    border-radius: var(--radius);
    background: var(--bg-elev-2);
    border: 1px solid var(--hairline);
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
    /* Allow wrapping for multi-line reasons (e.g. "no dark within +/-3C..."). */
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
    background: linear-gradient(135deg, var(--accent), var(--good));
    color: var(--accent-ink);
    border: 1px solid transparent;
    padding: 0.32rem 0.95rem;
    border-radius: 999px;
    font-weight: 600;
    cursor: pointer;
    font-size: 0.85rem;
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 18px var(--accent-soft);
    transition: filter 160ms ease, transform 160ms ease;
  }
  .run-go:hover:not(:disabled) {
    filter: brightness(1.08);
    transform: translateY(-1px);
  }
  .run-go:disabled {
    opacity: 0.6;
    cursor: progress;
  }

  .link {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent-soft);
    text-underline-offset: 2px;
  }
  .link:hover {
    text-decoration-color: var(--accent);
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
