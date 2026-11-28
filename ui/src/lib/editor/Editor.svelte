<!--
  The editor: one screen, no page scroll. The picture fills what the tools
  don't; one tool's few controls sit beside it (desktop, tablet landscape) or
  under it (phone, tablet portrait), with the tools in a tab bar.

  Edits happen on a local draft of the project's overrides. While a slider
  moves, the instant preview (server/quick_preview.py) re-renders a small
  copy of the picture; a second and a half after the last change the draft
  is committed to the pipeline as this editing session's version (one
  version per session, server/projects.py), and the full-size render
  follows in the background. Undo and redo walk the draft.
-->
<script lang="ts">
  import { onDestroy, onMount, untrack, type Snippet } from 'svelte';
  import { api, ApiError, FRAMING_KEYS, type Project } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import CropEditor from '$lib/CropEditor.svelte';
  import Icon from './Icon.svelte';
  import Control from './Control.svelte';
  import {
    CONTROLS, defaultOf, has, needsRender, templateParams,
    type ControlDef, type Ctx, type Draft, type Tab,
  } from './controls';
  import { PALETTES, STYLES, type Look } from './looks';

  type View = 'edit' | 'pipeline' | 'details';
  type Props = {
    project: Project;
    title: string;
    subtitle?: string;
    /** The current version's full-size render, when there is one. */
    resultSrc: string | null;
    /** A pipeline job for this project is running. */
    rendering: boolean;
    /** Per-step state of the current job, from the page's pipeline view. */
    nodeStatus?: Record<string, string>;
    nodeProgress?: Record<string, { fraction: number; message: string }>;
    cropNode: string;
    cropInput: { hash?: string; port?: string };
    cropPreviewUrl: string | null;
    onProjectUpdated: (p: Project) => void;
    onView: (v: View) => void;
    onReprocess?: () => void;
    upgradeLabel?: string | null;
    onUpgrade?: () => void;
    versions: Snippet;
  };
  let {
    project, title, subtitle = '', resultSrc, rendering, nodeStatus = {}, nodeProgress = {},
    cropNode, cropInput, cropPreviewUrl,
    onProjectUpdated, onView, onReprocess, upgradeLabel = null, onUpgrade, versions,
  }: Props = $props();

  const TABS: { id: Tab; label: string; icon: string }[] = [
    { id: 'looks', label: 'Looks', icon: 'looks' },
    { id: 'crop', label: 'Crop', icon: 'crop' },
    { id: 'light', label: 'Light', icon: 'light' },
    { id: 'color', label: 'Color', icon: 'color' },
    { id: 'detail', label: 'Detail', icon: 'detail' },
    { id: 'versions', label: 'Versions', icon: 'versions' },
  ];
  let tab = $state<Tab>('looks');

  // ---- draft, undo, commit ---------------------------------------------------
  const clone = (d: Draft): Draft => JSON.parse(JSON.stringify(d ?? {}));
  const norm = (d: Draft) => JSON.stringify(
    Object.fromEntries(Object.entries(d).sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => [k, Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b)))]))
  );

  let session = newSession();
  // svelte-ignore state_referenced_locally
  let draft = $state<Draft>(clone(project.current_overrides as Draft));
  // svelte-ignore state_referenced_locally
  let startDraft = $state<Draft>(clone(project.current_overrides as Draft));
  let undoStack = $state<Draft[]>([]);
  let redoStack = $state<Draft[]>([]);
  let committing = $state(false);
  let commitTimer: ReturnType<typeof setTimeout> | null = null;
  // What the server had last time we looked, to tell our own commits from
  // changes made elsewhere (a revert, an upgrade, a new night).
  // svelte-ignore state_referenced_locally
  let serverSeen = norm(project.current_overrides as Draft);

  const ctx = $derived<Ctx>({ project, draft });
  const serverDraft = $derived(project.current_overrides as Draft);
  const dirty = $derived(norm(draft) !== norm(serverDraft));

  function newSession(): string {
    return typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  }

  $effect(() => {
    const now = norm(serverDraft);
    if (now === serverSeen) return;
    serverSeen = now;
    // Changed elsewhere: adopt it unless the user is mid-edit.
    if (!dirty || committing) return;
    draft = clone(serverDraft);
  });

  /** Live change while a control moves: preview only. */
  function live(next: Draft) {
    draft = next;
    if (commitTimer) clearTimeout(commitTimer);
  }

  /** A finished change: one undo step, commit after a pause. */
  let preEdit: Draft | null = null;
  function begin() {
    if (preEdit === null) preEdit = clone(draft);
  }
  function settle(next: Draft) {
    const before = preEdit ?? clone(draft);
    preEdit = null;
    draft = next;
    if (norm(before) !== norm(next)) {
      undoStack = [...undoStack.slice(-79), before];
      redoStack = [];
    }
    scheduleCommit(needsRender(before, next) ? 300 : 1500);
  }

  function scheduleCommit(ms: number) {
    if (commitTimer) clearTimeout(commitTimer);
    commitTimer = setTimeout(commit, ms);
  }

  async function commit(): Promise<void> {
    commitTimer = null;
    if (!dirty || committing) return;
    const server = serverDraft;
    const overrides: Record<string, Record<string, unknown> | null> = {};
    for (const node of new Set([...Object.keys(server), ...Object.keys(draft)])) {
      const prev = server[node] ?? {};
      const next = draft[node] ?? {};
      const diff: Record<string, unknown> = {};
      for (const k of Object.keys(prev)) if (!(k in next)) diff[k] = null;
      for (const [k, v] of Object.entries(next)) if (JSON.stringify(prev[k]) !== JSON.stringify(v)) diff[k] = v;
      if (Object.keys(diff).length) overrides[node] = diff;
    }
    if (!Object.keys(overrides).length) return;
    committing = true;
    const sent = norm(draft);
    try {
      const updated = await api.patchProject(project.id, { overrides, session });
      serverSeen = norm(updated.current_overrides as Draft);
      onProjectUpdated(updated);
    } catch (e) {
      toast.error(`Couldn't save the edit: ${(e as Error).message}`);
    } finally {
      committing = false;
      if (norm(draft) !== sent) scheduleCommit(800);
    }
  }

  function undo() {
    const prev = undoStack.at(-1);
    if (!prev) return;
    undoStack = undoStack.slice(0, -1);
    redoStack = [...redoStack, clone(draft)];
    draft = prev;
    scheduleCommit(needsRender(prev, serverDraft) ? 300 : 1500);
  }
  function redo() {
    const next = redoStack.at(-1);
    if (!next) return;
    redoStack = redoStack.slice(0, -1);
    undoStack = [...undoStack, clone(draft)];
    draft = next;
    scheduleCommit(needsRender(next, serverDraft) ? 300 : 1500);
  }

  /** Later edits go into a new version rather than amending this one. */
  async function saveVersion() {
    await commit();
    session = newSession();
    startDraft = clone(draft);
    toast.success('Saved. New edits start a new version.');
  }

  // ---- instant preview -------------------------------------------------------
  let previewUrl = $state<string | null>(null);
  let beforeUrl = $state<string | null>(null);
  let previewOk = $state(true);
  let previewBusy = $state(false);
  let queued = false;
  let maxEdge = 1280;
  // The pipeline output the preview starts from changes when a render
  // finishes; refetch then.
  // ---- render progress --------------------------------------------------------
  // The preview starts from the step before the finishing chain. While a
  // render is re-making that step (after a crop, gradient or noise change)
  // the preview still shows the old one: "cooking".
  const FINISH_KINDS = new Set([
    'channel_map', 'color_balance', 'auto_bp_shift', 'stretch',
    'starnet_extract', 'starnet_replace', 'starnet_recombine', 'save_image',
  ]);
  const DONE = new Set(['completed', 'cached', 'skipped']);
  const baseNode = $derived.by(() => {
    const nodes = project.template.nodes;
    let i = nodes.length;
    while (i > 0 && FINISH_KINDS.has(nodes[i - 1].kind)) i--;
    const src = (nodes[i]?.inputs as Record<string, string> | undefined)?.image;
    return src ? src.split('.')[0] : null;
  });
  const cooking = $derived(
    rendering && !!baseNode && !!nodeStatus[baseNode] && !DONE.has(nodeStatus[baseNode])
  );
  /** Share of the job's steps done, counting the running step's progress. */
  const jobPct = $derived.by(() => {
    // Per-night steps report as "convert@43"; count those instead of the
    // template entry ("convert") they stand for.
    const keys = Object.keys(nodeStatus);
    const ids = keys.filter((k) => k.includes('@') || !keys.some((o) => o.startsWith(`${k}@`)));
    if (!ids.length) return 0;
    let done = 0;
    for (const id of ids) {
      const st = nodeStatus[id];
      if (st && DONE.has(st)) done += 1;
      else if (st === 'running') done += Math.min(1, Math.max(0, nodeProgress[id]?.fraction ?? 0));
    }
    return Math.round((100 * done) / ids.length);
  });
  const STEP_LABEL: Record<string, string> = {
    convert: 'Reading frames', calibrate: 'Calibrating', register: 'Aligning', offset: 'Leveling frames',
    resample: 'Resampling', bg_extract: 'Flattening frames', extract: 'Splitting H-alpha and OIII',
    pack: 'Packing channels', narrowband_extract: 'Splitting channels', narrowband_compose: 'Composing channels',
    stack: 'Stacking', combine: 'Combining nights', crop: 'Cropping', frame: 'Cropping',
    graxpert_bg: 'Removing gradient', graxpert_denoise: 'Reducing noise',
    channel_map: 'Coloring', color_balance: 'Balancing color', auto_bp_shift: 'Setting black point',
    stretch: 'Stretching', starnet_extract: 'Finding stars', starnet_replace: 'Rebuilding stars',
    starnet_recombine: 'Finishing', save: 'Saving',
  };
  const runningLabel = $derived.by(() => {
    const id = Object.keys(nodeStatus).find((k) => nodeStatus[k] === 'running');
    return id ? (STEP_LABEL[id.split('@')[0]] ?? 'Rendering') : 'Rendering';
  });

  const baseKey = $derived(`${project.current_job_id}:${cooking ? 'c' : 'b'}:${rendering ? 'r' : 'd'}`);

  async function fetchPreview() {
    if (previewBusy) {
      queued = true;
      return;
    }
    previewBusy = true;
    const want = norm(draft);
    try {
      const blob = await api.quickPreview(project.id, JSON.parse(want), maxEdge);
      const url = URL.createObjectURL(blob);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = url;
      previewOk = true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) previewOk = false;
      else if (!(e instanceof DOMException)) console.warn('preview failed', e);
    } finally {
      previewBusy = false;
      if (queued || norm(draft) !== want) {
        queued = false;
        fetchPreview();
      }
    }
  }

  async function fetchBefore() {
    try {
      const blob = await api.quickPreview(project.id, JSON.parse(norm(startDraft)), maxEdge);
      if (beforeUrl) URL.revokeObjectURL(beforeUrl);
      beforeUrl = URL.createObjectURL(blob);
    } catch {
      beforeUrl = null;
    }
  }

  // untrack: the fetches read and replace the URLs they render, which must
  // not re-run the effect that started them.
  $effect(() => {
    void norm(draft);
    void baseKey;
    untrack(fetchPreview);
  });
  $effect(() => {
    void norm(startDraft);
    void baseKey;
    untrack(fetchBefore);
  });

  // ---- looks thumbnails ------------------------------------------------------
  let thumbs = $state<Record<string, string>>({});
  let thumbsFor = '';
  let thumbTimer: ReturnType<typeof setTimeout> | null = null;
  const lookList = $derived([...PALETTES, ...STYLES].filter((l) => l.show(ctx)));

  $effect(() => {
    if (tab !== 'looks' || !previewOk) return;
    const key = `${norm(draft)}|${baseKey}`;
    if (key === thumbsFor) return;
    if (thumbTimer) clearTimeout(thumbTimer);
    thumbTimer = setTimeout(() => untrack(() => loadThumbs(key)), 500);
  });

  async function loadThumbs(key: string) {
      thumbsFor = key;
      for (const look of lookList) {
        if (thumbsFor !== key) return;
        try {
          const d = look.apply(ctx);
          const blob = await api.quickPreview(project.id, JSON.parse(norm(d)), 360);
          if (thumbs[look.id]) URL.revokeObjectURL(thumbs[look.id]);
          thumbs = { ...thumbs, [look.id]: URL.createObjectURL(blob) };
        } catch {
          return;
        }
      }
  }

  function applyLook(look: Look) {
    begin();
    settle(look.apply(ctx));
  }
  const lookActive = (l: Look) => (l.active ? l.active(ctx) : false);

  // ---- compare ---------------------------------------------------------------
  let compare = $state<'after' | 'split'>('after');
  let holding = $state(false);
  let splitPos = $state(50);
  let holdTimer: ReturnType<typeof setTimeout> | null = null;
  let stage = $state<HTMLDivElement | null>(null);

  function onStageDown(e: PointerEvent) {
    if (compare === 'split') {
      dragSplit(e);
      return;
    }
    holdTimer = setTimeout(() => (holding = true), 220);
  }
  function onStageUp() {
    if (holdTimer) clearTimeout(holdTimer);
    holdTimer = null;
    holding = false;
  }
  function dragSplit(e: PointerEvent) {
    if (!stage) return;
    const rect = stage.getBoundingClientRect();
    const move = (ev: PointerEvent) => {
      splitPos = Math.min(100, Math.max(0, ((ev.clientX - rect.left) / rect.width) * 100));
    };
    move(e);
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  const shown = $derived(previewOk && previewUrl ? previewUrl : resultSrc);
  const before = $derived(previewOk && beforeUrl ? beforeUrl : resultSrc);

  // ---- crop ------------------------------------------------------------------
  const cropParams = $derived({ ...templateParams(project, cropNode), ...(draft[cropNode] ?? {}) });
  function setCrop(next: Record<string, number> | null) {
    begin();
    const o = { ...(draft[cropNode] ?? {}) };
    for (const key of [...FRAMING_KEYS, 'enabled']) delete o[key];
    const values = Object.fromEntries(Object.entries(next ?? {}).filter(([, v]) => v !== 0));
    const on = cropNode === 'frame' && next ? { enabled: true } : {};
    const node = { ...o, ...values, ...on };
    const d = { ...draft };
    if (Object.keys(node).length) d[cropNode] = node;
    else delete d[cropNode];
    settle(d);
  }
  const customCrop = $derived(
    Number(cropParams.width ?? 0) > 0 || Number(cropParams.sky_width ?? 0) > 0
  );

  // ---- controls --------------------------------------------------------------
  const tabControls = $derived(CONTROLS.filter((c) => c.tab === tab && c.show(ctx)));
  const pendingRender = $derived(needsRender(draft, serverDraft) || (rendering && needsRender(startDraft, draft)));

  function controlInput(def: ControlDef, v: number | boolean) {
    begin();
    if (def.kind === 'slider') live(def.write(ctx, v as number));
    else settle(def.write(ctx, v as boolean));
  }
  function controlChange(def: ControlDef, v: number | boolean) {
    begin();
    settle(def.kind === 'slider' ? def.write(ctx, v as number) : def.write(ctx, v as boolean));
  }

  // ---- export, menu, keys ----------------------------------------------------
  let menuOpen = $state(false);
  async function exportPng() {
    if (commitTimer || dirty) await commit();
    if (rendering || committing) {
      toast.info('The full-size picture is still rendering. Export again when it is done.');
      return;
    }
    const a = document.createElement('a');
    a.href = `/api/projects/${project.id}/export`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function onKey(e: KeyboardEvent) {
    const t = e.target as HTMLElement | null;
    if (t && ['INPUT', 'TEXTAREA'].includes(t.tagName) && (t as HTMLInputElement).type !== 'range') return;
    const meta = e.metaKey || e.ctrlKey;
    if (meta && e.key === 'z' && !e.shiftKey) {
      e.preventDefault();
      undo();
    } else if (meta && ((e.key === 'z' && e.shiftKey) || e.key === 'y')) {
      e.preventDefault();
      redo();
    } else if (!meta && e.key === '\\') {
      compare = compare === 'split' ? 'after' : 'split';
    } else if (e.key === 'Escape') {
      menuOpen = false;
    }
  }

  // The editor sits under the site header and fills the rest of the
  // window, so it needs to know where it starts.
  let root = $state<HTMLDivElement | undefined>();
  let top = $state(0);
  function measure() {
    if (root) top = Math.round(root.getBoundingClientRect().top + window.scrollY);
  }

  onMount(() => {
    measure();
    window.addEventListener('resize', measure);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    maxEdge = Math.round(Math.min(1600, Math.max(900, Math.max(window.innerWidth, window.innerHeight) * dpr * 0.7)));
    document.body.classList.add('editor-open');
    const flush = () => { if (dirty) commit(); };
    window.addEventListener('pagehide', flush);
    return () => {
      window.removeEventListener('pagehide', flush);
      window.removeEventListener('resize', measure);
    };
  });
  onDestroy(() => {
    document.body.classList.remove('editor-open');
    if (commitTimer) {
      clearTimeout(commitTimer);
      if (dirty) commit();
    }
    for (const u of [previewUrl, beforeUrl, ...Object.values(thumbs)]) if (u) URL.revokeObjectURL(u);
  });

  const status = $derived(
    committing ? 'Saving…'
      : dirty ? 'Edited'
      : rendering ? `${runningLabel}… ${jobPct}%`
      : `v${project.current_seq + 1}`
  );
</script>

<svelte:window onkeydown={onKey} />

<div class="editor" bind:this={root} style="--ed-top: {top}px">
  <header class="top">
    <h1 class="t">{title}{#if subtitle}<span class="canon">{subtitle}</span>{/if}</h1>
    <span class="s">{status}</span>
    <button class="icon-btn wide-only" onclick={undo} disabled={!undoStack.length} aria-label="Undo" title="Undo (Cmd-Z)"><Icon name="undo" size={18} /></button>
    <button class="icon-btn wide-only" onclick={redo} disabled={!redoStack.length} aria-label="Redo" title="Redo (Cmd-Shift-Z)"><Icon name="redo" size={18} /></button>
    <div class="menu-wrap">
      <button class="icon-btn" onclick={() => (menuOpen = !menuOpen)} aria-label="More" aria-expanded={menuOpen}><Icon name="more" /></button>
      {#if menuOpen}
        <div class="menu" role="menu">
          <a role="menuitem" href="/api/projects/{project.id}/export?kind=stacked" download>Download linear stack (FITS)</a>
          {#if resultSrc}<a role="menuitem" href={resultSrc} target="_blank" rel="noopener">Open full size</a>{/if}
          {#if upgradeLabel && onUpgrade}
            <button role="menuitem" onclick={() => { menuOpen = false; onUpgrade?.(); }}>{upgradeLabel}</button>
          {/if}
          {#if onReprocess}
            <button role="menuitem" onclick={() => { menuOpen = false; onReprocess?.(); }}>Reprocess from scratch</button>
          {/if}
        </div>
      {/if}
    </div>
    <button class="export" onclick={exportPng} class:busy={rendering || committing}
      title={rendering ? `Rendering the full-size picture, ${jobPct}%` : 'Download the full-size PNG'}>
      {#if rendering || committing}<span class="spin"></span>{:else}<Icon name="download" size={16} />{/if} Export
    </button>
  </header>
  <div class="views" role="tablist" aria-label="Project views">
    <button role="tab" class="on" aria-selected="true">Edit</button>
    <button role="tab" aria-selected="false" onclick={() => onView('pipeline')}>Pipeline</button>
    <button role="tab" aria-selected="false" onclick={() => onView('details')}>Details</button>
  </div>

  <div
    class="stage"
    class:cropping={tab === 'crop'}
    class:stale={cooking && tab !== 'crop'}
    bind:this={stage}
    onpointerdown={tab === 'crop' ? undefined : onStageDown}
    onpointerup={onStageUp}
    onpointerleave={onStageUp}
    oncontextmenu={(e) => e.preventDefault()}
    role="img"
    aria-label="Picture of {title}"
  >
    {#if tab === 'crop'}
      <div class="crop-wrap">
        <CropEditor
          previewUrl={cropPreviewUrl}
          hash={cropInput.hash}
          port={cropInput.port ?? 'image'}
          params={cropParams}
          onparams={setCrop}
          maxHeight="calc(100cqh - 6.5rem)"
        />
      </div>
    {:else if shown}
      {#if before && (compare === 'split' || holding)}
        <img class="pic" src={before} alt="" draggable="false" />
      {/if}
      {#if !holding}
        <img
          class="pic"
          src={shown}
          alt="Your edit"
          draggable="false"
          style={compare === 'split' ? `clip-path: inset(0 0 0 ${splitPos}%)` : ''}
        />
      {/if}
      {#if compare === 'split' && !holding}
        <div class="divider" style="left: {splitPos}%"><span class="knob"><Icon name="split" size={16} /></span></div>
        <span class="tag l">Before</span><span class="tag r">After</span>
      {/if}
      {#if holding}<span class="tag l">Before</span>{/if}
    {:else}
      <div class="empty">{rendering ? 'Rendering…' : 'No render yet'}</div>
    {/if}
    {#if tab !== 'crop' && cooking}
      <div class="cooking" role="status" aria-live="polite">
        <span class="cook-chip"><span class="spin"></span>{runningLabel}… {jobPct}%</span>
      </div>
    {/if}
    {#if rendering}
      <div class="jobbar" aria-hidden="true"><span style="width: {Math.max(jobPct, 3)}%"></span></div>
    {/if}
    {#if tab !== 'crop'}
      <div class="overlay">
        <button class="pill" class:on={compare === 'split'} onclick={(e) => { e.stopPropagation(); compare = compare === 'split' ? 'after' : 'split'; }}
          onpointerdown={(e) => e.stopPropagation()} title="Compare with how this edit started (\)">
          <Icon name="split" size={15} /> Compare
        </button>
        {#if rendering && !cooking}<span class="pill quiet" title="The full-size picture is rendering; the preview is up to date"><span class="spin"></span>Full size {jobPct}%</span>
        {:else if pendingRender && !rendering}<span class="pill quiet"><span class="spin"></span>Saving</span>
        {:else if !previewOk && !rendering}<span class="pill quiet">Full render</span>{/if}
        <span class="narrow-only undo-pills">
          <button class="pill round" onclick={(e) => { e.stopPropagation(); undo(); }} onpointerdown={(e) => e.stopPropagation()}
            disabled={!undoStack.length} aria-label="Undo"><Icon name="undo" size={16} /></button>
          <button class="pill round" onclick={(e) => { e.stopPropagation(); redo(); }} onpointerdown={(e) => e.stopPropagation()}
            disabled={!redoStack.length} aria-label="Redo"><Icon name="redo" size={16} /></button>
        </span>
      </div>
    {/if}
  </div>

  <section class="panel" aria-label={TABS.find((t) => t.id === tab)?.label}>
    {#if tab === 'looks'}
      {#if PALETTES.some((l) => l.show(ctx))}
        <h3>Palette</h3>
        <div class="cards">
          {#each PALETTES.filter((l) => l.show(ctx)) as look (look.id)}
            <button class="card" class:on={lookActive(look)} onclick={() => applyLook(look)}>
              <span class="thumb" class:loading={cooking || !thumbs[look.id]}>{#if thumbs[look.id]}<img src={thumbs[look.id]} alt="" />{/if}<span class="spin"></span></span>
              <span class="cn">{look.name}</span><span class="cs">{look.sub}</span>
            </button>
          {/each}
        </div>
      {/if}
      <h3>Style</h3>
      <div class="cards">
        {#each STYLES.filter((l) => l.show(ctx)) as look (look.id)}
          <button class="card" onclick={() => applyLook(look)}>
            <span class="thumb" class:loading={cooking || !thumbs[look.id]}>{#if thumbs[look.id]}<img src={thumbs[look.id]} alt="" />{/if}<span class="spin"></span></span>
            <span class="cn">{look.name}</span><span class="cs">{look.sub}</span>
          </button>
        {/each}
      </div>
    {:else if tab === 'crop'}
      <p class="note"><b>{customCrop ? 'Custom framing.' : 'Whole frame, empty edges trimmed.'}</b>
        Drag a box on the picture and turn it by the knob above it. The framing stays on the same patch of sky as new nights are added.</p>
      <p class="note muted">Cropping re-renders the steps after it, so the other tools catch up after a short wait.</p>
    {:else if tab === 'versions'}
      <div class="versions">{@render versions()}</div>
      <div class="crop-actions">
        <span class="muted">Edits in one sitting make one version.</span>
        <button class="ghost" onclick={saveVersion} disabled={!dirty && undoStack.length === 0}>Keep as a version</button>
      </div>
    {:else}
      <div class="controls">
        {#each tabControls as def (def.id)}
          <Control
            {def}
            value={def.read(ctx)}
            dflt={defaultOf(def, ctx)}
            oninput={(v) => controlInput(def, v)}
            onchange={(v) => controlChange(def, v)}
          />
        {:else}
          <p class="note muted">Nothing to adjust here for this recipe.</p>
        {/each}
      </div>
      {#if tab === 'color' && has(project, 'channel_map')}
        <p class="note muted">Palettes are under Looks.</p>
      {/if}
    {/if}
  </section>

  <nav class="tools" aria-label="Tools">
    {#each TABS as t (t.id)}
      <button class:on={tab === t.id} onclick={() => (tab = t.id)} aria-current={tab === t.id ? 'page' : undefined}>
        <Icon name={t.icon} size={20} /><span>{t.label}</span>
      </button>
    {/each}
  </nav>
</div>

<style>
  .editor {
    --ed-track: rgba(233, 241, 255, 0.12);
    --ed-surface: var(--bg-elev);
    color: var(--fg);
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto auto minmax(0, 1fr) auto auto;
    grid-template-areas: 'top' 'views' 'stage' 'panel' 'tools';
    height: calc(100dvh - var(--ed-top, 64px) - 0.75rem);
    min-height: 420px;
  }

  /* ---- title row and view tabs, laid out like the Pipeline and Details views */
  .top {
    grid-area: top;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.15rem;
  }
  .t {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: 1.5rem;
    line-height: 1.15;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .canon {
    margin-left: 0.5rem;
    color: var(--fg-mute);
    font-size: 0.85em;
  }
  .s {
    font-size: 0.85rem;
    color: var(--fg-mute);
    white-space: nowrap;
    margin-right: 0.25rem;
  }
  .icon-btn {
    appearance: none;
    flex: none;
    width: 38px;
    height: 38px;
    min-height: 0;
    padding: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg-elev);
    color: var(--fg);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }
  .icon-btn:disabled {
    opacity: 0.35;
    cursor: default;
  }
  .export {
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    height: 38px;
    min-height: 0;
    padding: 0 0.95rem;
    border-radius: 999px;
    border: 0;
    background: #f4f7fb;
    color: #08101f;
    font: inherit;
    font-weight: 600;
    font-size: 0.9rem;
    cursor: pointer;
    flex: none;
  }
  .views {
    grid-area: views;
    justify-self: start;
    display: inline-flex;
    gap: 0.2rem;
    padding: 0.2rem;
    margin: 0 0 1rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--bg-elev);
  }
  .views button {
    appearance: none;
    border: 0;
    background: none;
    color: var(--fg-mute);
    font: inherit;
    font-size: 0.88rem;
    padding: 0.35rem 1rem;
    min-height: 0;
    border-radius: 999px;
    cursor: pointer;
  }
  .views button.on {
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 600;
  }
  .menu-wrap {
    position: relative;
  }
  .menu {
    position: absolute;
    right: 0;
    top: calc(100% + 6px);
    z-index: 5;
    min-width: 230px;
    padding: 0.35rem;
    border-radius: 14px;
    border: 1px solid var(--border);
    background: var(--ed-surface);
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
    display: flex;
    flex-direction: column;
  }
  .menu > * {
    appearance: none;
    text-align: left;
    border: 0;
    background: none;
    color: var(--fg);
    font: inherit;
    font-size: 0.9rem;
    padding: 0.6rem 0.75rem;
    min-height: 0;
    border-radius: 9px;
    text-decoration: none;
    cursor: pointer;
  }
  .menu > *:hover {
    background: rgba(255, 255, 255, 0.05);
  }

  /* ---- picture */
  .stage {
    grid-area: stage;
    position: relative;
    border-radius: 16px;
    overflow: hidden;
    background: #000;
    border: 1px solid var(--border);
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .stage.stale .pic {
    opacity: 0.45;
    filter: saturate(0.6) blur(1px);
    transition: opacity 200ms ease, filter 200ms ease;
  }
  .cooking {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    pointer-events: none;
    z-index: 2;
  }
  .cook-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.55rem 1rem;
    border-radius: 999px;
    background: rgba(8, 12, 24, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.15);
    font-size: 0.9rem;
    font-variant-numeric: tabular-nums;
  }
  .jobbar {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 3px;
    background: rgba(94, 234, 212, 0.12);
    z-index: 3;
    pointer-events: none;
  }
  .jobbar span {
    display: block;
    height: 100%;
    background: var(--accent);
    transition: width 500ms ease-out;
  }
  .thumb {
    position: relative;
  }
  .thumb .spin {
    position: absolute;
    top: 50%;
    left: 50%;
    margin: -7px 0 0 -7px;
    width: 14px;
    height: 14px;
    color: var(--fg);
    display: none;
  }
  .thumb.loading .spin {
    display: block;
  }
  .thumb.loading img {
    opacity: 0.4;
  }
  .export.busy {
    background: rgba(244, 247, 251, 0.85);
    font-variant-numeric: tabular-nums;
    min-width: 5.2rem;
    justify-content: center;
  }
  .pic {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    pointer-events: none;
  }
  .stage.cropping {
    touch-action: auto;
    background: #05070d;
  }
  .crop-wrap {
    position: absolute;
    inset: 0;
    padding: 0.75rem;
    overflow: auto;
    container-type: size;
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  .empty {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    color: var(--fg-mute);
  }
  .divider {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: rgba(255, 255, 255, 0.85);
    pointer-events: none;
  }
  .knob {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 34px;
    height: 48px;
    border-radius: 999px;
    background: rgba(8, 12, 24, 0.85);
    border: 1px solid rgba(255, 255, 255, 0.5);
    display: grid;
    place-items: center;
    color: #fff;
  }
  .tag {
    position: absolute;
    bottom: 0.7rem;
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    background: rgba(8, 12, 24, 0.75);
    border: 1px solid rgba(255, 255, 255, 0.15);
    font-size: 0.8rem;
    pointer-events: none;
  }
  .tag.l { left: 0.7rem; }
  .tag.r { right: 0.7rem; }
  .overlay {
    position: absolute;
    top: 0.6rem;
    left: 0.6rem;
    right: 0.6rem;
    display: flex;
    gap: 0.4rem;
    pointer-events: none;
  }
  .pill {
    pointer-events: auto;
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.3rem 0.7rem;
    min-height: 0;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    background: rgba(8, 12, 24, 0.72);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    color: var(--fg);
    font: inherit;
    font-size: 0.78rem;
    cursor: pointer;
  }
  .pill.on {
    background: #f4f7fb;
    color: #08101f;
  }
  .pill.round {
    width: 34px;
    height: 34px;
    padding: 0;
    justify-content: center;
  }
  .pill:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .undo-pills {
    display: inline-flex;
    gap: 0.35rem;
    margin-left: auto;
  }
  .pill.quiet + .undo-pills {
    margin-left: 0;
  }
  .pill.quiet {
    margin-left: auto;
    cursor: default;
    color: var(--fg-mute);
    pointer-events: none;
  }
  .spin {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 2px solid currentColor;
    border-top-color: transparent;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- tool panel */
  .panel {
    grid-area: panel;
    margin: 0.6rem 0 0;
    padding: 0.4rem 0.85rem;
    border-radius: 16px;
    background: var(--ed-surface);
    border: 1px solid var(--border);
    max-height: 42dvh;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .controls {
    display: grid;
    grid-template-columns: 1fr;
  }
  .controls > :global(*:not(:last-child)) {
    border-bottom: 1px solid rgba(233, 241, 255, 0.05);
  }
  h3 {
    margin: 0.45rem 0 0.45rem;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--fg-mute);
  }
  .cards {
    display: flex;
    gap: 0.6rem;
    overflow-x: auto;
    padding-bottom: 0.5rem;
    scroll-snap-type: x proximity;
    scrollbar-width: none;
  }
  .card {
    appearance: none;
    flex: none;
    width: 122px;
    padding: 0.35rem;
    min-height: 0;
    border-radius: 14px;
    border: 1px solid rgba(233, 241, 255, 0.08);
    background: rgba(255, 255, 255, 0.02);
    color: var(--fg);
    font: inherit;
    text-align: left;
    display: flex;
    flex-direction: column;
    cursor: pointer;
    scroll-snap-align: start;
  }
  .card.on {
    border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent);
  }
  .thumb {
    display: block;
    aspect-ratio: 4 / 3;
    border-radius: 10px;
    overflow: hidden;
    background: #05070d;
    margin-bottom: 0.4rem;
  }
  .thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .cn {
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0 0.2rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .cs {
    font-size: 0.72rem;
    color: var(--fg-mute);
    padding: 0 0.2rem 0.15rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .note {
    font-size: 0.85rem;
    line-height: 1.45;
    margin: 0.5rem 0.2rem;
  }
  .muted {
    color: var(--fg-mute);
  }
  .crop-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    margin: 0.4rem 0.2rem;
    font-size: 0.85rem;
  }
  .ghost {
    appearance: none;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.4rem 0.85rem;
    min-height: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: none;
    color: var(--fg);
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
    flex: none;
  }
  .ghost:disabled {
    opacity: 0.4;
    cursor: default;
  }
  .versions {
    margin: 0.2rem 0;
  }

  /* ---- tool tabs */
  .tools {
    grid-area: tools;
    display: flex;
    gap: 0.2rem;
    margin: 0.6rem 0 0;
    padding: 0.3rem;
    border-radius: 20px;
    background: var(--ed-surface);
    border: 1px solid var(--border);
  }
  .tools button {
    appearance: none;
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.2rem;
    padding: 0.5rem 0.1rem 0.4rem;
    min-height: 0;
    border: 0;
    border-radius: 15px;
    background: none;
    color: var(--fg-mute);
    font: inherit;
    font-size: 0.7rem;
    font-weight: 500;
    cursor: pointer;
  }
  .tools button.on {
    background: #f4f7fb;
    color: #08101f;
  }

  .wide-only { display: none; }
  /* Phones: tighter chrome. */
  @media (max-width: 819px) {
    .export { padding: 0 0.8rem; }
    .t { font-size: 1.25rem; }
    .views { margin: 0.35rem 0 0.5rem; }
    .canon { display: none; }
    .s { display: none; }
  }
  @media (max-width: 380px) {
    .export :global(svg) { display: none; }
    .tools button span { font-size: 0.62rem; }
  }

  /* Tablet landscape and desktop: picture left, tools in a column right. */
  @media (min-width: 820px) and (min-aspect-ratio: 1/1), (min-width: 1100px) {
    .editor {
      grid-template-columns: minmax(0, 1fr) clamp(320px, 30vw, 400px);
      grid-template-rows: auto auto minmax(0, 1fr) auto;
      grid-template-areas: 'top top' 'views views' 'stage panel' 'stage tools';
    }
    .wide-only { display: inline-flex; }
    .narrow-only { display: none; }
    .panel {
      margin: 0 0 0 1rem;
      max-height: none;
      align-self: stretch;
    }
    .tools {
      margin: 0.75rem 0 0 1rem;
    }
    .cards {
      flex-wrap: wrap;
      overflow: visible;
    }
    .card {
      width: calc(50% - 0.3rem);
    }
  }
  /* Tablet portrait: more room for the panel, two columns of controls. */
  @media (min-width: 600px) and (max-aspect-ratio: 1/1) {
    .panel { max-height: 36dvh; }
    .controls { grid-template-columns: 1fr 1fr; column-gap: 1.4rem; }
    .controls > :global(*:not(:last-child)) { border-bottom: 0; }
  }
  @media (hover: hover) {
    .tools button:not(.on):hover,
    .card:hover { background: rgba(255, 255, 255, 0.05); }
  }
</style>
