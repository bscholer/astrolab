<!--
  Projects list — full-width rows. See ui/design.md for the layout.

  Per-row affordances on the right rail:
    - Storage size (owned + shared sub-line if any)
    - Broom — drops the heavy mid-pipeline cache while keeping the
      saved final image so the UI still has a thumbnail.
    - Trash — deletes the project AND its owned cache entries. Shared
      entries (used by other projects) stay on disk.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type Project, type ProjectCapture, type StorageSnapshot, type SystemActiveJob } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import {
    failPctClass,
    formatBytes,
    formatFailPct,
    formatIntegrationTime,
    shortAgo,
    templateDisplayName
  } from '$lib/format';

  function shortDate(iso: string | null): string {
    return iso ? iso.slice(0, 10) : '';
  }

  /** Format the date span of the project's source sessions: a single
   * date when start/end fall on the same day, otherwise 'YYYY-MM-DD →
   * YYYY-MM-DD'. Empty string when nothing's known. */
  function captureDateRange(c: ProjectCapture | undefined): string {
    if (!c) return '';
    const s = shortDate(c.started_at);
    const e = shortDate(c.ended_at);
    if (!s && !e) return '';
    if (!e || s === e) return s || e;
    return `${s} → ${e}`;
  }

  /** Class hint for the Astro/Duo-Band/etc. filter chip. Narrowband
   * filters get the magenta treatment, broadband (Astro / UV/IR / L)
   * the cool steel-blue. Falls through to a neutral chip otherwise. */
  function filterClass(f: string | null | undefined): string {
    if (!f) return '';
    const k = f.toLowerCase();
    if (k.includes('duo') || k.includes('ha') || k.includes('oiii') || k.includes('sii') || k.includes('narrow'))
      return 'duoband';
    if (k.includes('astro') || k.includes('uv/ir') || k === 'l' || k.includes('lum') || k.includes('rgb') || k.includes('broad'))
      return 'astro';
    return '';
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
  <ul class="list">
    {#each projects as r, i (r.id)}
      {@const s = storageById.get(r.id)}
      {@const activeJob = activeByJobId.get(r.current_job_id)}
      <li class="prow" class:busy={busyId === r.id} style="--stagger: {i}">
        {#if r.preview_hash && r.preview_port}
          <a class="prow-thumb" href="/projects/{r.id}" aria-label="Open project">
            <img
              class="prow-thumb-img"
              src={api.previewUrl(r.preview_hash, r.preview_port)}
              alt=""
              loading="lazy"
              decoding="async"
            />
            <span class="prow-thumb-version">v{r.current_seq + 1}</span>
          </a>
        {:else}
          <!-- Reserve the same 84px slot so rows without a preview line
               up with rows that do have one. Title says why so future
               eyes don't think it's a bug. -->
          <div
            class="prow-thumb prow-thumb-empty"
            title="No saved render yet"
            aria-hidden="true"
          ></div>
        {/if}
        <a class="prow-link" href="/projects/{r.id}">
          <div class="prow-name">
            {#if r.capture?.target_common_name && r.capture.target_common_name !== r.name}
              {r.capture.target_common_name}
              <span class="prow-cat muted">{r.name}</span>
            {:else}
              {r.name}
            {/if}
            {#if !r.preview_hash}
              <span class="version-chip">v{r.current_seq + 1}</span>
            {/if}
          </div>
          <div class="prow-template">{templateDisplayName(r.template_id)}</div>
          {#if r.capture && r.capture.frame_count > 0}
            <div class="prow-meta muted small">
              <span class="meta-strong num">{r.capture.frame_count.toLocaleString()} frames</span>
              <span class="dot" aria-hidden="true">·</span>
              <span class="fail-pct {failPctClass(r.capture.failed_count, r.capture.frame_count)}">
                {formatFailPct(r.capture.failed_count, r.capture.frame_count)}
              </span>
              {#if r.capture.session_count > 0}
                <span class="dot" aria-hidden="true">·</span>
                <span>{r.capture.session_count} session{r.capture.session_count === 1 ? '' : 's'}</span>
              {/if}
              {#if r.capture.exptime != null || r.capture.gain != null}
                <span class="dot" aria-hidden="true">·</span>
                <span class="num">
                  {#if r.capture.exptime != null}{r.capture.exptime}s{/if}{#if r.capture.exptime != null && r.capture.gain != null} · {/if}{#if r.capture.gain != null}gain {r.capture.gain}{/if}
                </span>
              {/if}
              {#if r.capture.integration_seconds && r.capture.integration_seconds > 0}
                <span class="dot" aria-hidden="true">·</span>
                <span class="num" title="Useful integration time across source sessions">
                  {formatIntegrationTime(r.capture.integration_seconds)} integ
                </span>
              {/if}
              {#if r.capture.bytes_on_disk > 0}
                <span class="dot" aria-hidden="true">·</span>
                <span class="num" title="Source-frame bytes on disk">
                  {formatBytes(r.capture.bytes_on_disk)}
                </span>
              {/if}
              {#if r.capture.filter}
                <span class="dot" aria-hidden="true">·</span>
                <span class="filter-pill {filterClass(r.capture.filter)}">{r.capture.filter}</span>
              {/if}
              {#if captureDateRange(r.capture)}
                <span class="dot" aria-hidden="true">·</span>
                <span class="num">{captureDateRange(r.capture)}</span>
              {/if}
            </div>
          {/if}
          <div class="prow-foot muted small">
            <span class="num" title={r.updated_at}>edited {shortAgo(r.updated_at)}</span>
          </div>
          {#if activeJob}
            <div class="prow-progress">
              <div class="progress" aria-label="progress">
                <div class="progress-fill" style="--pct: {(activeJob.progress * 100).toFixed(1)}%"></div>
              </div>
              <span class="prow-pct muted small mono">{(activeJob.progress * 100).toFixed(0)}%</span>
            </div>
          {/if}
        </a>
        <div class="prow-side">
          {#if s}
            <div class="prow-storage">
              <span class="storage-main num">{formatBytes(s.owned_bytes)}</span>
              {#if s.shared_bytes > 0}
                <span class="storage-sub muted num">+{formatBytes(s.shared_bytes)} shared</span>
              {/if}
            </div>
          {/if}
          <div class="prow-actions">
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

  .list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }

  /* Project row — see design.md "Project row layout".
     Stagger animation pulled from app.css. */
  .prow {
    display: flex;
    align-items: stretch;
    gap: 1rem;
    padding: 0.95rem 1.15rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 60ms + 80ms);
    transition: border-color 160ms ease, transform 160ms ease, opacity 160ms ease;
  }
  .prow:hover {
    border-color: var(--border-strong);
    transform: translateY(-1px);
  }
  .prow.busy {
    opacity: 0.55;
    pointer-events: none;
  }

  /* Left column: name + template + foot. */
  .prow-link {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    flex: 1;
    min-width: 0;
    color: inherit;
    text-decoration: none;
  }
  .prow-name {
    /* Project names are entities in their own right — serif per design.md. */
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.2rem;
    letter-spacing: -0.01em;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .prow-template {
    font-size: 0.88rem;
    color: var(--fg);
    opacity: 0.85;
    font-variant: small-caps;
    letter-spacing: 0.02em;
  }
  /* Mid-row meta: frames + % failed + sessions + capture details. The
     same dot/strong/filter pattern Library uses inside session rows. */
  .prow-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.4rem;
    font-size: 0.82rem;
  }
  .prow-meta .dot { opacity: 0.5; }
  .meta-strong {
    color: var(--fg);
    font-weight: 500;
  }
  .prow-foot {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    font-size: 0.78rem;
    opacity: 0.85;
  }

  /* Catalog id (M 33, NGC 7380) sits next to the friendly name in mono.
     Nudge the baseline because the serif name sits taller than mono. */
  .prow-cat {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    position: relative;
    top: -0.1em;
  }

  /* Failure percentage — semantic color, matches Library tokens. */
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

  /* Filter chip — narrowband / broadband at a glance. */
  .filter-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.7rem;
    letter-spacing: 0.02em;
    border: 1px solid currentColor;
    line-height: 1.4;
    color: var(--fg-mute);
  }
  .filter-pill.astro {
    color: #a3d8ff;
    background: rgba(163, 216, 255, 0.10);
  }
  .filter-pill.duoband {
    color: var(--bad);
    background: color-mix(in oklab, var(--bad) 12%, transparent);
  }

  /* Project thumbnail — real preview from the current job's output.
     When the job is still running or its cache was evicted we fall
     through to the inline version chip on the name (no placeholder,
     no fake gradient). */
  .prow-thumb {
    position: relative;
    width: 84px;
    height: 84px;
    flex-shrink: 0;
    border-radius: calc(var(--radius-card) - 2px);
    overflow: hidden;
    background: var(--bg-elev-2);
    box-shadow:
      0 0 0 1px var(--hairline),
      inset 0 0 18px rgba(0, 0, 0, 0.5);
    transition: transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1), filter 220ms ease;
  }
  .prow:hover .prow-thumb:not(.prow-thumb-empty) {
    transform: scale(1.04);
    filter: saturate(1.15) brightness(1.05);
  }
  /* Empty placeholder: reserves the 84px slot so name + meta align with
     rows that do have a preview. Subtle inset hairline so it reads as
     'preview slot, nothing here yet' rather than a missing element. */
  .prow-thumb-empty {
    background: transparent;
    box-shadow: inset 0 0 0 1px var(--hairline);
    cursor: default;
  }
  .prow-thumb-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .prow-thumb-version {
    position: absolute;
    bottom: 4px;
    right: 5px;
    font-family: var(--font-mono);
    font-size: 0.62rem;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.92);
    background: rgba(0, 0, 0, 0.55);
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
  }

  /* Inline version chip — hugs the project name. */
  .version-chip {
    padding: 0.05rem 0.45rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 500;
    color: var(--accent);
    background: var(--accent-soft);
    border: 1px solid var(--border);
    border-radius: 999px;
    line-height: 1.4;
    /* Mono baseline sits low under serif; nudge up to align. */
    position: relative;
    top: -0.15em;
  }

  /* Right rail — storage on top, action icons under it, hairline left
     edge. Storage lives next to the actions because the actions act on
     it ("how much will I free?"). */
  .prow-side {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    justify-content: space-between;
    gap: 0.6rem;
    flex-shrink: 0;
    padding-left: 0.85rem;
    border-left: 1px solid var(--hairline);
  }
  .prow-storage {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.05rem;
    line-height: 1.15;
    text-align: right;
  }
  .storage-main {
    font-weight: 500;
    font-size: 0.92rem;
    color: var(--fg);
  }
  .storage-sub {
    font-size: 0.7rem;
  }

  .prow-actions {
    display: flex;
    gap: 0.4rem;
  }
  .icon-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    width: 28px;
    height: 28px;
    padding: 0;
    border-radius: 6px;
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
    transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .icon-btn:hover:not(:disabled) svg { transform: rotate(-6deg); }
  .icon-btn.danger:hover:not(:disabled) svg { transform: translateY(-1px); }

  .prow-progress {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.15rem;
  }
  .progress {
    flex: 1;
    height: 4px;
    background: rgba(94, 234, 212, 0.08);
    border-radius: 999px;
    overflow: hidden;
  }
  .progress-fill {
    width: var(--pct);
    height: 100%;
    background: linear-gradient(90deg, var(--accent), color-mix(in oklab, var(--accent) 60%, var(--bad)));
    transition: width 600ms ease-out;
  }
  .prow-pct {
    flex-shrink: 0;
    min-width: 2.6rem;
    text-align: right;
  }
</style>
