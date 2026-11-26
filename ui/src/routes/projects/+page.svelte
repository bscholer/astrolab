<!--
  Projects list: a grid of picture-first cards. See ui/design.md.

  Each card: the current render with the name over it, one line of facts
  (integration, sessions, filter), and a footer with the cache size and
  its two actions:
    - Broom: drops the heavy mid-pipeline cache while keeping the saved
      final image so the UI still has a picture.
    - Trash: deletes the project AND its owned cache entries. Shared
      entries (used by other projects) stay on disk.
  Frame counts, exposure, gain and the like live on the project page.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type Project, type ProjectCapture, type StorageSnapshot, type SystemActiveJob } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import {
    filterBadgeClass,
    formatBytes,
    formatDateSpan,
    formatFailPct,
    formatIntegrationTime,
    shortAgo,
    templateDisplayName,
    templateShortName
  } from '$lib/format';

  /** The details the card leaves out, for its tooltip. */
  function captureDetail(c: ProjectCapture): string {
    const bits = [`${c.frame_count.toLocaleString()} frames`];
    const fail = formatFailPct(c.failed_count, c.frame_count);
    if (fail) bits.push(fail);
    if (c.exptime != null) bits.push(`${c.exptime}s`);
    if (c.gain != null) bits.push(`gain ${c.gain}`);
    if (c.bytes_on_disk > 0) bits.push(`${formatBytes(c.bytes_on_disk)} of frames`);
    return bits.join(' · ');
  }

  let projects = $state<Project[] | null>(null);
  let storage = $state<StorageSnapshot | null>(null);
  let busyId = $state<string | null>(null);
  let activeJobs = $state<SystemActiveJob[]>([]);

  async function loadAll() {
    try {
      [projects, storage] = await Promise.all([
        api.listProjects(),
        api.getStorage(),
      ]);
    } catch (e) {
      toast.error(`Couldn't load projects: ${(e as Error).message}`);
    }
  }

  async function pollSystem() {
    try {
      const snap = await api.getSystem();
      activeJobs = snap.jobs.active;
    } catch {
      // ignore: poll runs every 1.5s, no need to surface transient failures
    }
  }

  let pollHandle: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    pollSystem();
    pollHandle = setInterval(pollSystem, 1500);
  });
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
  });

  $effect(() => {
    loadAll();
  });

  const activeByJobId = $derived(new Map(activeJobs.map((j) => [j.id, j])));

  // Map project id -> storage row so each render row can pull its own
  // owned/shared bytes without scanning per render.
  const storageById = $derived.by(() => {
    if (!storage) return new Map<string, StorageSnapshot['per_project'][number]>();
    return new Map(storage.per_project.map((p) => [p.project_id, p]));
  });

  async function freeIntermediates(r: Project) {
    const ok = confirm(
      `Free intermediates for "${r.name}"? The saved final image stays; ` +
        'cached upstream stages will need to be recomputed if you tweak.'
    );
    if (!ok) return;
    busyId = r.id;
    const pendingId = toast.info(`Sweeping "${r.name}"…`, null);
    try {
      const result = await api.purgeProjectCache(r.id, true);
      toast.dismiss(pendingId);
      toast.success(
        `Freed ${formatBytes(result.bytes_freed)} (${result.evicted_count} entries)`
      );
      await loadAll();
    } catch (e) {
      toast.dismiss(pendingId);
      toast.error(`Couldn't free intermediates: ${(e as Error).message}`);
    } finally {
      busyId = null;
    }
  }

  async function deleteProject(r: Project) {
    const owned = storageById.get(r.id)?.owned_bytes ?? 0;
    const ok = confirm(
      `Delete "${r.name}"? This drops the project and frees ` +
        `${formatBytes(owned)} of cache it owns. Shared cache stays.`
    );
    if (!ok) return;
    busyId = r.id;
    const pendingId = toast.info(`Deleting "${r.name}"…`, null);
    try {
      const result = await api.deleteProject(r.id);
      toast.dismiss(pendingId);
      toast.success(
        `Deleted "${r.name}" — freed ${formatBytes(result.bytes_freed)}`
      );
      await loadAll();
    } catch (e) {
      toast.dismiss(pendingId);
      toast.error(`Couldn't delete: ${(e as Error).message}`);
    } finally {
      busyId = null;
    }
  }
</script>

