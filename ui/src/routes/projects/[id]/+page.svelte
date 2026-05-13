<!--
  Project detail page: a live, editable view of one stack pipeline.

  Layout (top to bottom):
    1. Header: target name, version pointer, undo/redo, reprocess.
    2. Capture line.
    3. Notes block.
    4. Pipeline accordion: one PipelineRow per visible node.
    5. Error box (when the active job errored).
    6. HistoryStrip: horizontal scroll rail + publish toggles.
    7. CompareController: rendered outside .project-root for backdrop coverage.

  Edits debounce 350ms then PATCH the project. The active job is the
  current_seq's job_id; we tear down/rebind the WS subscription whenever
  the active job changes.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { page } from '$app/stores';
  import {
    api,
    type CalibrationStatus,
    type CostClass,
    type Project,
    type SessionSummary,
    type SuggestedAdditions,
    type TemplateSchema
  } from '$lib/api';
  import { blastRadiusCost, isNodeVisible } from '$lib/graph';
  import { toast } from '$lib/toast.svelte';
  import { formatBytes, formatDuration, formatIntegrationTime, shortAgo } from '$lib/format';
  import { createPipelineState } from '$lib/projects/usePipelineState.svelte';
  import { createJobSubscription } from '$lib/projects/useJobSubscription.svelte';
  import { createPatchQueue } from '$lib/projects/usePatchQueue.svelte';
  import { createCompareSlots } from '$lib/projects/useCompareSlots.svelte';
  import HistoryStrip from '$lib/projects/HistoryStrip.svelte';
  import PipelineRow from '$lib/projects/PipelineRow.svelte';
  import CompareController from '$lib/projects/CompareController.svelte';
  import SessionRow from '$lib/SessionRow.svelte';

  let project = $state<Project | null>(null);
  let schema = $state<TemplateSchema | null>(null);

  const pipeline = createPipelineState();
  const subscription = createJobSubscription(
    (ev, onTerminal) => pipeline.applyEvent(ev, onTerminal)
  );
  const patchQueue = createPatchQueue(() => project);
  const compare = createCompareSlots(() => project);

  const id = $derived($page.params.id ?? '');

  let reprocessing = $state(false);
  let upgrading = $state(false);
  let coverBusy = $state(false);
  let publishBusy = $state<Record<number, boolean>>({});

  // Notes draft
  let descriptionDraft = $state<string>('');
  let descriptionSaving = $state(false);

  // Accordion expansion
  let expandedNodes = $state<Set<string>>(new Set());

  // ---- Suggestion banner state -------------------------------------
  // Local hide flag bumped whenever the dismissal localStorage key
  // changes. Declared as `$state` so the banner's `isDismissed`
  // check refires after a click; the banner re-shows automatically
  // when suggestions_token changes (new orphan session captured).
  let bannerDismissalTick = $state(0);
  let bannerSwapping = $state(false);

  // ---- Manage Sessions modal state ---------------------------------
  let manageOpen = $state(false);
  let sessionRows = $state<SessionSummary[]>([]);
  let sessionRowsLoading = $state(false);
  let modalSelected = $state<Set<number>>(new Set());
  // Cache-eviction preview for the live diff. Refreshed on every
  // checkbox toggle via the dry-run endpoint.
  let modalEvictionBytes = $state<number>(0);
  let modalSaving = $state(false);
  let modalError = $state<string | null>(null);

  // ---- Source sessions (read-only collapsible) ---------------------
  // The "Sessions used" `<details>` lazily fans out api.getSession
  // for each id in project.source_session_ids. Same SessionRow visuals
  // as the Library page but with the interactive affordances suppressed.
  let sourceSessions = $state<SessionSummary[] | null>(null);
  let sourceSessionsLoading = $state(false);
  let sourceSessionsError = $state<string | null>(null);
  let sourceSessionsOpen = $state(false);
  let sourceSessionsForProjectId = $state<string | null>(null);

  // Per-session notes mirror the Library page so the textarea autosaves
  // on blur. Open id, drafts, and saving id are owned here so the row's
  // "saving..." affordance lights up correctly.
  let sessionNotesOpenId = $state<number | null>(null);
  let sessionNotesDraftById = $state<Map<number, string>>(new Map());
  let sessionNotesSavingId = $state<number | null>(null);

  // Derived ordering / lookups
  const schemaByNodeId = $derived.by(() => {
    const out: Record<string, TemplateSchema['nodes'][number]> = {};
    if (!schema) return out;
    for (const n of schema.nodes) out[n.node_id] = n;
    return out;
  });
  const costByNode = $derived.by(() => {
    if (!schema) return {} as Record<string, CostClass>;
    const out: Record<string, CostClass> = {};
    for (const n of schema.nodes) out[n.node_id] = n.cost;
    return out;
  });

  const outputNodeId = $derived.by(() => {
    if (!schema) return null;
    const entries = Object.entries(schema.outputs);
    const named = entries.find(([k]) => k === 'image') ?? entries[0];
    if (!named) return null;
    return named[1].split('.')[0] || null;
  });

  const finalOutput = $derived.by(() => {
    const job = subscription.activeJob;
    if (!job?.outputs) return null;
    const entries = Object.entries(job.outputs);
    const named = entries.find(([k]) => k === 'image');
    return named ?? (entries[0] ?? null);
  });

  const undoDisabled = $derived(
    !project || !project.history.some((h) => h.seq === (project!.current_seq - 1))
  );
  const redoDisabled = $derived(
    !project || !project.history.some((h) => h.seq === (project!.current_seq + 1))
  );

  function pickPreviewPort(nodeId: string): string {
    const node = schema?.nodes.find((n) => n.node_id === nodeId);
    if (node?.outputs) {
      if ('image' in node.outputs) return 'image';
      const first = Object.keys(node.outputs)[0];
      if (first) return first;
    }
    return 'image';
  }

  function upstreamHashFor(nid: string): { hash: string | undefined; port: string | undefined } {
    if (!project) return { hash: undefined, port: undefined };
    const node = project.template.nodes.find((n) => n.id === nid);
    if (!node) return { hash: undefined, port: undefined };
    const src = node.inputs?.image;
    if (!src) return { hash: undefined, port: undefined };
    const srcId = src.split('.')[0];
    return {
      hash: pipeline.nodeHash[srcId],
      port: pipeline.nodePort[srcId] ?? 'image',
    };
  }

  function syncDescriptionFromProject(p: Project | null) {
    const next = p?.description ?? '';
    if (descriptionDraft !== next) descriptionDraft = next;
  }

  function onProjectUpdated(next: Project) {
    project = next;
    syncDescriptionFromProject(next);
    if (schema) pipeline.softReset(schema.nodes);
    subscription.attachToJob(next.current_job_id);
  }

  async function loadProject(rid: string) {
    try {
      const fresh = await api.getProject(rid);
      if (rid !== id) return;
      project = fresh;
      syncDescriptionFromProject(fresh);
      schema = await api.getTemplateSchema(fresh.template_id);
      if (rid !== id) return;
      pipeline.initFromTemplate(
        fresh.template.nodes,
        pickPreviewPort,
        schema.nodes
      );
      await subscription.attachToJob(fresh.current_job_id);
    } catch (e) {
      toast.error(`Couldn't load project ${rid}: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    if (!id) return;
    project = null;
    schema = null;
    // Drop the previous project's source-session list so the collapsible
    // doesn't briefly render stale rows under the new project's header.
    sourceSessions = null;
    sourceSessionsForProjectId = null;
    sourceSessionsError = null;
    sessionNotesOpenId = null;
    sessionNotesDraftById = new Map();
    pipeline.reset();
    subscription.detachFromJob();
    loadProject(id);
  });

  // Land with the output node expanded.
  $effect(() => {
    if (outputNodeId && expandedNodes.size === 0) {
      expandedNodes = new Set([outputNodeId]);
    }
  });

  // Widen the global container while on this route.
  $effect(() => {
    document.body.classList.add('project-page');
    return () => document.body.classList.remove('project-page');
  });

  onDestroy(() => {
    patchQueue.cancel();
    subscription.detachFromJob();
  });

  // ---- Source sessions: lazy-load on collapse open -----------------
  async function loadSourceSessions() {
    if (!project) return;
    const projectId = project.id;
    sourceSessionsLoading = true;
    sourceSessionsError = null;
    try {
      const ids = project.source_session_ids
        .map((s) => Number(s))
        .filter((n) => Number.isFinite(n));
      // Settled-not-rejected so a single 404 (session deleted out from
      // under the project) doesn't break the whole list.
      const results = await Promise.allSettled(ids.map((sid) => api.getSession(sid)));
      // Guard against the user navigating mid-fetch: drop the result if
      // the active project changed.
      if (!project || project.id !== projectId) return;
      const sessions: SessionSummary[] = [];
      for (const r of results) {
        if (r.status === 'fulfilled') sessions.push(r.value);
      }
      sourceSessions = sessions;
      sourceSessionsForProjectId = projectId;
    } catch (e) {
      sourceSessionsError = (e as Error).message;
    } finally {
      sourceSessionsLoading = false;
    }
  }

  function onSourceSessionsToggle(e: Event) {
    const open = (e.currentTarget as HTMLDetailsElement).open;
    sourceSessionsOpen = open;
    // Lazy-load on first open. Subsequent opens hit the cached array
    // (sessions don't change without a rescan, and a rescan is a
    // library-level action).
    if (
      open
      && project
      && (sourceSessions === null || sourceSessionsForProjectId !== project.id)
    ) {
      loadSourceSessions();
    }
  }

  function toggleSessionNotes(s: SessionSummary) {
    if (sessionNotesOpenId === s.id) {
      sessionNotesOpenId = null;
      return;
    }
    if (!sessionNotesDraftById.has(s.id)) {
      const next = new Map(sessionNotesDraftById);
      next.set(s.id, s.description ?? '');
      sessionNotesDraftById = next;
    }
    sessionNotesOpenId = s.id;
  }

  function setSessionNotesDraft(sessionId: number, value: string) {
    const next = new Map(sessionNotesDraftById);
    next.set(sessionId, value);
    sessionNotesDraftById = next;
  }

  async function saveSessionNotesOnBlur(s: SessionSummary) {
    const draft = sessionNotesDraftById.get(s.id) ?? '';
    const server = s.description ?? '';
    if (draft === server) return;
    sessionNotesSavingId = s.id;
    try {
      const resp = await api.patchSession(s.id, { description: draft });
      const updated = resp.session;
      if (sourceSessions) {
        sourceSessions = sourceSessions.map((existing) =>
          existing.id === s.id ? updated : existing,
        );
      }
      const draftNext = new Map(sessionNotesDraftById);
      draftNext.set(s.id, updated.description ?? '');
      sessionNotesDraftById = draftNext;
    } catch (e) {
      toast.error(`Couldn't save notes: ${(e as Error).message}`);
    } finally {
      sessionNotesSavingId = null;
    }
  }

  // Calibration tooltip helpers. Duplicated rather than imported because
  // they're tiny and tightly coupled to the row visuals; lifting them
  // into $lib feels like premature abstraction.
  const KIND_NAME: Record<string, string> = {
    dark: 'Dark',
    flat: 'Flat',
    bias: 'Bias',
  };
  const QUALITY_HELP: Record<string, string> = {
    exact: 'exact match',
    approx: 'approximate match (within tolerance)',
    none: 'no match found',
  };
  function calLabel(kind: string): string {
    return kind[0].toUpperCase();
  }
  function calTitle(c: CalibrationStatus): string {
    const base = `${KIND_NAME[c.kind] ?? c.kind} ${QUALITY_HELP[c.quality] ?? c.quality}`;
    return c.reason && c.quality !== 'exact' ? `${base}\n${c.reason}` : base;
  }
  function shortSessionDate(iso: string | null): string {
    if (!iso) return '';
    return iso.slice(0, 10);
  }

  // ---- Suggestion banner -------------------------------------------
  function dismissalKey(projectId: string, token: string): string {
    // Scope dismissals per (project, suggestions_token). A new captured
    // session changes the token, so the banner reappears without us
    // having to invalidate a separate flag.
    return `astrolab.dismissed_suggestions.${projectId}.${token}`;
  }

  function isDismissed(s: SuggestedAdditions | null, projectId: string): boolean {
    // Touch the tick so the derivation refires after a click; without
    // it the dismissal check would only run once on page load.
    void bannerDismissalTick;
    if (!s || !s.session_ids.length || !s.suggestions_token) return true;
    try {
      return localStorage.getItem(dismissalKey(projectId, s.suggestions_token)) === '1';
    } catch {
      return false;
    }
  }

  function dismissBanner() {
    if (!project || !project.suggested_additions) return;
    const s = project.suggested_additions;
    try {
      localStorage.setItem(dismissalKey(project.id, s.suggestions_token), '1');
    } catch {
      // Quota exceeded / disabled storage / Safari private mode: dismissal
      // doesn't persist but the banner still hides this session.
    }
    bannerDismissalTick++;
  }

  async function addSuggestedSessions() {
    if (!project || !project.suggested_additions || bannerSwapping) return;
    const merged = [
      ...project.source_session_ids
        .map((s) => Number.parseInt(s, 10))
        .filter((n) => Number.isFinite(n)),
      ...project.suggested_additions.session_ids
    ];
    bannerSwapping = true;
    try {
      const next = await api.patchProjectSessions(project.id, {
        session_ids: merged,
        auto_render: true
      });
      // The PATCH response is the Project DTO with eviction stats tacked
      // on; the extras don't break the Project shape so onProjectUpdated
      // only reads Project keys.
      onProjectUpdated(next);
      toast.success(
        `Added ${project.suggested_additions.session_count} session${
          project.suggested_additions.session_count === 1 ? '' : 's'
        }; rendering...`
      );
    } catch (e) {
      toast.error(`Couldn't add sessions: ${(e as Error).message}`);
    } finally {
      bannerSwapping = false;
    }
  }

  // ---- Manage Sessions modal ---------------------------------------
  function modalReset() {
    manageOpen = true;
    modalSelected = new Set();
    sessionRows = [];
    modalEvictionBytes = 0;
    modalError = null;
  }

  async function openManageSessions() {
    if (!project) return;
    modalReset();
    sessionRowsLoading = true;
    modalError = null;
    try {
      // Pull the project's existing sessions + the suggested orphans;
      // these are all the same-canonical sessions the modal needs. We
      // fan out via getSession because there's no batch endpoint and
      // the typical bundle is <20 rows.
      const existingIds = project.source_session_ids
        .map((s) => Number.parseInt(s, 10))
        .filter((n) => Number.isFinite(n));
      const suggestedIds = project.suggested_additions?.session_ids ?? [];
      const ids = Array.from(new Set([...existingIds, ...suggestedIds]));
      const fetched = await Promise.all(ids.map((sid) => api.getSession(sid)));
      sessionRows = fetched.sort((a, b) =>
        (a.started_at ?? '').localeCompare(b.started_at ?? '')
      );
      modalSelected = new Set(existingIds);
      await refreshEvictionPreview();
    } catch (e) {
      modalError = (e as Error).message;
    } finally {
      sessionRowsLoading = false;
    }
  }

  function closeManageSessions() {
    manageOpen = false;
  }

  function toggleModalSelected(sid: number) {
    const next = new Set(modalSelected);
    if (next.has(sid)) next.delete(sid);
    else next.add(sid);
    modalSelected = next;
  }

  async function refreshEvictionPreview() {
    if (!project) return;
    try {
      const r = await api.projectCacheDryRun(project.id, false);
      modalEvictionBytes = r.bytes_to_free;
    } catch {
      modalEvictionBytes = 0;
    }
  }

  function bestCalibrationQuality(s: SessionSummary): 'auto' | 'approx' | 'missing' {
    // Treat the session's calibration list as triage: any "exact" /
    // "approx" hit on a dark beats "missing". Multiple kinds collapse to
    // the lowest-quality match so the badge is honest about the gap.
    // 'not_needed' (scope subtracts on-device) rolls up to 'auto' because
    // calibration is intentional and complete, not absent.
    if (!s.calibration || s.calibration.length === 0) return 'missing';
    let best: 'auto' | 'approx' | 'missing' = 'missing';
    for (const c of s.calibration) {
      if (c.quality === 'exact' || c.quality === 'not_needed') {
        if (best !== 'auto') best = 'auto';
      } else if (c.quality === 'approx') {
        if (best === 'missing') best = 'approx';
      }
    }
    return best;
  }

  async function saveManageSessions() {
    if (!project || modalSaving) return;
    if (modalSelected.size === 0) {
      modalError = 'select at least one session';
      return;
    }
    modalSaving = true;
    modalError = null;
    try {
      const next = await api.patchProjectSessions(project.id, {
        session_ids: Array.from(modalSelected),
        auto_render: true
      });
      onProjectUpdated(next);
      const delta = modalSelected.size - project.source_session_ids.length;
      const verb = delta > 0 ? `Added ${delta}` : delta < 0 ? `Removed ${-delta}` : 'Updated';
      toast.success(`${verb} session${Math.abs(delta) === 1 ? '' : 's'}; rendering...`);
      closeManageSessions();
    } catch (e) {
      modalError = (e as Error).message;
    } finally {
      modalSaving = false;
    }
  }

  // Aggregated counts the live diff in the modal needs. Refires on
  // every checkbox toggle.
  const modalDiff = $derived.by(() => {
    const sel = sessionRows.filter((s) => modalSelected.has(s.id));
    const prevIds = new Set(
      (project?.source_session_ids ?? [])
        .map((s) => Number.parseInt(s, 10))
        .filter((n) => Number.isFinite(n))
    );
    const sessionDelta = sel.length - prevIds.size;
    let frameDelta = 0;
    let integDelta = 0;
    for (const s of sel) {
      if (!prevIds.has(s.id)) {
        frameDelta += s.frame_count;
        integDelta += s.integration_seconds ?? 0;
      }
    }
    for (const s of sessionRows) {
      if (prevIds.has(s.id) && !modalSelected.has(s.id)) {
        frameDelta -= s.frame_count;
        integDelta -= s.integration_seconds ?? 0;
      }
    }
    return {
      sessionDelta,
      frameDelta,
      integDelta,
      selectedCount: sel.length
    };
  });

  function toggleNode(nid: string) {
    const next = new Set(expandedNodes);
    if (next.has(nid)) next.delete(nid);
    else next.add(nid);
    expandedNodes = next;
  }

  function toggleNodeEnabled(
    nodeId: string,
    nodeSchemaProps: Record<string, { default?: unknown }>,
    fullDefaults: Record<string, unknown>,
    currentOverrides: Record<string, unknown>
  ) {
    const cur = ('enabled' in currentOverrides
      ? currentOverrides.enabled
      : fullDefaults.enabled) as boolean | undefined;
    const next = { ...currentOverrides };
    const newValue = !cur;
    if (newValue === fullDefaults.enabled) {
      delete next.enabled;
    } else {
      next.enabled = newValue;
    }
    // Enabled toggle goes through the debounce queue then flushes.
    patchQueue.onNodeOverrideChange(nodeId, next);
    patchQueue.flush().then((updated) => { if (updated) onProjectUpdated(updated); });
  }

  async function reprocess() {
    if (!project) return;
    reprocessing = true;
    try {
      const next = await api.patchProject(project.id, { force: true });
      toast.info('Reprocessing - every step runs from scratch');
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't reprocess: ${(e as Error).message}`);
    } finally {
      reprocessing = false;
    }
  }

  async function upgradeTemplate() {
    if (!project) return;
    const from = project.template_version;
    const to = project.latest_template_version ?? from;
    if (to <= from) return;
    upgrading = true;
    try {
      const next = await api.upgradeProjectTemplate(project.id);
      const dropped = next.dropped_overrides ?? [];
      if (dropped.length > 0) {
        toast.info(
          `Upgraded to v${to}. Dropped ${dropped.length} stale override${dropped.length === 1 ? '' : 's'}: ${dropped.join('; ')}`,
          null
        );
      } else {
        toast.info(`Upgraded template v${from} -> v${to}. Cached nodes will be reused.`);
      }
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't upgrade template: ${(e as Error).message}`);
    } finally {
      upgrading = false;
    }
  }

  async function revertTo(seq: number) {
    if (!project) return;
    try {
      const next = await api.revertProject(project.id, seq);
      onProjectUpdated(next);
    } catch (e) {
      toast.error(`Couldn't revert: ${(e as Error).message}`);
    }
  }

  async function saveDescriptionOnBlur() {
    if (!project) return;
    const server = project.description ?? '';
    if (descriptionDraft === server) return;
    descriptionSaving = true;
    try {
      const next = await api.patchProject(project.id, { description: descriptionDraft });
      project = next;
      syncDescriptionFromProject(next);
    } catch (e) {
      toast.error(`Couldn't save notes: ${(e as Error).message}`);
    } finally {
      descriptionSaving = false;
    }
  }

  async function toggleCover() {
    if (!project || coverBusy) return;
    const seq = project.cover_seq === project.current_seq ? null : project.current_seq;
    coverBusy = true;
    try {
      const next = await api.setProjectCover(project.id, seq);
      onProjectUpdated(next);
      toast.success(seq === null ? 'Cleared cover' : `Cover set to v${seq + 1}`);
    } catch (e) {
      toast.error(`Couldn't set cover: ${(e as Error).message}`);
    } finally {
      coverBusy = false;
    }
  }

  async function togglePublished(seq: number, currentlyPublished: boolean) {
    if (!project || publishBusy[seq]) return;
    publishBusy = { ...publishBusy, [seq]: true };
    try {
      const next = await api.setHistoryPublished(project.id, seq, !currentlyPublished);
      onProjectUpdated(next);
      toast.success(
        !currentlyPublished
          ? `Published v${seq + 1} to the gallery`
          : `Unpublished v${seq + 1}`
      );
    } catch (e) {
      toast.error(`Couldn't update gallery: ${(e as Error).message}`);
    } finally {
      const { [seq]: _, ...rest } = publishBusy;
      publishBusy = rest;
    }
  }

  async function copyToClipboard(text: string, msg = 'Copied to clipboard') {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(msg);
    } catch (e) {
      toast.error(`Copy failed: ${(e as Error).message}`);
    }
  }

  function handleNodeOverrideChange(nodeId: string, partial: Record<string, unknown>) {
    patchQueue.onNodeOverrideChange(nodeId, partial);
    patchQueue.flush().then((updated) => { if (updated) onProjectUpdated(updated); });
  }

  function onKeydown(e: KeyboardEvent) {
    if (!project) return;
    const meta = e.metaKey || e.ctrlKey;
    if (!meta) return;
    if (e.key === 'z' && !e.shiftKey) {
      e.preventDefault();
      const prev = project.current_seq - 1;
      if (prev >= 0 && project.history.some((h) => h.seq === prev)) revertTo(prev);
    } else if ((e.key === 'z' && e.shiftKey) || e.key === 'y') {
      e.preventDefault();
      const next = project.current_seq + 1;
      if (project.history.some((h) => h.seq === next)) revertTo(next);
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div class="project-root">
  <div class="header">
    {#if project}
      <div class="title-block">
        {#if project.display}
          {@const capName = subscription.activeJob?.capture?.target_name?.trim() ?? ''}
          {@const echo = capName && capName.toLowerCase() !== project.display.name.toLowerCase()}
          <h1 title="Catalog: {project.display.canonical}">
            {project.display.name}
            {#if echo}
              <span class="title-canonical muted small">{capName}</span>
            {/if}
          </h1>
        {:else if subscription.activeJob?.capture?.target_name}
          <h1>{subscription.activeJob.capture.target_name}</h1>
        {:else}
          <h1>{project.name}</h1>
        {/if}
      </div>
      <span class="version muted small">
        v{project.current_seq + 1} of {project.history.length}
      </span>
      <button
        type="button"
        class="hbtn"
        onclick={() => revertTo(project!.current_seq - 1)}
        disabled={undoDisabled}
        title="Undo (Cmd-Z)"
      >&#x21B6; Undo</button>
      <button
        type="button"
        class="hbtn"
        onclick={() => revertTo(project!.current_seq + 1)}
        disabled={redoDisabled}
        title="Redo (Cmd-Shift-Z)"
      >&#x21B7; Redo</button>
      {#if project && (project.latest_template_version ?? project.template_version) > project.template_version}
        <button
          type="button"
          class="hbtn upgrade"
          onclick={upgradeTemplate}
          disabled={upgrading || patchQueue.patching}
          title="Move this project to the latest template version. Cached upstream nodes are reused, only changed steps re-run."
        >
          {upgrading
            ? 'Upgrading...'
            : `Upgrade template (v${project.template_version} -> v${project.latest_template_version})`}
        </button>
      {/if}
      <button
        type="button"
        class="hbtn warn reprocess"
        onclick={reprocess}
        disabled={reprocessing || patchQueue.patching}
        title="Re-run every step from scratch (bypasses the cache)"
      >
        {reprocessing ? 'Submitting...' : 'Reprocess'}
      </button>
    {/if}
  </div>

  {#if project?.suggested_additions && !isDismissed(project.suggested_additions, project.id)}
    {@const s = project.suggested_additions}
    <div class="suggest-banner" role="status">
      <span class="suggest-text">
        + Add {s.session_count} more session{s.session_count === 1 ? '' : 's'} to this project?
        <span class="muted small">
          {s.frame_count.toLocaleString()} frames, {formatIntegrationTime(s.integration_seconds)} of integration.
        </span>
      </span>
      <span class="suggest-actions">
        <button
          type="button"
          class="hbtn"
          onclick={addSuggestedSessions}
          disabled={bannerSwapping}
        >{bannerSwapping ? 'Adding...' : 'Add ->'}</button>
        <button
          type="button"
          class="hbtn dismiss"
          onclick={dismissBanner}
          aria-label="Dismiss this suggestion"
          title="Dismiss"
        >&times;</button>
      </span>
    </div>
  {/if}

  {#if project === null}
    <p class="muted">Loading...</p>
  {:else}
    <p class="capture-line muted small">
      {#if subscription.activeJob?.capture?.frame_count}
        <span>{subscription.activeJob.capture.frame_count} frame{subscription.activeJob.capture.frame_count === 1 ? '' : 's'}</span>
      {/if}
      {#if project.capture?.integration_seconds && project.capture.integration_seconds > 0}
        <span aria-hidden="true">·</span>
        <span title="Useful integration time across source sessions">
          {formatIntegrationTime(project.capture.integration_seconds)} integ
        </span>
      {/if}
      {#if project.capture && project.capture.bytes_on_disk > 0}
        <span aria-hidden="true">·</span>
        <span title="Source-frame bytes on disk">{formatBytes(project.capture.bytes_on_disk)}</span>
      {/if}
      <span aria-hidden="true">·</span>
      <span title={project.created_at}>created {shortAgo(project.created_at)}</span>
      {#if subscription.activeJob?.started_at}
        <span aria-hidden="true">·</span>
        <span>{formatDuration(subscription.activeJob.started_at, subscription.activeJob.finished_at)}</span>
      {/if}
      {#if subscription.activeJob}
        <span aria-hidden="true">·</span>
        <span class="status status-{subscription.activeJob.status}">{subscription.activeJob.status}</span>
      {/if}
      {#if patchQueue.patching}
        <span aria-hidden="true">·</span>
        <span class="status status-running">applying...</span>
      {/if}
    </p>

    {@const sessionCount = project.source_session_ids.length}
    <div class="notes-sources">
      <!-- Notes card: same border/header chrome as the Sessions used
           card so the two columns line up on wide viewports. The
           textarea itself drops its own border and rides flush inside
           the card, with the NOTES label living in the matching
           header row. -->
      <div class="notes-block">
        <div class="notes-head">
          <label class="notes-label" for="project-notes">Notes</label>
          {#if descriptionSaving}
            <span class="muted small">saving...</span>
          {/if}
        </div>
        <textarea
          id="project-notes"
          class="notes-area"
          bind:value={descriptionDraft}
          onblur={saveDescriptionOnBlur}
          rows="2"
          placeholder="Add notes (capture conditions, gear tweaks, etc.). Unfocus to save."
        ></textarea>
      </div>

      <!-- Sessions used: collapsible read-only view of the source
           sessions backing this project. Helpful for looking back at
           raw capture metadata without leaving the project page.
           Run / reassign / multi-select are intentionally omitted
           (renders happen project-wide; reassigning would yank a
           session out of this project, which is a surprising side
           effect to hide behind a pencil). The Manage sessions
           affordance lives in the summary so it's reachable even
           when the section is collapsed; stopPropagation on its
           click stops the details from toggling underneath. -->
      <details
        class="sources"
        ontoggle={onSourceSessionsToggle}
      >
      <summary class="sources-summary">
        <svg
          class="sources-chevron"
          viewBox="0 0 24 24"
          width="14"
          height="14"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
        <span class="sources-title">Sessions used</span>
        <span class="sources-count muted">({sessionCount})</span>
        <button
          type="button"
          class="hbtn sources-manage"
          onclick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            openManageSessions();
          }}
          title="Add or remove sessions on this project"
        >Manage sessions...</button>
      </summary>
      <div class="sources-body">
        {#if sourceSessionsLoading && sourceSessions === null}
          <p class="muted small">Loading sessions...</p>
        {:else if sourceSessionsError && sourceSessions === null}
          <p class="muted small">Couldn't load sessions: {sourceSessionsError}</p>
        {:else if sourceSessions && sourceSessions.length === 0}
          <p class="muted small">No source sessions resolved. The capture rows may have been removed since this project was created.</p>
        {:else if sourceSessions}
          <ul class="sources-list">
            {#each sourceSessions as s (s.id)}
              <SessionRow
                session={s}
                shortDate={shortSessionDate(s.started_at)}
                notesOpen={sessionNotesOpenId === s.id}
                notesDraft={sessionNotesDraftById.get(s.id) ?? ''}
                notesSaving={sessionNotesSavingId === s.id}
                onToggleNotes={() => toggleSessionNotes(s)}
                onNotesInput={(v) => setSessionNotesDraft(s.id, v)}
                onNotesBlur={() => saveSessionNotesOnBlur(s)}
                {calLabel}
                {calTitle}
              />
            {/each}
          </ul>
        {/if}
      </div>
    </details>
    </div>

    {#if schema && project}
      {@const isCover = project.cover_seq === project.current_seq}
      <section class="nodes">
        <h2 class="section-h">Pipeline</h2>
        <ol class="node-list">
          {#each schema.nodes as nschema (nschema.node_id)}
            {@const nid = nschema.node_id}
            {@const visible = isNodeVisible(
              nid,
              schemaByNodeId,
              project.current_overrides as Record<string, Record<string, unknown>>
            )}
            {#if visible}
              {@const upstream = upstreamHashFor(nid)}
              <PipelineRow
                {nschema}
                {project}
                status={pipeline.nodeStatus[nid] ?? 'pending'}
                progress={pipeline.nodeProgress[nid]}
                hash={pipeline.nodeHash[nid]}
                port={pipeline.nodePort[nid] ?? 'image'}
                kind={pipeline.nodeKind[nid] ?? nschema.kind ?? nid}
                previewLoaded={pipeline.previewLoaded[nid] ?? false}
                isOutput={nid === outputNodeId}
                isExpanded={expandedNodes.has(nid)}
                closureCost={blastRadiusCost(project.template, nid, costByNode)}
                upstreamHash={upstream.hash}
                upstreamPort={upstream.port}
                durationMs={pipeline.nodeDurationMs[nid]}
                {isCover}
                {coverBusy}
                onToggle={() => toggleNode(nid)}
                onPreviewLoad={() => pipeline.onPreviewLoad(nid)}
                onPreviewError={() => pipeline.onPreviewError(nid)}
                onNodeOverrideChange={handleNodeOverrideChange}
                onToggleEnabled={toggleNodeEnabled}
                onToggleCover={toggleCover}
                onCopyPath={(path) => copyToClipboard(path, 'Copied output path')}
                finalOutputPort={nid === outputNodeId && finalOutput ? finalOutput[0] : undefined}
                finalOutputRef={nid === outputNodeId && finalOutput ? finalOutput[1] : undefined}
              />
            {/if}
          {/each}
        </ol>
      </section>
    {/if}

    {#if subscription.activeJob?.error}
      <section class="error-box">
        <h2 class="section-h">Error</h2>
        <pre>{subscription.activeJob.error}</pre>
      </section>
    {/if}

    {#if project}
      <HistoryStrip
        {project}
        compareA={compare.compareA}
        compareB={compare.compareB}
        compareLoading={compare.compareLoading}
        {publishBusy}
        onRevert={revertTo}
        onToggleCompareSlot={compare.toggleCompareSlot}
        onTogglePublished={togglePublished}
        onOpenCompare={() => { compare.compareOpen = true; }}
        onClearCompare={compare.clear}
      />
    {/if}
  {/if}
</div>

<CompareController
  {project}
  compareA={compare.compareA}
  compareB={compare.compareB}
  compareOpen={compare.compareOpen}
  comparePreviews={compare.comparePreviews}
  onClose={() => { compare.compareOpen = false; }}
/>

{#if manageOpen && project}
  {@const projectCanonical = project.display?.canonical ?? null}
  {@const prevIds = new Set(project.source_session_ids.map((s) => Number.parseInt(s, 10)))}
  <div
    class="manage-backdrop"
    role="presentation"
    onclick={closeManageSessions}
    onkeydown={(e) => e.key === 'Escape' && closeManageSessions()}
  >
    <div
      class="manage-dialog"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Manage sessions"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.key === 'Escape' && closeManageSessions()}
    >
      <header class="manage-dialog-head">
        <span class="muted small">Manage sessions</span>
        {#if projectCanonical}
          <span class="muted">on {projectCanonical}</span>
        {/if}
        <button type="button" class="ghost-btn" onclick={closeManageSessions} aria-label="Close">&times;</button>
      </header>

      {#if sessionRowsLoading}
        <p class="muted">Loading sessions...</p>
      {:else if modalError}
        <p class="err">{modalError}</p>
      {:else}
        <div class="manage-list">
          {#each sessionRows as s (s.id)}
            {@const checked = modalSelected.has(s.id)}
            {@const cal = bestCalibrationQuality(s)}
            {@const wasIn = prevIds.has(s.id)}
            <label class="manage-session-row" class:was-in={wasIn}>
              <input
                type="checkbox"
                checked={checked}
                onchange={() => toggleModalSelected(s.id)}
              />
              <span class="session-meta">
                <span class="session-key">{s.target_name ?? `session ${s.id}`}</span>
                <span class="muted small">
                  {s.frame_count} frames · {formatIntegrationTime(s.integration_seconds ?? 0)} integ
                  {#if s.started_at}
                    · {s.started_at.slice(0, 10)}
                  {/if}
                </span>
              </span>
              <span class="cal-badge cal-{cal}" title="Calibration coverage">{cal}</span>
            </label>
          {/each}
        </div>

        <div class="manage-diff muted small">
          Net change:
          {modalDiff.sessionDelta > 0 ? '+' : ''}{modalDiff.sessionDelta} sessions,
          {modalDiff.frameDelta > 0 ? '+' : ''}{modalDiff.frameDelta.toLocaleString()} frames,
          {modalDiff.integDelta >= 0 ? '+' : ''}{formatIntegrationTime(Math.abs(modalDiff.integDelta))} integration.
          {#if modalEvictionBytes > 0}
            ~{formatBytes(modalEvictionBytes)} of cached renders will be evicted.
          {/if}
        </div>

        <footer class="manage-dialog-foot">
          <button type="button" class="hbtn" onclick={closeManageSessions} disabled={modalSaving}>Cancel</button>
          <button
            type="button"
            class="hbtn warn"
            onclick={saveManageSessions}
            disabled={modalSelected.size === 0 || modalSaving}
            title={modalSelected.size === 0 ? 'select at least one session' : ''}
          >{modalSaving ? 'Saving...' : 'Save & render'}</button>
        </footer>
      {/if}
    </div>
  </div>
{/if}

<style>
  .project-root {
    display: contents;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.25rem;
    flex-wrap: wrap;
  }
  .header h1 {
    margin: 0;
    font-size: 1.5rem;
    line-height: 1.15;
  }
  .title-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 0.05rem;
    flex: 1;
    min-width: 0;
  }
  .title-canonical {
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }
  .version {
    font-variant-numeric: tabular-nums;
  }

  .hbtn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #444);
    color: var(--accent, #5eead4);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
  }
  .hbtn:hover:not(:disabled) {
    background: rgba(94, 234, 212, 0.1);
  }
  .hbtn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .hbtn.warn {
    color: var(--warn, #fbbf24);
    border-color: var(--warn, #fbbf24);
  }
  .hbtn.warn:hover:not(:disabled) {
    background: rgba(251, 191, 36, 0.1);
  }
  .reprocess {
    margin-left: 0.25rem;
  }
  .hbtn.upgrade {
    color: var(--accent-strong, #8be9fd);
    border-color: var(--accent-strong, #8be9fd);
  }
  .hbtn.upgrade:hover:not(:disabled) {
    background: rgba(139, 233, 253, 0.12);
  }

  .capture-line {
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
    flex-wrap: wrap;
    margin: 0.25rem 0 0.75rem;
  }
  .small { font-size: 0.85em; }
  .muted { color: var(--fg-mute, #888); }

  .section-h {
    margin: 1.25rem 0 0.5rem;
    font-size: 1rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }

  /* Notes textarea and the Sessions used details share a wrapper so we
     can lay them out side-by-side on wide viewports. Neither needs the
     full canvas width, and Sessions used grows tall when open — keeping
     it beside Notes pulls the pipeline up by that much. */
  .notes-sources {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin: 0.25rem 0 0.75rem;
  }
  @media (min-width: 960px) {
    .notes-sources {
      display: grid;
      grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
      align-items: start;
    }
  }
  .notes-sources .notes-block,
  .notes-sources .sources {
    margin: 0;
  }

  /* Notes card: matches .sources visually so the two columns share a
     header row at the same y. Border + bg live on the wrapper; the
     textarea is borderless and rides flush inside. */
  .notes-block {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--radius, 8px);
    background: var(--bg-elev);
    overflow: hidden;
  }
  .notes-block:focus-within {
    border-color: var(--accent, #5eead4);
  }
  .notes-head {
    padding: 0.55rem 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border);
    min-height: 2.1rem;
    box-sizing: border-box;
  }
  .notes-label {
    font-size: 0.95rem;
    font-weight: 500;
    color: var(--fg);
    cursor: pointer;
  }
  .notes-area {
    width: 100%;
    background: transparent;
    color: var(--fg, #e6e6e6);
    border: none;
    border-radius: 0;
    padding: 0.5rem 0.85rem;
    font-size: 0.9rem;
    line-height: 1.4;
    resize: vertical;
    font-family: inherit;
  }
  .notes-area:focus {
    outline: none;
  }

  .node-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    grid-auto-flow: dense;
    align-items: start;
    gap: 0.7rem;
  }

  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-running { background: var(--accent-soft); color: var(--accent); }
  .status-queued { background: rgba(255, 255, 255, 0.10); color: var(--fg); }
  .status-completed { background: color-mix(in oklab, var(--good) 18%, transparent); color: var(--good); }
  .status-failed { background: color-mix(in oklab, var(--bad) 18%, transparent); color: var(--bad); }

  .error-box {
    background: #2a1818;
    border: 1px solid #500;
    border-radius: 6px;
    padding: 0.75rem;
    margin: 1rem 0;
  }
  .error-box pre {
    margin: 0.25rem 0 0;
    font-size: 0.85rem;
    white-space: pre-wrap;
  }

  /* Suggestion banner: sits just below the header. Soft accent tint so
     it reads as an invitation, not a warning. Collapses into nothing
     when the suggestion is null / empty / dismissed. */
  .suggest-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    flex-wrap: wrap;
    background: var(--accent-soft, rgba(94, 234, 212, 0.10));
    border: 1px solid rgba(94, 234, 212, 0.25);
    border-radius: 8px;
    padding: 0.5rem 0.75rem;
    margin: 0.25rem 0 0.5rem;
  }
  .suggest-text {
    flex: 1;
    min-width: 0;
  }
  .suggest-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  .hbtn.dismiss {
    color: var(--fg-mute, #888);
    border-color: transparent;
    padding: 0.2rem 0.5rem;
    font-size: 1rem;
    line-height: 1;
  }
  .hbtn.dismiss:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.05);
    color: var(--fg, #e6e6e6);
  }

  /* Sessions used collapsible. Read-only view of the project's source
     sessions. Stays collapsed by default so the pipeline below remains
     the page's center of gravity; rotates a chevron on open like the
     rest of the app's <details> usage. */
  .sources {
    margin: 0 0 0.75rem;
    border: 1px solid var(--border);
    border-radius: var(--radius, 8px);
    background: var(--bg-elev);
    overflow: hidden;
  }
  .sources-summary {
    list-style: none;
    cursor: pointer;
    padding: 0.55rem 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    user-select: none;
    min-height: 2.1rem;
    box-sizing: border-box;
  }
  /* Manage sessions sits at the right of the summary so it's
     reachable whether the section is collapsed or open. The
     auto-margin pushes everything after the count flush to the
     right edge. */
  .sources-manage {
    margin-left: auto;
    padding: 0.2rem 0.55rem;
    font-size: 0.78rem;
  }
  .sources-summary::-webkit-details-marker {
    display: none;
  }
  .sources-title {
    font-size: 0.95rem;
    font-weight: 500;
  }
  .sources-count {
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
  }
  .sources-chevron {
    color: var(--fg-mute);
    transition: transform 160ms ease;
    flex-shrink: 0;
  }
  .sources[open] .sources-chevron {
    transform: rotate(180deg);
  }
  .sources[open] .sources-summary {
    border-bottom: 1px solid var(--border);
  }
  .sources-body {
    padding: 0.6rem 0.85rem 0.85rem;
  }
  .sources-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  /* Manage Sessions modal. Same backdrop convention as the Compare
     dialog: full-viewport scrim, centered card, click-outside +
     Escape close. */
  .manage-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: grid;
    place-items: center;
    z-index: 50;
  }
  .manage-dialog {
    background: var(--bg-elev, #1a1a1a);
    border: 1px solid var(--border, #444);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    width: min(620px, 92vw);
    max-height: 80vh;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
  }
  .manage-dialog-head {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border-bottom: 1px solid var(--border, #444);
    padding-bottom: 0.5rem;
  }
  .manage-dialog-head .ghost-btn {
    margin-left: auto;
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute, #888);
    cursor: pointer;
    font-size: 1rem;
  }
  .manage-dialog-head .ghost-btn:hover {
    color: var(--fg, #e6e6e6);
  }
  .manage-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    max-height: 50vh;
    overflow-y: auto;
  }
  .manage-session-row {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 0.6rem;
    align-items: center;
    padding: 0.45rem 0.6rem;
    border: 1px solid transparent;
    border-radius: 6px;
    cursor: pointer;
  }
  .manage-session-row:hover {
    background: rgba(255, 255, 255, 0.03);
  }
  .manage-session-row.was-in {
    border-color: var(--border, #444);
  }
  .session-meta {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
  }
  .session-key {
    font-variant-numeric: tabular-nums;
  }
  .cal-badge {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border, #444);
  }
  .cal-auto { color: var(--accent, #5eead4); border-color: var(--accent, #5eead4); }
  .cal-approx { color: var(--warn, #fbbf24); border-color: var(--warn, #fbbf24); }
  .cal-missing { color: var(--fg-mute, #888); }
  .manage-diff {
    padding: 0.4rem 0.5rem;
    border-top: 1px solid var(--border, #444);
  }
  .manage-dialog-foot {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
  }
  .err {
    color: var(--bad, #f87171);
  }
</style>
