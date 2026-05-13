<script lang="ts">
  import { goto } from '$app/navigation';
  import { slide } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import { cubicOut } from 'svelte/easing';
  import {
    api,
    type CalibrationMode,
    type CalibrationStatus,
    type ReassignCandidatesResponse,
    type ScanStatusResponse,
    type SessionSummary,
    type Template,
    type TargetDetail,
    type TargetSummary
  } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo, formatFailPct, failPctClass, formatBytes, formatIntegrationTime } from '$lib/format';
  import SessionRow from '$lib/SessionRow.svelte';

  let targets = $state<TargetSummary[] | null>(null);
  // Gates the cascading rise-in animation. The stagger looks great on
  // first paint but is noisy when the list re-renders for any other
  // reason (sort change, post-scan refresh), so we play it once.
  let cascadeOnLoad = $state(true);
  let openTargetId = $state<number | null>(null);
  // Detail-per-target, populated in parallel after the targets list lands.
  // Keeping every target's sessions in hand makes expand/collapse feel
  // free: no spinner, no API roundtrip when the user clicks.
  let targetDetails = $state<Map<number, TargetDetail>>(new Map());
  let scanning = $state(false);
  let scanStatus = $state<ScanStatusResponse | null>(null);
  let lastScanAt = $state<string | null>(null);
  let nowTick = $state(Date.now());

  // Polling handles: status poll (1.5s) and library reload (4s during scan).
  let _statusPollHandle: ReturnType<typeof setInterval> | null = null;
  let _libraryPollHandle: ReturnType<typeof setInterval> | null = null;

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
      // Schedule the cascade flag to flip off once the staggered intro
      // has had time to play. Length-aware so a big library still gets
      // its full animation, then later refreshes render without it.
      if (cascadeOnLoad && targets === null) {
        const duration = 360 + list.length * 60 + 80 + 50;
        setTimeout(() => {
          cascadeOnLoad = false;
        }, duration);
      }
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

  function _stopPolling() {
    if (_statusPollHandle !== null) {
      clearInterval(_statusPollHandle);
      _statusPollHandle = null;
    }
    if (_libraryPollHandle !== null) {
      clearInterval(_libraryPollHandle);
      _libraryPollHandle = null;
    }
  }

  async function _pollStatus() {
    try {
      const s = await api.scanStatus();
      scanStatus = s;
      if (!s.running) {
        // Scan finished (or was never running).
        _stopPolling();
        scanning = false;
        await load();
        if (s.error) {
          toast.error(`Scan failed: ${s.error}`);
        } else if (s.last_stats) {
          const stamp = new Date().toISOString();
          localStorage.setItem(LAST_SCAN_KEY, stamp);
          lastScanAt = stamp;
          const st = s.last_stats;
          toast.success(
            `Scanned: +${st.inserted} frames, +${st.masters_inserted} masters, -${st.removed} orphans`
          );
          // Only Dwarf 3 is end-to-end validated through processing today.
          // Warn (once) when any frames from another scope landed in the
          // library so the user knows calibration/processing may misbehave.
          const breakdown = st.scope_breakdown ?? {};
          const otherScopes = Object.entries(breakdown)
            .filter(([scope, n]) => scope !== 'dwarf3' && n > 0)
            .map(([scope, n]) => `${scope} (${n})`);
          if (otherScopes.length > 0) {
            toast.info(
              `Detected non-Dwarf-3 captures: ${otherScopes.join(', ')}. ` +
                `Metadata is best-effort; calibration and processing are validated only for Dwarf 3.`,
              15_000
            );
          }
        }
      }
    } catch {
      // Network hiccup — keep polling.
    }
  }

  function _startPolling() {
    scanning = true;
    _stopPolling(); // Clear any pre-existing handles.
    _statusPollHandle = setInterval(_pollStatus, 1500);
    // Reload library every 4s so new sessions appear incrementally.
    _libraryPollHandle = setInterval(load, 4000);
  }

  async function rescan() {
    if (!captureRoot || !captureRoot.trim()) {
      toast.error('No capture root set: open Settings and add one.');
      return;
    }
    try {
      await api.scan(captureRoot.trim());
    } catch (e: unknown) {
      // 409 means already running — enter polling mode anyway.
      const status = (e as { status?: number }).status;
      if (status !== 409) {
        toast.error(`Scan failed: ${(e as Error).message}`);
        return;
      }
    }
    _startPolling();
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
    // If the bootstrap scan is already running when the page loads,
    // enter polling mode without firing a new scan.
    api.scanStatus()
      .then((s) => {
        scanStatus = s;
        if (s.running) _startPolling();
      })
      .catch(() => {});
    // Tick the clock so the muted "scanned 3m ago" line stays fresh
    // without the user reloading.
    const handle = setInterval(() => (nowTick = Date.now()), 30_000);
    return () => {
      clearInterval(handle);
      _stopPolling();
    };
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

  /** Keep only the last 2 path segments for display (e.g. foo/bar.fits). */
  function shortPath(p: string | null): string {
    if (!p) return '';
    const parts = p.replace(/\\/g, '/').split('/').filter(Boolean);
    return parts.slice(-2).join('/');
  }

  function shortDate(iso: string | null): string {
    if (!iso) return '';
    return iso.slice(0, 10);
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
    none: 'no match found',
    not_needed: 'not needed (scope subtracts on device)'
  };

  function calTitle(c: CalibrationStatus): string {
    const base = `${KIND_NAME[c.kind] ?? c.kind} · ${QUALITY_HELP[c.quality] ?? c.quality}`;
    // Only append the matcher's reason when it adds real info beyond the
    // quality label (it's redundant for 'exact', sometimes useful for
    // 'approx', always useful for 'none' and 'not_needed').
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

{#if scanning && scanStatus}
  <div class="scan-banner" role="status" aria-live="polite">
    <span class="scan-spinner" aria-hidden="true"></span>
    <span class="scan-text">
      Scanning&hellip; {scanStatus.discovered.toLocaleString()} files
      {#if scanStatus.current_path}
        &middot; <span class="scan-path" title={scanStatus.current_path}>{shortPath(scanStatus.current_path)}</span>
      {/if}
    </span>
  </div>
{/if}

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
      <li
        class="target"
        class:open={openTargetId === t.id}
        class:cascade={cascadeOnLoad}
        style={cascadeOnLoad ? `--stagger: ${i}` : ''}
        animate:flip={{ duration: 350, easing: cubicOut }}
      >
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
                  <SessionRow
                    session={s}
                    shortDate={shortDate(s.started_at)}
                    notesOpen={notesOpenId === s.id}
                    notesDraft={notesDraftById.get(s.id) ?? ''}
                    notesSaving={notesSavingId === s.id}
                    onToggleNotes={() => toggleNotes(s)}
                    onNotesInput={(v) => setNotesDraft(s.id, v)}
                    onNotesBlur={() => saveNotesOnBlur(s)}
                    selectable={{
                      checked,
                      disabled: blocked,
                      reason,
                      onToggle: () => toggleSelected(s),
                    }}
                    runAffordance={{
                      open: runOpenSessionId === s.id,
                      onToggle: () => toggleRun(s.id),
                      hidden: multiMode,
                    }}
                    reassign={{
                      open: reassignSessionId === s.id,
                      onClick: (e) => {
                        e.stopPropagation();
                        openReassign(s);
                      },
                    }}
                    notesHidden={multiMode}
                    {calLabel}
                    {calTitle}
                  >
                    {#snippet extrasAfterBody()}
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
                    {/snippet}
                    {#snippet extrasAfterNotes()}
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
                    {/snippet}
                  </SessionRow>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </li>
    {/each}
  </ul>
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

  .scan-banner {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.45rem 0.8rem;
    margin: 0 0 0.85rem;
    background: color-mix(in oklab, var(--accent) 8%, var(--bg-elev));
    border: 1px solid color-mix(in oklab, var(--accent) 30%, var(--border));
    border-radius: var(--radius);
    font-size: 0.85rem;
    color: var(--fg);
  }
  .scan-spinner {
    display: inline-block;
    width: 0.75rem;
    height: 0.75rem;
    border: 2px solid color-mix(in oklab, var(--accent) 30%, transparent);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: refresh-spin 700ms linear infinite;
    flex-shrink: 0;
  }
  .scan-text {
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .scan-path {
    color: var(--fg-mute);
    font-family: var(--font-mono);
    font-size: 0.78rem;
  }

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
    transition: border-color 160ms ease, transform 160ms ease;
  }
  .target.cascade {
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 60ms + 80ms);
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

  /* .session, .session-pick, .session-content, .incompat-chip and the
     reassign pencil moved into SessionRow.svelte. The reassign-popup
     below still lives in the parent (rendered via the extrasAfterBody
     snippet) so its styles stay here. */

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

  /* .session-head, .session-when, .session-tags, .session-body, .cal-row,
     and the .cal* family moved into SessionRow.svelte along with the rest
     of the per-row visuals. The legend below still renders its own swatches
     in the parent markup, so it keeps a local copy of the .cal styles
     (scoped here so the swatches inherit them). */
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
    margin: 1rem 0 0;
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

  /* .run-btn, .notes-btn, .session-note, .notes-panel, .notes-label
     and .session-notes-area moved into SessionRow.svelte. The run
     panel below is still rendered here (via extrasAfterNotes), so its
     styles stay. */
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