<div class="header">
  <h1>Projects</h1>
  {#if storage}
    <a href="/settings" class="storage-pill" title="Open storage settings">
      cache <span class="num">{formatBytes(storage.total_bytes)}</span>
      {#if storage.unreachable_bytes > 0}
        <span class="muted">· {formatBytes(storage.unreachable_bytes)} dead</span>
      {/if}
    </a>
  {/if}
</div>

{#if projects === null}
  <p class="muted">Loading…</p>
{:else if projects.length === 0}
  <p class="muted">
    No projects yet. Click "Run…" on a session in the
    <a href="/" class="link">Library</a> to start one.
  </p>
{:else}
  <ul class="grid">
    {#each projects as r, i (r.id)}
      {@const s = storageById.get(r.id)}
      {@const activeJob = activeByJobId.get(r.current_job_id)}
      {@const c = r.capture}
      {@const common = c?.target_common_name && c.target_common_name !== r.name ? c.target_common_name : null}
      <li class="card" class:busy={busyId === r.id} style="--stagger: {i}">
        <a class="pic" href="/projects/{r.id}" aria-label="Open {common ?? r.name}">
          {#if r.preview_hash && r.preview_port}
            <img
              src={api.previewUrl(r.preview_hash, r.preview_port)}
              alt=""
              loading="lazy"
              decoding="async"
            />
          {:else}
            <span class="no-pic muted small">{activeJob ? 'Rendering…' : 'No render yet'}</span>
          {/if}
          <span class="version">v{r.current_seq + 1}</span>
          <span class="title">
            <span class="name">{common ?? r.name}</span>
            {#if common}<span class="cat">{r.name}</span>{/if}
          </span>
          {#if activeJob}
            <span class="progress" role="progressbar" aria-valuenow={Math.round(activeJob.progress * 100)} aria-valuemin="0" aria-valuemax="100" aria-label="Processing">
              <span class="progress-fill" style="--pct: {(activeJob.progress * 100).toFixed(1)}%"></span>
            </span>
          {/if}
        </a>

        <div class="facts" title={c && c.frame_count > 0 ? captureDetail(c) : undefined}>
          {#if c?.integration_seconds && c.integration_seconds > 0}
            <span class="strong num">{formatIntegrationTime(c.integration_seconds)}</span>
          {/if}
          {#if c && c.session_count > 0}
            <span>{c.session_count} session{c.session_count === 1 ? '' : 's'}</span>
          {/if}
          {#if c && formatDateSpan(c.started_at, c.ended_at)}
            <span>{formatDateSpan(c.started_at, c.ended_at)}</span>
          {/if}
          {#if c?.filter}
            <span class={filterBadgeClass(c.filter)}>{c.filter}</span>
          {/if}
        </div>
        <div class="recipe muted" title={templateDisplayName(r.template_id)}>
          {templateShortName(r.template_id)} · edited <span title={r.updated_at}>{shortAgo(r.updated_at)}</span>
        </div>

        <div class="foot">
          {#if s}
            <span class="size num" title="Cache this project owns{s.shared_bytes > 0 ? `, plus ${formatBytes(s.shared_bytes)} shared with other projects` : ''}">
              {formatBytes(s.owned_bytes)}
              {#if s.shared_bytes > 0}<span class="muted">+{formatBytes(s.shared_bytes)}</span>{/if}
            </span>
          {/if}
          <div class="actions">
            <button
              type="button"
              class="icon-btn"
              onclick={() => freeIntermediates(r)}
              disabled={busyId !== null}
              aria-label="Free intermediates"
              title="Free intermediates — keep the saved image, drop cached stages"
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M20 3 L12 11" />
                <path d="M10 9 L14 13" />
                <path d="M10 9 L3 16" />
                <path d="M14 13 L8 21" />
                <path d="M3 16 L8 21" />
                <path d="M7 13 L5 18" />
                <path d="M10 16 L8 20" />
              </svg>
            </button>
            <button
              type="button"
              class="icon-btn danger"
              onclick={() => deleteProject(r)}
              disabled={busyId !== null}
              aria-label="Delete project"
              title="Delete project + its owned cache"
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M3 6h18" />
                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                <path d="M10 11v6" />
                <path d="M14 11v6" />
              </svg>
            </button>
          </div>
        </div>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin: 0.5rem 0 1.25rem;
    flex-wrap: wrap;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }
  .storage-pill {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: border-color 160ms ease;
  }
  .storage-pill:hover {
    border-color: var(--accent);
  }
  .link {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent-soft);
    text-underline-offset: 2px;
  }
  .link:hover { text-decoration-color: var(--accent); }

  .grid {
    list-style: none;
    padding: 0;
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
    gap: 1rem;
  }

  .card {
    display: flex;
    flex-direction: column;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
    overflow: hidden;
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 50ms + 60ms);
    transition: border-color 160ms ease, transform 160ms ease, opacity 160ms ease;
  }
  .card:hover {
    border-color: var(--border-strong);
    transform: translateY(-2px);
  }
  .card.busy {
    opacity: 0.55;
    pointer-events: none;
  }

  /* The picture, with the name laid over its lower edge. */
  .pic {
    position: relative;
    display: block;
    /* 4:3 like the Gallery: renders run from portrait to 2:1 wide. */
    aspect-ratio: 4 / 3;
    background: var(--bg-elev-2);
    overflow: hidden;
    color: inherit;
    text-decoration: none;
  }
  .pic img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 280ms cubic-bezier(0.2, 0.8, 0.2, 1), filter 280ms ease;
  }
  .card:hover .pic img {
    transform: scale(1.03);
    filter: saturate(1.1) brightness(1.05);
  }
  .no-pic {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding-bottom: 2.5rem;
    box-shadow: inset 0 0 0 1px var(--hairline);
  }
  .pic::after {
    /* Darken the bottom so the name reads on any picture. */
    content: '';
    position: absolute;
    inset: 45% 0 0 0;
    background: linear-gradient(180deg, transparent, rgba(2, 3, 10, 0.85));
    pointer-events: none;
  }
  .title {
    position: absolute;
    left: 0.95rem;
    right: 0.95rem;
    bottom: 0.7rem;
    z-index: 1;
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.15rem 0.55rem;
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.6);
  }
  .name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.3rem;
    line-height: 1.15;
    letter-spacing: -0.01em;
    color: #fff;
  }
  .cat {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: rgba(233, 241, 255, 0.75);
  }
  .version {
    position: absolute;
    top: 0.6rem;
    right: 0.7rem;
    z-index: 1;
    padding: 0.1rem 0.5rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.92);
    background: rgba(0, 0, 0, 0.55);
    border-radius: 999px;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }

  .facts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.3rem 0.9rem;
    padding: 0.8rem 0.95rem 0.15rem;
    font-size: 0.88rem;
    color: var(--fg-mute);
  }
  .facts:empty {
    display: none;
  }
  .strong {
    color: var(--fg);
    font-weight: 600;
  }
  .recipe {
    padding: 0.15rem 0.95rem 0;
    font-size: 0.8rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .foot {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-top: auto;
    padding: 0.7rem 0.95rem 0.8rem;
  }
  .size {
    font-size: 0.82rem;
    color: var(--fg);
  }
  .size .muted {
    margin-left: 0.25rem;
    font-size: 0.75rem;
  }
  .num {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .muted {
    color: var(--fg-mute);
  }
  .small {
    font-size: 0.85em;
  }

  .actions {
    display: flex;
    gap: 0.4rem;
    margin-left: auto;
  }
  .icon-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    width: 32px;
    height: 32px;
    min-height: 0;
    padding: 0;
    border-radius: 8px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: color 160ms ease, border-color 160ms ease;
  }
  .icon-btn:hover:not(:disabled) {
    color: var(--accent);
    border-color: var(--accent);
  }
  .icon-btn.danger:hover:not(:disabled) {
    color: var(--bad);
    border-color: var(--bad);
  }
  .icon-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .icon-btn svg {
    display: block;
  }

  /* Processing progress along the picture's bottom edge. */
  .progress {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 3px;
    z-index: 2;
    background: rgba(94, 234, 212, 0.12);
    overflow: hidden;
  }
  @keyframes bar-shimmer {
    0%   { transform: translateX(-100%); }
    100% { transform: translateX(400%); }
  }
  .progress-fill {
    display: block;
    width: var(--pct);
    height: 100%;
    background: linear-gradient(90deg, var(--accent), color-mix(in oklab, var(--accent) 60%, var(--bad)));
    transition: width 600ms ease-out;
    position: relative;
    overflow: hidden;
  }
  .progress-fill::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.28) 50%, transparent 100%);
    animation: bar-shimmer 2.4s linear infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .progress-fill::after { animation: none; }
    .card, .pic img { transition: none; }
  }
</style>
