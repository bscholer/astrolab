<!--
  Rendering list with storage management built in.

  Per-row affordances:
    - "Free intermediates" — drops the heavy mid-pipeline cache while
      keeping the saved final image so the UI still has a thumbnail.
    - Trash — deletes the rendering AND its owned cache entries. Shared
      entries (used by other projects) stay on disk.

  Header pill shows total cache + a "Run cleanup" link to /settings for
  the system-wide budget knobs.
-->
<script lang="ts">
  import { api, type Rendering, type StorageSnapshot } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { formatBytes, shortAgo } from '$lib/format';

  let renderings = $state<Rendering[] | null>(null);
  let storage = $state<StorageSnapshot | null>(null);
  let busyId = $state<string | null>(null);

  async function loadAll() {
    try {
      [renderings, storage] = await Promise.all([
        api.listRenderings(),
        api.getStorage(),
      ]);
    } catch (e) {
      toast.error(`Couldn't load renderings: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    loadAll();
  });

  // Map rendering id -> storage row so each render row can pull its own
  // owned/shared bytes without scanning per render.
  const storageById = $derived.by(() => {
    if (!storage) return new Map<string, StorageSnapshot['per_project'][number]>();
    return new Map(storage.per_project.map((p) => [p.rendering_id, p]));
  });

  async function freeIntermediates(r: Rendering) {
    const ok = confirm(
      `Free intermediates for "${r.name}"? The saved final image stays; ` +
        'cached upstream stages will need to be recomputed if you tweak.'
    );
    if (!ok) return;
    busyId = r.id;
    try {
      const result = await api.purgeRenderingCache(r.id, true);
      toast.success(
        `Freed ${formatBytes(result.bytes_freed)} (${result.evicted_count} entries)`
      );
      await loadAll();
    } catch (e) {
      toast.error(`Couldn't free intermediates: ${(e as Error).message}`);
    } finally {
      busyId = null;
    }
  }

  async function deleteRendering(r: Rendering) {
    const owned = storageById.get(r.id)?.owned_bytes ?? 0;
    const ok = confirm(
      `Delete "${r.name}"? This drops the project and frees ` +
        `${formatBytes(owned)} of cache it owns. Shared cache stays.`
    );
    if (!ok) return;
    busyId = r.id;
    try {
      const result = await api.deleteRendering(r.id);
      toast.success(
        `Deleted "${r.name}" — freed ${formatBytes(result.bytes_freed)}`
      );
      await loadAll();
    } catch (e) {
      toast.error(`Couldn't delete: ${(e as Error).message}`);
    } finally {
      busyId = null;
    }
  }
</script>

<div class="header">
  <a href="/" class="back">← library</a>
  <h1>Renderings</h1>
  {#if storage}
    <a href="/settings" class="storage-pill" title="Open storage settings">
      cache {formatBytes(storage.total_bytes)}
      {#if storage.unreachable_bytes > 0}
        <span class="muted">· {formatBytes(storage.unreachable_bytes)} dead</span>
      {/if}
    </a>
  {/if}
  <a href="/jobs" class="muted small debug">debug: jobs</a>
</div>

{#if renderings === null}
  <p class="muted">Loading…</p>
{:else if renderings.length === 0}
  <p class="muted">
    No renderings yet. Click "Run…" on a session in the library to start one.
  </p>
{:else}
  <ul class="list">
    {#each renderings as r (r.id)}
      {@const s = storageById.get(r.id)}
      <li class="row" class:busy={busyId === r.id}>
        <a class="row-link" href="/renderings/{r.id}">
          <div class="row-name">{r.name}</div>
          <div class="row-meta muted small">
            <span title={r.template_id}>{r.template_id}</span>
            <span aria-hidden="true">·</span>
            <span>v{r.current_seq + 1} of {r.history.length}</span>
            <span aria-hidden="true">·</span>
            <span title={r.updated_at}>{shortAgo(r.updated_at)}</span>
            {#if s}
              <span aria-hidden="true">·</span>
              <span class="storage" title="Owned: cache only this project references. Shared: counted toward other projects too.">
                {formatBytes(s.owned_bytes)} owned{#if s.shared_bytes > 0}, +{formatBytes(s.shared_bytes)} shared{/if}
              </span>
            {/if}
          </div>
        </a>
        <div class="row-actions">
          <button
            type="button"
            class="action-btn"
            onclick={() => freeIntermediates(r)}
            disabled={busyId !== null}
            title="Drop cached intermediate stages; keep the saved image"
          >free intermediates</button>
          <button
            type="button"
            class="action-btn danger"
            onclick={() => deleteRendering(r)}
            disabled={busyId !== null}
            title="Delete project + its owned cache"
          >🗑</button>
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
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }
  .back {
    color: var(--fg-mute, #888);
    text-decoration: none;
  }

  .storage-pill {
    text-decoration: none;
    background: var(--bg-elev, #14171d);
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    padding: 0.2rem 0.6rem;
    border-radius: 999px;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
    cursor: pointer;
  }
  .storage-pill:hover {
    border-color: var(--accent, #7aa2ff);
  }

  .debug {
    text-decoration: none;
  }
  .debug:hover {
    text-decoration: underline;
  }
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute, #888);
  }

  .list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    background: var(--bg-elev, #14171d);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    transition: opacity 120ms ease;
  }
  .row.busy {
    opacity: 0.55;
    pointer-events: none;
  }
  .row:hover {
    border-color: var(--accent, #7aa2ff);
  }
  .row-link {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    flex: 1;
    text-decoration: none;
    color: inherit;
    min-width: 0;
  }
  .row-name {
    font-weight: 600;
    font-size: 1rem;
  }
  .row-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
  .storage {
    font-variant-numeric: tabular-nums;
  }
  .row-actions {
    display: flex;
    gap: 0.4rem;
    flex-shrink: 0;
  }
  .action-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg-mute, #888);
    padding: 0.25rem 0.6rem;
    border-radius: 6px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .action-btn:hover:not(:disabled) {
    border-color: var(--accent, #7aa2ff);
    color: var(--accent, #7aa2ff);
  }
  .action-btn.danger:hover:not(:disabled) {
    border-color: var(--bad, #ff7a8a);
    color: var(--bad, #ff7a8a);
  }
  .action-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
</style>
