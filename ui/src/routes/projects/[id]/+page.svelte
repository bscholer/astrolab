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
    type CostClass,
    type Project,
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
  let coverBusy = $state(false);
  let publishBusy = $state<Record<number, boolean>>({});

  // Notes draft
  let descriptionDraft = $state<string>('');
  let descriptionSaving = $state(false);

  // Accordion expansion
  let expandedNodes = $state<Set<string>>(new Set());

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

    <div class="notes-block">
      <label class="notes-label" for="project-notes">
        Notes
        {#if descriptionSaving}
          <span class="muted small">· saving...</span>
        {/if}
      </label>
      <textarea
        id="project-notes"
        class="notes-area"
        bind:value={descriptionDraft}
        onblur={saveDescriptionOnBlur}
        rows="2"
        placeholder="Add notes (capture conditions, gear tweaks, etc.). Unfocus to save."
      ></textarea>
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

  .notes-block {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin: 0.25rem 0 0.75rem;
  }
  .notes-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }
  .notes-area {
    width: 100%;
    background: var(--bg-elev, #1a1a1a);
    color: var(--fg, #e6e6e6);
    border: 1px solid var(--border, #444);
    border-radius: 6px;
    padding: 0.4rem 0.5rem;
    font-size: 0.9rem;
    line-height: 1.4;
    resize: vertical;
    font-family: inherit;
  }
  .notes-area:focus {
    outline: none;
    border-color: var(--accent, #5eead4);
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
</style>
