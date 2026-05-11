<script lang="ts">
  import { goto } from '$app/navigation';
  import { slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import {
    api,
    type CalibrationMode,
    type CalibrationStatus,
    type ResolvedAs,
    type SessionSummary,
    type Template,
    type TargetDetail,
    type TargetSummary
  } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo, formatFailPct, failPctClass, formatBytes, formatIntegrationTime } from '$lib/format';

  let targets = $state<TargetSummary[] | null>(null);
  let openTargetId = $state<number | null>(null);
  // Which group headers are expanded. Keyed by canonical id so the set
  // survives a re-fetch (group identity is stable across loads, group
  // *contents* may shift as users pin overrides).
  let expandedGroups = $state<Set<string>>(new Set());
  // Which target row currently has its override editor open. At most
  // one open at a time keeps the page legible; clicking the pencil on
  // another row hands the editor over.
  let editingOverrideTargetId = $state<number | null>(null);
  // Detail-per-target, populated in parallel after the targets list lands.
  // Keeping every target's sessions in hand makes expand/collapse feel
  // free — no spinner, no API roundtrip when the user clicks.
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

  // Resolve-as override editor state, keyed by target id.
  // - nearbyById: cached suggestion list per target (fetched lazily on panel open).
  // - overrideChoiceById: which option the user selected in the <select>.
  //   Sentinel values: 'AUTO' (clears the override) and 'OTHER' (swaps to a
  //   free-text input).
  // - otherDraftById: the typed value when 'OTHER' is active.
  // - savingOverrideId / loadingNearbyId: which row is mid-async.
  let nearbyById = $state<Map<number, ResolvedAs[]>>(new Map());
  let overrideChoiceById = $state<Map<number, string>>(new Map());
  let otherDraftById = $state<Map<number, string>>(new Map());
  let loadingNearbyId = $state<number | null>(null);
  let savingOverrideId = $state<number | null>(null);

  function sepLabel(resolved: ResolvedAs | null): string {
    if (!resolved || resolved.separation_arcmin == null) return '';
    return `matched ${resolved.separation_arcmin.toFixed(1)}'`;
  }

  // Display-layer bucket: zero or more targets that resolve to the same
  // canonical id. The orchestrator intentionally chose a display-only
  // merge here, with underlying DB rows staying distinct, so anything
  // that walks targets (Projects, scan diffs) sees the same rows it
  // always did.
  interface TargetBucket {
    // null = unresolved bucket. Non-null buckets carry the catalog id.
    key: string | null;
    name: string | null;
    members: TargetSummary[];
    total_frames: number;
    total_sessions: number;
    total_integration: number;
    total_bytes: number;
    last_session_at: string | null;
  }

  function bucketTargets(list: TargetSummary[]): {
    groups: TargetBucket[];
    singles: TargetBucket[];
    unresolved: TargetBucket | null;
  } {
    const byKey = new Map<string, TargetBucket>();
    const unresolvedMembers: TargetSummary[] = [];
    for (const t of list) {
      if (t.canonical_group == null) {
        unresolvedMembers.push(t);
        continue;
      }
      let b = byKey.get(t.canonical_group);
      if (!b) {
        b = {
          key: t.canonical_group,
          name: t.canonical_group_name,
          members: [],
          total_frames: 0,
          total_sessions: 0,
          total_integration: 0,
          total_bytes: 0,
          last_session_at: null
        };
        byKey.set(t.canonical_group, b);
      }
      b.members.push(t);
      b.total_frames += t.frame_count;
      b.total_sessions += t.session_count;
      b.total_integration += t.integration_seconds ?? 0;
      b.total_bytes += t.bytes_on_disk;
      // Latest session-end timestamp across all members; lexicographic
      // ordering works because the values are ISO 8601 from sqlite.
      if (
        t.last_session_at &&
        (b.last_session_at == null || t.last_session_at > b.last_session_at)
      ) {
        b.last_session_at = t.last_session_at;
      }
    }
    const all = Array.from(byKey.values());
    // Groups (2+ members) first, sorted by total frames descending. Singles
    // (1 member) next, sorted by frame_count descending. The orchestrator
    // wants the high-volume merged groups at the top where the value is.
    const groups = all
      .filter((b) => b.members.length >= 2)
      .sort((a, b) => b.total_frames - a.total_frames);
    const singles = all
      .filter((b) => b.members.length === 1)
      .sort((a, b) => b.total_frames - a.total_frames);
    let unresolved: TargetBucket | null = null;
    if (unresolvedMembers.length > 0) {
      unresolvedMembers.sort((a, b) => b.frame_count - a.frame_count);
      unresolved = {
        key: null,
        name: null,
        members: unresolvedMembers,
        total_frames: unresolvedMembers.reduce((a, t) => a + t.frame_count, 0),
        total_sessions: unresolvedMembers.reduce(
          (a, t) => a + t.session_count,
          0
        ),
        total_integration: unresolvedMembers.reduce(
          (a, t) => a + (t.integration_seconds ?? 0),
          0
        ),
        total_bytes: unresolvedMembers.reduce(
          (a, t) => a + t.bytes_on_disk,
          0
        ),
        last_session_at: null
      };
    }
    return { groups, singles, unresolved };
  }

  const buckets = $derived(
    targets ? bucketTargets(targets) : { groups: [], singles: [], unresolved: null }
  );

  function toggleGroup(key: string) {
    const next = new Set(expandedGroups);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    expandedGroups = next;
  }

  function openOverrideEditor(t: TargetSummary) {
    // Toggle off when re-clicking the same target's pencil; opening a
    // different target replaces the open editor.
    if (editingOverrideTargetId === t.id) {
      editingOverrideTargetId = null;
      return;
    }
    editingOverrideTargetId = t.id;
    const detail = targetDetails.get(t.id);
    if (detail) ensureNearby(detail);
  }

  function closeOverrideEditor() {
    editingOverrideTargetId = null;
  }

  /** Returns the option key currently selected for `target`.
   * Defaults to the persisted override (or 'AUTO' if no override). */
  function overrideChoice(target: TargetDetail): string {
    const v = overrideChoiceById.get(target.id);
    if (v !== undefined) return v;
    return target.resolved_as?.source === 'override'
      ? target.resolved_as.canonical
      : 'AUTO';
  }

  function setOverrideChoice(id: number, value: string) {
    const next = new Map(overrideChoiceById);
    next.set(id, value);
    overrideChoiceById = next;
  }

  function otherDraft(id: number): string {
    return otherDraftById.get(id) ?? '';
  }

  function setOtherDraft(id: number, value: string) {
    const next = new Map(otherDraftById);
    next.set(id, value);
    otherDraftById = next;
  }

  async function ensureNearby(target: TargetDetail) {
    if (nearbyById.has(target.id)) return;
    loadingNearbyId = target.id;
    try {
      const list = await api.getTargetNearby(target.id);
      const next = new Map(nearbyById);
      next.set(target.id, list);
      nearbyById = next;
    } catch (e) {
      toast.error(`Couldn't load nearby suggestions: ${(e as Error).message}`);
    } finally {
      loadingNearbyId = null;
    }
  }

  async function saveOverride(target: TargetDetail) {
    const choice = overrideChoice(target);
    let value: string | null;
    if (choice === 'AUTO') {
      value = null;
    } else if (choice === 'OTHER') {
      const draft = otherDraft(target.id).trim();
      if (!draft) {
        toast.error('Type a canonical id (e.g. NGC 7000) or pick (auto-resolved).');
        return;
      }
      value = draft;
    } else {
      value = choice;
    }
    savingOverrideId = target.id;
    try {
      const updated = await api.patchTarget(target.id, {
        resolved_canonical_override: value
      });
      // Mutate caches so the caption refreshes without a full reload.
      const cloned = new Map(targetDetails);
      cloned.set(target.id, updated);
      targetDetails = cloned;
      const summary = await api.listTargets();
      targets = summary;
      // Reset transient drafts; the persisted state is now the truth.
      const drafts = new Map(otherDraftById);
      drafts.delete(target.id);
      otherDraftById = drafts;
      const choices = new Map(overrideChoiceById);
      choices.delete(target.id);
      overrideChoiceById = choices;
      // Save commits the user's intent; the pencil editor closes back
      // up so the freshly-grouped row is visible without a second click.
      editingOverrideTargetId = null;
      toast.success(value ? 'Override saved' : 'Override cleared');
    } catch (e) {
      toast.error(`Couldn't save override: ${(e as Error).message}`);
    } finally {
      savingOverrideId = null;
    }
  }

  // Multi-select state: a set of session ids the user has checked in the
  // currently-open target. Reset whenever the open target changes — bundling
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
    }
    openTargetId = next;
    if (next !== null) {
      const detail = targetDetails.get(next);
      if (detail) ensureNearby(detail);
    }
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
      toast.error('No capture root set — open Settings and add one.');
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

