<script lang="ts">
  import { goto } from '$app/navigation';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import {
    api,
    type CalibrationMode,
    type CalibrationStatus,
    type ReassignCandidatesResponse,
    type SessionSummary,
    type Template,
    type TargetDetail,
    type TargetSummary
  } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo, formatFailPct, failPctClass, formatBytes, formatIntegrationTime } from '$lib/format';

  let targets = $state<TargetSummary[] | null>(null);
  let openTargetId = $state<number | null>(null);
  // Detail-per-target, populated in parallel after the targets list lands.
  // Keeping every target's sessions in hand makes expand/collapse feel
  // free: no spinner, no API roundtrip when the user clicks.
  let targetDetails = $state<Map<number, TargetDetail>>(new Map());
  let scanning = $state(false);
  let lastScanAt = $state<string | null>(null);
  let nowTick = $state(Date.now());

  // Capture root lives in the server-side settings KV; the Library just
  // calls /api/scan against whatever's saved there. last-scan timestamp
  // is per-browser metadata and stays in localStorage.
  const LAST_SCAN_KEY = 'astrolab.last_scan_at';
  let captureRoot = $state<string | null>(null);

  // Runs UI: which session's Run panel is open, plus its in-progress form state.
  let templates = $state<Template[] | null>(null);
  let runOpenSessionId = $state<number | null>(null);
  let runTemplateId = $state<string>('calibrate_register_stack');
  let runCalibrationMode = $state<CalibrationMode>('auto');
  let running = $state(false);

  // Reassign popup state. Keyed by session id; only one open at a time.
  // - reassignSessionId: which session row's popup is currently visible.
  // - reassignCandidates: lazy-loaded {targets, catalog} for the open session.
  // - reassignNewNameDraft: free-text input contents for "totally custom name".
  // - reassignSaving: true while the PATCH is in flight.
  let reassignSessionId = $state<number | null>(null);
  let reassignCandidates = $state<ReassignCandidatesResponse | null>(null);
  let reassignLoading = $state(false);
  let reassignNewNameDraft = $state('');
  let reassignSaving = $state(false);

  // Sort selector for the target list. Persisted per-browser so the
  // user's preferred view sticks across reloads. Defaults to 'name'
  // (alphabetical) which matches what /api/targets used to return.
  type SortMode = 'name' | 'integration' | 'recency';
  const SORT_KEY = 'astrolab.library.sort';
  const SORT_MODES: ReadonlySet<SortMode> = new Set(['name', 'integration', 'recency']);
  let sortMode = $state<SortMode>('name');

  function targetSortKey(t: TargetSummary, mode: SortMode): string | number | null {
    if (mode === 'name') {
      // Prefer the common name (Andromeda) over the catalog id; lowercase
      // so the comparator is case-insensitive.
      const display = t.common_name ?? t.name;
      return display ? display.toLowerCase() : null;
    }
    if (mode === 'integration') {
      // Zero integration shouldn't sort above "unknown"; both go to the
      // bottom on descending sort.
      return t.integration_seconds && t.integration_seconds > 0
        ? t.integration_seconds
        : null;
    }
    return t.last_session_at;
  }

  function cmpSortKeys(
    a: string | number | null,
    b: string | number | null,
    descending: boolean,
  ): number {
    // Nulls always sink, regardless of direction, so an unknown row
    // doesn't pretend to be zero or empty string.
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (a < b) return descending ? 1 : -1;
    if (a > b) return descending ? -1 : 1;
    return 0;
  }

  const sortedTargets = $derived.by(() => {
    if (!targets) return null;
    // Name ascending (A->Z), the quantitative modes descending
    // (most-integrated / most-recent first).
    const descending = sortMode !== 'name';
    return [...targets].sort((a, b) =>
      cmpSortKeys(targetSortKey(a, sortMode), targetSortKey(b, sortMode), descending),
    );
  });

  function setSort(mode: SortMode) {
    sortMode = mode;
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(SORT_KEY, mode);
    }
  }

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
    const next = openTargetId === id ? null : id;
    if (next !== openTargetId) {
      // Clear selection across target switches: bundling sessions from
      // different targets is never something we'd want to do silently.
      selectedSessionIds = new Set();
      runOpenSessionId = null;
      // Close any open reassign popup too; it's anchored to a session
      // row inside the previous target's expansion.
      closeReassign();
    }
    openTargetId = next;
  }

  function closeReassign() {
    reassignSessionId = null;
    reassignCandidates = null;
    reassignNewNameDraft = '';
  }

  async function openReassign(session: SessionSummary) {
    if (reassignSessionId === session.id) {
      closeReassign();
      return;
    }
    reassignSessionId = session.id;
    reassignCandidates = null;
    reassignNewNameDraft = '';
    reassignLoading = true;
    try {
      reassignCandidates = await api.getSessionReassignCandidates(session.id);
    } catch (e) {
      toast.error(`Couldn't load reassign options: ${(e as Error).message}`);
      reassignCandidates = { targets: [], catalog: [] };
    } finally {
      reassignLoading = false;
    }
  }

  async function applyReassign(
    body: { target_id?: number; new_target_name?: string }
  ) {
    if (reassignSessionId == null) return;
    reassignSaving = true;
    try {
      await api.patchSession(reassignSessionId, body);
      // Simplest correct refresh: re-pull the whole library. Re-syncs
      // session counts, drops emptied targets, picks up the new target
      // row if one was created.
      await load();
      closeReassign();
      toast.success('Session reassigned');
    } catch (e) {
      toast.error(`Reassign failed: ${(e as Error).message}`);
    } finally {
      reassignSaving = false;
    }
  }

  function handleReassignKey(e: KeyboardEvent) {
    if (e.key === 'Escape' && reassignSessionId != null) {
      closeReassign();
    }
  }

  // Multi-select state: a set of session ids the user has checked in the
  // currently-open target. Reset whenever the open target changes: bundling
  // sessions across targets is nonsense, and we'd rather have the user
  // re-pick than silently drop selections.
  let selectedSessionIds = $state<Set<number>>(new Set());
  // Track multi-mode submission so we can disable both buttons during it.
  let runningMulti = $state(false);
  // The first checked session 'anchors' the bundle: every other session must
  // share its target/gain/exptime/filter/instrument/binning. The anchor is
  // the lowest-id session in the set so the rule is stable as the user
  // toggles checkboxes around.
  function getAnchor(detail: TargetDetail | undefined): SessionSummary | null {
    if (!detail || selectedSessionIds.size === 0) return null;
    const id = Math.min(...selectedSessionIds);
    return detail.sessions.find((s) => s.id === id) ?? null;
  }
  const COMPAT_KEYS = [
    'instrument',
    'camera',
    'filter',
    'exptime',
    'gain',
    'binning'
  ] as const;
  const COMPAT_LABEL: Record<(typeof COMPAT_KEYS)[number], string> = {
    instrument: 'instrument',
    camera: 'camera',
    filter: 'filter',
    exptime: 'exposure',
    gain: 'gain',
    binning: 'binning'
  };
  /** Returns null when `s` could be added to the selection anchored on
   * `anchor`, or a short reason string when it can't. */
  function incompatibleReason(
    s: SessionSummary,
    anchor: SessionSummary | null
  ): string | null {
    if (!anchor || s.id === anchor.id) return null;
    const mismatches: string[] = [];
    for (const k of COMPAT_KEYS) {
      if (s[k] !== anchor[k]) mismatches.push(COMPAT_LABEL[k]);
    }
    if (mismatches.length === 0) return null;
    return `different ${mismatches.join(', ')}`;
  }

  function toggleSelected(s: SessionSummary) {
    const next = new Set(selectedSessionIds);
    if (next.has(s.id)) next.delete(s.id);
    else next.add(s.id);
    selectedSessionIds = next;
    // Hide any open per-session run panel; multi-mode owns the run
    // affordance once anything is checked.
    if (next.size > 0) runOpenSessionId = null;
  }

  async function submitMultiRun() {
    if (selectedSessionIds.size === 0) return;
    runningMulti = true;
    try {
      const ids = Array.from(selectedSessionIds).sort((a, b) => a - b);
      const r = await api.createProjectFromSessions({
        session_ids: ids,
        template_id: runTemplateId,
        calibration: { mode: runCalibrationMode }
      });
      goto(`/projects/${r.id}`);
    } catch (e) {
      toast.error(`Couldn't start project: ${(e as Error).message}`);
    } finally {
      runningMulti = false;
    }
  }

  async function rescan() {
    if (!captureRoot || !captureRoot.trim()) {
      toast.error('No capture root set: open Settings and add one.');
      return;
    }
    scanning = true;
    try {
      const r = await api.scan(captureRoot.trim(), 'dwarf3');
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
    // Hydrate the captures path + last-scan timestamp. The path is the
    // load-bearing one (drives whether Refresh works); a transient settings
    // fetch failure shouldn't block the targets list.
    api.getSettings()
      .then((s) => (captureRoot = s.capture_root))
      .catch(() => (captureRoot = null));
    if (typeof localStorage !== 'undefined') {
      lastScanAt = localStorage.getItem(LAST_SCAN_KEY);
      const saved = localStorage.getItem(SORT_KEY);
      if (saved && SORT_MODES.has(saved as SortMode)) {
        sortMode = saved as SortMode;
      }
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

  // ---- Session notes (light-touch affordance) ------------------------
  // The Library is already dense, so the notes UI here is intentionally
  // minimal: a "Notes" toggle reveals an inline textarea, autosaves on
  // blur via PATCH /api/sessions/{id}. We keep open state and draft
  // values keyed by session id so flipping between sessions works.
  let notesOpenId = $state<number | null>(null);
  let notesDraftById = $state<Map<number, string>>(new Map());
  let notesSavingId = $state<number | null>(null);

  function toggleNotes(s: SessionSummary) {
    if (notesOpenId === s.id) {
      notesOpenId = null;
      return;
    }
    // Initialize the draft from the server snapshot on first open so
    // late-arriving target detail edits don't surprise the user.
    if (!notesDraftById.has(s.id)) {
      const next = new Map(notesDraftById);
      next.set(s.id, s.description ?? '');
      notesDraftById = next;
    }
    notesOpenId = s.id;
  }

  function setNotesDraft(sessionId: number, value: string) {
    const next = new Map(notesDraftById);
    next.set(sessionId, value);
    notesDraftById = next;
  }

  async function saveNotesOnBlur(s: SessionSummary) {
    const draft = notesDraftById.get(s.id) ?? '';
    const server = s.description ?? '';
    if (draft === server) return;
    notesSavingId = s.id;
    try {
      const resp = await api.patchSession(s.id, { description: draft });
      const updated = resp.session;
      // Re-thread the server's normalized value into the in-memory
      // target detail map so subsequent reads see the saved state.
      const detail = targetDetails.get(openTargetId ?? -1);
      if (detail) {
        const nextSessions = detail.sessions.map((existing) =>
          existing.id === s.id ? updated : existing
        );
        const cloned = new Map(targetDetails);
        cloned.set(detail.id, { ...detail, sessions: nextSessions });
        targetDetails = cloned;
      }
      const draftNext = new Map(notesDraftById);
      draftNext.set(s.id, updated.description ?? '');
      notesDraftById = draftNext;
    } catch (e) {
      toast.error(`Couldn't save notes: ${(e as Error).message}`);
    } finally {
      notesSavingId = null;
    }
  }
</script>

<svelte:window on:keydown={handleReassignKey} />

<div class="lib-header">
  <h1>Library</h1>
  <button
    type="button"
    class="refresh-btn"
    class:spinning={scanning}
    onclick={rescan}
    disabled={scanning}
    aria-label={scanning ? 'Scanning…' : 'Refresh library'}
    title={scanning ? 'Scanning…' : 'Refresh: re-scan the capture root'}
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
  <div class="sort-row" role="radiogroup" aria-label="Sort targets">
    <span class="sort-label">Sort by</span>
    <div class="sort-chips">
      <button
        type="button"
        role="radio"
        aria-checked={sortMode === 'name'}
        class="sort-chip"
        class:active={sortMode === 'name'}
        onclick={() => setSort('name')}
      >Name</button>
      <button
        type="button"
        role="radio"
        aria-checked={sortMode === 'integration'}
        class="sort-chip"
        class:active={sortMode === 'integration'}
        onclick={() => setSort('integration')}
      >Integration</button>
      <button
        type="button"
        role="radio"
        aria-checked={sortMode === 'recency'}
        class="sort-chip"
        class:active={sortMode === 'recency'}
        onclick={() => setSort('recency')}
      >Recency</button>
    </div>
  </div>
  <ul class="target-list">
    {#each sortedTargets ?? [] as t, i (t.id)}
      <li class="target" class:open={openTargetId === t.id} style="--stagger: {i}">
        <div
          class="target-row"
          role="button"
          tabindex="0"
          aria-expanded={openTargetId === t.id}
          onclick={() => openTarget_(t.id)}
          onkeydown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              openTarget_(t.id);
            }
          }}
        >
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
            {#if t.integration_seconds && t.integration_seconds > 0}
              <span aria-hidden="true">·</span>
              <span
                class="num"
                title="Useful integration: (frame_count - failed_count) × exptime"
              >
                {formatIntegrationTime(t.integration_seconds)} integ
              </span>
            {/if}
            {#if t.bytes_on_disk > 0}
              <span aria-hidden="true">·</span>
              <span class="num" title="Total on-disk size of this target's frames">
                {formatBytes(t.bytes_on_disk)}
              </span>
            {/if}
            {#if t.last_session_at}
              <span aria-hidden="true">·</span>
              <span class="num">last {shortDate(t.last_session_at)}</span>
            {/if}
          </div>
        </div>

        {#if openTargetId === t.id}
          {@const detail = targetDetails.get(t.id)}
          <div class="target-detail" transition:slide={{ duration: 220, easing: cubicOut }}>
            {#if detail === undefined}
              <p class="muted">Loading…</p>
            {:else if detail.sessions.length === 0}
              <p class="muted">No sessions yet for this target.</p>
            {:else}
              {@const anchor = getAnchor(detail)}
              {@const multiMode = selectedSessionIds.size > 0}
              {#if multiMode}
                <div class="multi-bar" role="region" aria-label="Selected sessions">
                  <div class="multi-bar-head">
                    <span class="multi-count">
                      {selectedSessionIds.size} session{selectedSessionIds.size === 1 ? '' : 's'} selected
                    </span>
                    <button
                      type="button"
                      class="multi-clear"
                      onclick={() => (selectedSessionIds = new Set())}
                    >
                      clear
                    </button>
                  </div>
                  <div class="multi-bar-controls">
                    <label class="run-row run-row--inline">
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
                    <label class="run-row run-row--inline">
                      <span>Calibration</span>
                      <select bind:value={runCalibrationMode}>
                        <option value="auto">auto (use catalog match)</option>
                        <option value="none">none (skip masters)</option>
                      </select>
                    </label>
                    <button
                      type="button"
                      class="run-go"
                      onclick={submitMultiRun}
                      disabled={runningMulti}
                    >
                      {runningMulti
                        ? 'Submitting…'
                        : selectedSessionIds.size === 1
                          ? 'Run pipeline'
                          : `Run pipeline (${selectedSessionIds.size} sessions)`}
                    </button>
                  </div>
                </div>
              {/if}
              <ul class="session-list">
                {#each detail.sessions as s (s.id)}
                  {@const checked = selectedSessionIds.has(s.id)}
                  {@const reason = incompatibleReason(s, anchor)}
                  {@const blocked = !!reason && !checked}
                  <li class="session" class:dim={blocked}>
                    <label class="session-pick" title={reason ?? ''}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={blocked}
                        onchange={() => toggleSelected(s)}
                      />
                    </label>
                    <div class="session-content">
                      <div class="session-head">
                        <span class="session-when">{shortDate(s.started_at)}</span>
                        <span class="session-tags muted">
                          {s.exptime ?? '?'}s · gain {s.gain ?? '?'} · {s.filter ?? '-'}
                        </span>
                        {#if reason && !checked}
                          <span class="incompat-chip" title={reason}>
                            {reason}
                          </span>
                        {/if}
                        <!-- Per-session reassign pencil. Sits at the end of the
                             head row so the popup unfurls below without
                             pushing the session body around. -->
                        <button
                          type="button"
                          class="reassign-btn"
                          aria-label="Reassign session to another target"
                          aria-expanded={reassignSessionId === s.id}
                          title="Reassign to another target"
                          onclick={(e) => {
                            e.stopPropagation();
                            openReassign(s);
                          }}
                        >
                          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                            <path d="M11.5 1.5l3 3-9 9H2.5v-3l9-9z" />
                            <path d="M10 3l3 3" />
                          </svg>
                        </button>
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
                              class={calBadgeClass(c)}
                              title={calTitle(c)}
                              aria-label={calTitle(c)}
                              data-tip={calTitle(c)}
                            >
                              {calLabel(c.kind)}
                            </button>
                          {/each}
                        </span>
                        {#if !multiMode}
                          <button
                            type="button"
                            class="notes-btn"
                            onclick={() => toggleNotes(s)}
                            aria-expanded={notesOpenId === s.id}
                            title={s.description ?? 'Add notes'}
                          >
                            {s.description ? 'Notes ●' : 'Notes'}
                          </button>
                          <button
                            type="button"
                            class="run-btn"
                            onclick={() => toggleRun(s.id)}
                            aria-expanded={runOpenSessionId === s.id}
                          >
                            {runOpenSessionId === s.id ? 'Cancel' : 'Run…'}
                          </button>
                        {/if}
                      </div>
                      {#if reassignSessionId === s.id}
                        <div
                          class="reassign-popup"
                          transition:slide={{ duration: 160, easing: cubicOut }}
                        >
                          {#if reassignLoading && !reassignCandidates}
                            <p class="muted">Loading suggestions…</p>
                          {:else}
                            {@const cands = reassignCandidates}
                            {#if cands && cands.targets.length > 0}
                              <div class="reassign-section">
                                <div class="reassign-section-label">Move to existing target</div>
                                <ul class="reassign-list">
                                  {#each cands.targets as cand (cand.id)}
                                    <li>
                                      <button
                                        type="button"
                                        class="reassign-option"
                                        disabled={reassignSaving}
                                        onclick={() => applyReassign({ target_id: cand.id })}
                                      >
                                        <span class="reassign-option-main">
                                          {cand.name}{cand.common_name ? ` - ${cand.common_name}` : ''}
                                        </span>
                                        <span class="reassign-option-sep num">
                                          {cand.separation_arcmin.toFixed(1)}'
                                        </span>
                                      </button>
                                    </li>
                                  {/each}
                                </ul>
                              </div>
                            {/if}
                            {#if cands && cands.catalog.length > 0}
                              <div class="reassign-section">
                                <div class="reassign-section-label">Create new target from catalog</div>
                                <ul class="reassign-list">
                                  {#each cands.catalog as cand (cand.canonical)}
                                    <li>
                                      <button
                                        type="button"
                                        class="reassign-option"
                                        disabled={reassignSaving}
                                        onclick={() => applyReassign({ new_target_name: cand.canonical })}
                                      >
                                        <span class="reassign-option-main">
                                          {cand.canonical}{cand.common_name ? ` - ${cand.common_name}` : ''}
                                        </span>
                                        <span class="reassign-option-sep num">
                                          {cand.separation_arcmin.toFixed(1)}'
                                        </span>
                                      </button>
                                    </li>
                                  {/each}
                                </ul>
                              </div>
                            {/if}
                            <div class="reassign-section">
                              <div class="reassign-section-label">Or type a custom name</div>
                              <form
                                class="reassign-custom"
                                onsubmit={(e) => {
                                  e.preventDefault();
                                  const name = reassignNewNameDraft.trim();
                                  if (!name) {
                                    toast.error('Type a target name or pick a suggestion.');
                                    return;
                                  }
                                  applyReassign({ new_target_name: name });
                                }}
                              >
                                <input
                                  type="text"
                                  class="reassign-input"
                                  placeholder="M 31, NGC 7000, My target…"
                                  bind:value={reassignNewNameDraft}
                                />
                                <button
                                  type="submit"
                                  class="reassign-save"
                                  disabled={reassignSaving}
                                >
                                  {reassignSaving ? 'Saving…' : 'Move'}
                                </button>
                                <button
                                  type="button"
                                  class="reassign-cancel"
                                  onclick={closeReassign}
                                >
                                  Cancel
                                </button>
                              </form>
                            </div>
                          {/if}
                        </div>
                      {/if}
                      {#if s.description && notesOpenId !== s.id}
                        <!-- When the textarea is closed, surface the saved
                             note as a small italic snippet so it's
                             discoverable without forcing the textarea
                             open. Suppressed entirely when empty. -->
                        <p class="session-note muted small" title={s.description}>
                          {s.description}
                        </p>
                      {/if}
                      {#if !multiMode && notesOpenId === s.id}
                        <div class="notes-panel" transition:slide={{ duration: 140, easing: cubicOut }}>
                          <label class="notes-label" for={`session-notes-${s.id}`}>
                            Session notes
                            {#if notesSavingId === s.id}
                              <span class="muted small">· saving…</span>
                            {/if}
                          </label>
                          <textarea
                            id={`session-notes-${s.id}`}
                            class="session-notes-area"
                            rows="2"
                            placeholder="Add notes (full moon, dew heater on, etc.). Saves on blur."
                            value={notesDraftById.get(s.id) ?? ''}
                            oninput={(e) =>
                              setNotesDraft(s.id, (e.currentTarget as HTMLTextAreaElement).value)
                            }
                            onblur={() => saveNotesOnBlur(s)}
                          ></textarea>
                        </div>
                      {/if}
                      {#if !multiMode && runOpenSessionId === s.id}
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
                    </div>
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

  .sort-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin: 0 0 0.85rem;
    flex-wrap: wrap;
  }
  .sort-label {
    color: var(--fg-mute);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .sort-chips {
    display: flex;
    gap: 0.4rem;
  }
  .sort-chip {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.75rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.8rem;
    cursor: pointer;
    transition: color 160ms ease, border-color 160ms ease, background 160ms ease;
  }
  .sort-chip:hover {
    color: var(--fg);
    border-color: var(--fg-mute);
  }
  .sort-chip.active {
    color: var(--accent-ink);
    background: var(--accent);
    border-color: transparent;
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
  .target-row:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
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

  /* Failure percentage: semantic color, mono numerals. */
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
    align-items: flex-start;
    gap: 0.55rem;
    transition: opacity 160ms ease;
  }
  .session.dim {
    opacity: 0.45;
  }
  .session-pick {
    /* Stretch a tappable hitbox so the whole left column toggles selection,
       not just the 13px native checkbox. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.35rem;
    margin: -0.35rem 0 -0.35rem -0.2rem;
    cursor: pointer;
  }
  /* Custom checkbox: hide the native input, render a styled box that
     follows the rest of the design language (rounded corners, accent on
     hover, accent fill + check glyph when checked). The native input is
     still present in the DOM so screen readers and keyboard nav (space
     to toggle, focus-visible ring) work normally. */
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
    /* Inline SVG check: recolored to currentColor by clip-path/mask. */
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
  }
  .incompat-chip {
    font-size: 0.72rem;
    color: var(--warn);
    border: 1px solid var(--warn);
    background: color-mix(in oklab, var(--warn) 10%, transparent);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    margin-left: auto;
  }

  /* Per-session reassign pencil button. Muted by default, accent on
     hover/open. Sits at the end of the head row so it doesn't compete
     with the date or tags. */
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
  /* If a row also shows an incompat-chip, the pencil follows it; both
     can claim margin-left:auto. Pull the pencil back to a fixed gap so
     the chip stays anchored right and the pencil tucks next to it. */
  .incompat-chip + .reassign-btn {
    margin-left: 0.4rem;
  }

  /* Reassign popup: slides down under the session head, lives inside the
     session card so it inherits the card chrome. Two sections (existing
     targets, catalog candidates) plus a free-text fallback. */
  .reassign-popup {
    margin-top: 0.45rem;
    padding: 0.55rem 0.7rem;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    font-size: 0.85rem;
  }
  .reassign-section {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .reassign-section-label {
    color: var(--fg-mute);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.7rem;
  }
  .reassign-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: 12rem;
    overflow-y: auto;
  }
  .reassign-option {
    width: 100%;
    appearance: none;
    background: transparent;
    border: 1px solid transparent;
    color: var(--fg);
    text-align: left;
    padding: 0.3rem 0.55rem;
    border-radius: 6px;
    font: inherit;
    font-size: 0.85rem;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    cursor: pointer;
    transition: background-color 140ms ease, border-color 140ms ease;
  }
  .reassign-option:hover:not(:disabled),
  .reassign-option:focus-visible {
    background: rgba(94, 234, 212, 0.06);
    border-color: var(--border);
    outline: none;
  }
  .reassign-option:disabled {
    opacity: 0.5;
    cursor: progress;
  }
  .reassign-option-main {
    flex: 1;
    min-width: 0;
  }
  .reassign-option-sep {
    color: var(--fg-mute);
    font-variant-numeric: tabular-nums;
    font-size: 0.78rem;
  }
  .reassign-custom {
    display: flex;
    gap: 0.4rem;
    align-items: center;
    flex-wrap: wrap;
  }
  .reassign-input {
    flex: 1 1 12rem;
    min-width: 0;
    padding: 0.3rem 0.55rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    font: inherit;
    font-family: var(--font-mono);
  }
  .reassign-save {
    appearance: none;
    background: transparent;
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 0.25rem 0.85rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .reassign-save:hover:not(:disabled) {
    background: var(--accent-soft);
  }
  .reassign-save:disabled {
    opacity: 0.6;
    cursor: progress;
  }
  .reassign-cancel {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.85rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .reassign-cancel:hover {
    color: var(--fg);
    border-color: var(--border-strong);
  }

  /* ---------- Multi-select action bar ---------- */
  .multi-bar {
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    padding: 0.7rem 0.85rem;
    margin-bottom: 0.65rem;
    border-radius: var(--radius);
    background:
      linear-gradient(135deg, rgba(94, 234, 212, 0.06), rgba(122, 162, 255, 0.06)),
      var(--bg-elev);
    border: 1px solid var(--accent);
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.15) inset, 0 0 24px -6px var(--accent-soft);
  }
  .multi-bar-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .multi-bar-controls {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .multi-count {
    font-weight: 600;
    color: var(--accent);
    font-variant-numeric: tabular-nums;
    font-size: 0.95rem;
  }
  .multi-clear {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    transition: background-color 140ms ease, color 140ms ease;
  }
  .multi-clear:hover {
    color: var(--fg);
    background: rgba(255, 255, 255, 0.05);
  }
  /* Inline row inside the bar: label (de-emphasized) + styled select. */
  .run-row--inline {
    font-size: 0.82rem;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
  }
  .run-row--inline > span {
    min-width: 0;
    color: var(--fg-mute);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.7rem;
  }
  .multi-bar select {
    appearance: none;
    -webkit-appearance: none;
    background-color: var(--bg);
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'><path d='M2 4l4 4 4-4' stroke='%239ca3af' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>");
    background-repeat: no-repeat;
    background-position: right 0.5rem center;
    background-size: 0.7rem;
    color: var(--fg);
    font: inherit;
    font-size: 0.85rem;
    border: 1px solid var(--hairline);
    border-radius: 6px;
    padding: 0.32rem 1.6rem 0.32rem 0.6rem;
    cursor: pointer;
    transition: border-color 140ms ease, background-color 140ms ease;
  }
  .multi-bar select:hover {
    border-color: var(--border-strong);
  }
  .multi-bar select:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-soft);
  }
  .multi-bar .run-go {
    margin-left: auto;
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

  /* Notes affordance lives next to the Run pill. Borderless pill so it
     reads as a secondary link, not a primary action. The user
     mentioned the Library was getting cluttered, so this stays quiet
     until they actually want it. A small filled dot signals "this
     session has a saved note". */
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
    /* The closed-state note line: truncate long notes to one row so a
       paragraph doesn't blow the session-row height; the textarea
       reveals the full text on open. */
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
  }
</style>