{#snippet targetRow(t: TargetSummary, i: number, inGroup: boolean)}
  <li class="target" class:open={openTargetId === t.id} class:in-group={inGroup} style="--stagger: {i}">
    <div class="target-row-wrap">
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
        {#if t.resolved_as && !inGroup}
          <div class="target-resolved muted" title="Resolved by sky position">
            <span class="resolved-arrow" aria-hidden="true">-&gt;</span>
            <span class="resolved-canonical">{t.resolved_as.canonical}</span>
            {#if t.resolved_as.common_name}
              <span class="resolved-common">({t.resolved_as.common_name})</span>
            {/if}
            {#if t.resolved_as.source === 'override'}
              <span class="resolved-suffix">(pinned)</span>
            {:else if t.resolved_as.separation_arcmin != null}
              <span class="resolved-suffix">{sepLabel(t.resolved_as)}</span>
            {/if}
          </div>
        {/if}
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
      <button
        type="button"
        class="pencil-btn"
        aria-label="Edit target override"
        aria-expanded={editingOverrideTargetId === t.id}
        title="Edit target override"
        onclick={(e) => {
          e.stopPropagation();
          openOverrideEditor(t);
        }}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M11.5 1.5l3 3-9 9H2.5v-3l9-9z" />
          <path d="M10 3l3 3" />
        </svg>
      </button>
    </div>

    {#if editingOverrideTargetId === t.id}
      {@const detail = targetDetails.get(t.id)}
      <div class="override-editor-wrap" transition:slide={{ duration: 180, easing: cubicOut }}>
        {#if detail === undefined}
          <p class="muted">Loading…</p>
        {:else}
          {@const choice = overrideChoice(detail)}
          {@const nearbyList = nearbyById.get(detail.id) ?? []}
          {@const overrideInNearby =
            detail.resolved_as?.source === 'override' &&
            nearbyList.some((c) => c.canonical === detail.resolved_as?.canonical)}
          <form
            class="resolve-editor"
            onsubmit={(e) => {
              e.preventDefault();
              saveOverride(detail);
            }}
          >
            <label class="resolve-label" for="resolve-{detail.id}">Resolve as</label>
            <select
              id="resolve-{detail.id}"
              class="resolve-select"
              value={choice}
              onchange={(e) =>
                setOverrideChoice(detail.id, (e.currentTarget as HTMLSelectElement).value)}
            >
              <option value="AUTO">(auto-resolved)</option>
              {#if detail.resolved_as?.source === 'override' && !overrideInNearby}
                <option value={detail.resolved_as.canonical}>
                  {detail.resolved_as.canonical}{detail.resolved_as.common_name
                    ? ` - ${detail.resolved_as.common_name}`
                    : ''} (pinned)
                </option>
              {/if}
              {#if loadingNearbyId === detail.id && nearbyList.length === 0}
                <option disabled>loading nearby…</option>
              {/if}
              {#each nearbyList as cand (cand.canonical)}
                <option value={cand.canonical}>
                  {cand.canonical}{cand.common_name ? ` - ${cand.common_name}` : ''} - {(cand.separation_arcmin != null ? (cand.separation_arcmin / 60).toFixed(2) : '?')} deg
                </option>
              {/each}
              <option value="OTHER">Other…</option>
            </select>
            {#if choice === 'OTHER'}
              <input
                type="text"
                class="resolve-input"
                placeholder="NGC 7000, M 31, C 20…"
                value={otherDraft(detail.id)}
                oninput={(e) =>
                  setOtherDraft(detail.id, (e.currentTarget as HTMLInputElement).value)}
              />
            {/if}
            <button
              type="submit"
              class="resolve-save"
              disabled={savingOverrideId === detail.id}
            >
              {savingOverrideId === detail.id ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              class="resolve-cancel"
              onclick={closeOverrideEditor}
            >
              Cancel
            </button>
          </form>
        {/if}
      </div>
    {/if}

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
                          {s.exptime ?? '?'}s · gain {s.gain ?? '?'} · {s.filter ?? '—'}
                        </span>
                        {#if reason && !checked}
                          <span class="incompat-chip" title={reason}>
                            {reason}
                          </span>
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
                            class="run-btn"
                            onclick={() => toggleRun(s.id)}
                            aria-expanded={runOpenSessionId === s.id}
                          >
                            {runOpenSessionId === s.id ? 'Cancel' : 'Run…'}
                          </button>
                        {/if}
                      </div>
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
{/snippet}

{#snippet groupHeader(b: TargetBucket)}
  {@const key = b.key as string}
  <li class="group-header" class:expanded={expandedGroups.has(key)}>
    <button
      type="button"
      class="group-header-btn"
      aria-expanded={expandedGroups.has(key)}
      onclick={() => toggleGroup(key)}
    >
      <span class="group-chevron" aria-hidden="true">
        {expandedGroups.has(key) ? '▴' : '▾'}
      </span>
      <span class="group-name">
        <span class="group-canonical">{key}</span>
        {#if b.name}
          <span class="group-common"> - {b.name}</span>
        {/if}
      </span>
      <span class="group-meta muted">
        <span class="num">{b.members.length} captures</span>
        <span aria-hidden="true">·</span>
        <span>{b.total_sessions} session{b.total_sessions === 1 ? '' : 's'}</span>
        <span aria-hidden="true">·</span>
        <span class="num">{b.total_frames.toLocaleString()} frames</span>
        {#if b.total_integration > 0}
          <span aria-hidden="true">·</span>
          <span class="num" title="Total useful integration across the bucket">
            {formatIntegrationTime(b.total_integration)} integ
          </span>
        {/if}
        {#if b.total_bytes > 0}
          <span aria-hidden="true">·</span>
          <span class="num">{formatBytes(b.total_bytes)}</span>
        {/if}
      </span>
    </button>
  </li>
{/snippet}

{#if targets === null}
  <p class="muted">Loading targets…</p>
{:else if targets.length === 0}
  <p class="muted">
    No targets yet. Open <a href="/settings" class="link">Settings</a> to set
    a capture root, then come back and hit refresh.
  </p>
{:else}
  <ul class="target-list">
    {#each buckets.groups as b, gi (b.key)}
      {@render groupHeader(b)}
      {#if expandedGroups.has(b.key as string)}
        {#each b.members as t (t.id)}
          {@render targetRow(t, gi, true)}
        {/each}
      {/if}
    {/each}
    {#each buckets.singles as b, si (b.key)}
      {@render targetRow(b.members[0], buckets.groups.length + si, false)}
    {/each}
    {#if buckets.unresolved}
      <li class="unresolved-header" aria-label="Unresolved targets">
        <span class="unresolved-divider" aria-hidden="true"></span>
        <span class="unresolved-label muted">Unresolved</span>
        <span class="unresolved-divider" aria-hidden="true"></span>
      </li>
      {#each buckets.unresolved.members as t, ui (t.id)}
        {@render targetRow(
          t,
          buckets.groups.length + buckets.singles.length + ui,
          false
        )}
      {/each}
    {/if}
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

  /* Constituent rows inside an expanded group: subtle inset + a left rule
     to signal nesting without doubling the cell padding. The card itself
     keeps its full chrome so the pencil hitbox stays on the right edge. */
  .target.in-group {
    border-left: 2px solid var(--accent-soft);
    margin-left: 1.1rem;
  }

  /* Wrap holds the row + the pencil button. Row flexes to fill, pencil
     pins to the right and keeps its own hitbox. */
  .target-row-wrap {
    display: flex;
    align-items: stretch;
  }

  .target-row {
    flex: 1;
    min-width: 0;
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

  /* Tiny pencil affordance on the right edge of every target row. Muted
     baseline so it doesn't compete with the row contents; lights up on
     hover/focus. Vertically centered against the row. */
  .pencil-btn {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    padding: 0 0.85rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: color 140ms ease, background-color 140ms ease;
    flex: 0 0 auto;
  }
  .pencil-btn:hover,
  .pencil-btn:focus-visible {
    color: var(--fg);
    background: rgba(255, 255, 255, 0.03);
  }
  .pencil-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -4px;
  }
  .pencil-btn[aria-expanded='true'] {
    color: var(--accent);
  }

  /* Override-editor reveal slot: lives directly beneath the row, NOT
     inside the .target-detail panel, so the user pins overrides without
     having to expand the sessions list first. */
  .override-editor-wrap {
    padding: 0 1rem 0.8rem;
    border-top: 1px solid var(--hairline);
  }
  .override-editor-wrap .resolve-editor {
    margin-top: 0.7rem;
    margin-bottom: 0;
  }

  /* Cancel button mirrors Save geometry but reads as muted: the user is
     bailing out, not committing. */
  .resolve-cancel {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.85rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .resolve-cancel:hover {
    color: var(--fg);
    border-color: var(--border-strong);
  }

  /* ---------- Group header (canonical bucket with 2+ targets) ---------- */
  .group-header {
    list-style: none;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .group-header-btn {
    width: 100%;
    background: transparent;
    border: none;
    padding: 0.7rem 1.15rem;
    text-align: left;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.7rem;
    color: inherit;
    font: inherit;
  }
  .group-header-btn:hover {
    background: rgba(94, 234, 212, 0.04);
  }
  .group-header-btn:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .group-chevron {
    color: var(--fg-mute);
    font-size: 0.85rem;
    min-width: 0.9rem;
    transition: color 140ms ease;
  }
  .group-header.expanded .group-chevron {
    color: var(--accent);
  }
  .group-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.1rem;
    letter-spacing: -0.005em;
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .group-canonical {
    font-family: var(--font-mono);
    color: var(--accent);
    font-size: 0.95rem;
    font-variant-numeric: tabular-nums;
  }
  .group-common {
    color: var(--fg);
    opacity: 0.85;
    font-family: var(--font-display);
    font-size: 1.05rem;
  }
  .group-meta {
    margin-left: auto;
    font-size: 0.82rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: baseline;
  }

  /* Unresolved section: a low-key divider line; muted label sits in the
     middle so the boundary reads structurally without screaming. */
  .unresolved-header {
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.7rem;
    margin: 0.4rem 0 0.1rem;
    padding: 0 0.3rem;
  }
  .unresolved-divider {
    flex: 1;
    height: 1px;
    background: var(--hairline);
  }
  .unresolved-label {
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
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

  /* Sky-position resolution caption beneath the target name. Stays muted
     so the user's stored name remains the primary read; the ASCII arrow
     keeps the "interpreted as" framing legible. */
  .target-resolved {
    font-size: 0.78rem;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.3rem;
    margin-top: 0.1rem;
  }
  .resolved-arrow {
    color: var(--fg-mute);
  }
  .resolved-canonical {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    color: var(--accent);
  }
  .resolved-common {
    color: var(--fg);
    opacity: 0.85;
  }
  .resolved-suffix {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    opacity: 0.7;
  }

  /* Override editor at the top of the open target panel. */
  .resolve-editor {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.55rem 0.7rem;
    margin: 0 0 0.7rem;
    background: var(--bg-elev-2);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    font-size: 0.85rem;
  }
  .resolve-label {
    color: var(--fg-mute);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.7rem;
    min-width: 5.5rem;
  }
  .resolve-select {
    flex: 1 1 16rem;
    min-width: 0;
    padding: 0.25rem 0.5rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: 6px;
    font: inherit;
    font-family: var(--font-mono);
  }
  .resolve-input {
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
  .resolve-save {
    appearance: none;
    background: transparent;
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 0.25rem 0.85rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .resolve-save:hover:not(:disabled) {
    background: var(--accent-soft);
  }
  .resolve-save:disabled {
    opacity: 0.6;
    cursor: progress;
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
    /* Inline SVG check — recolored to currentColor by clip-path/mask. */
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

  /* ---------- Multi-select action bar ---------- */
  /* Sticky-feeling command bar that appears above the session list when
     the user has 1+ sessions checked. Two rows: header (count + clear)
     on top, controls (template + calibration + run) below — keeps the
     run button anchored at the same spot regardless of how many session
     pills wrap. */
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
  /* Custom select: kill the native chrome, paint our own chevron via an
     inline SVG background. Looks coherent with the dark UI; the popup
     itself is still the OS-native list (acceptable cost for not having
     to ship a full headless dropdown). */
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
