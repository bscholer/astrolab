<!--
  System-wide settings: cache budget + on-demand cleanup.

  Slider sets cache_max_bytes (binary GiB). The "Run cleanup" button
  triggers POST /api/storage/cleanup which evicts entries until the cache
  fits under the budget. Eviction order:
    1. Unreachable entries (deleted projects, half-runs) always go first.
    2. Reachable entries by ascending score (cost × recency); cheap
       outputs from old projects evict before expensive recent stuff.
-->
<script lang="ts">
  import { api, type StorageSnapshot } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { formatBytes } from '$lib/format';

  const GIB = 1024 * 1024 * 1024;

  let storage = $state<StorageSnapshot | null>(null);
  let cacheMaxBytes = $state<number>(200 * GIB);
  let saving = $state(false);
  let cleaning = $state(false);

  async function load() {
    try {
      const [snap, settings] = await Promise.all([
        api.getStorage(),
        api.getSettings(),
      ]);
      storage = snap;
      cacheMaxBytes = settings.cache_max_bytes;
    } catch (e) {
      toast.error(`Couldn't load settings: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    load();
  });

  // Slider operates on GiB to give us friendly round-number values; we
  // convert back to bytes on save. Range goes from a 1 GiB floor (the
  // server-side minimum) up to whatever the cache currently holds + 200
  // GiB headroom, so users can pick 'a bit larger than current'.
  const sliderMaxGiB = $derived.by(() => {
    const current = (storage?.total_bytes ?? 0) / GIB;
    return Math.max(50, Math.ceil(current) + 200);
  });
  const cacheMaxGiB = $derived(cacheMaxBytes / GIB);

  function setSliderGiB(g: number) {
    cacheMaxBytes = Math.round(g * GIB);
  }

  async function save() {
    saving = true;
    try {
      const r = await api.patchSettings({ cache_max_bytes: cacheMaxBytes });
      cacheMaxBytes = r.cache_max_bytes;
      toast.success(`Budget set to ${formatBytes(cacheMaxBytes)}`);
    } catch (e) {
      toast.error(`Couldn't save: ${(e as Error).message}`);
    } finally {
      saving = false;
    }
  }

  async function runCleanup() {
    cleaning = true;
    try {
      const r = await api.storageCleanup();
      const msg = r.evicted_count === 0
        ? 'Cache already fits under the budget — nothing to evict.'
        : `Evicted ${r.evicted_count} entries, freed ${formatBytes(r.bytes_freed)}.`;
      if (r.over_budget) {
        toast.error(
          `${msg} Still over budget — consider raising the cap or deleting projects.`
        );
      } else {
        toast.success(msg);
      }
      await load();
    } catch (e) {
      toast.error(`Cleanup failed: ${(e as Error).message}`);
    } finally {
      cleaning = false;
    }
  }
</script>

<div class="header">
  <a href="/projects" class="back">← projects</a>
  <h1>Settings</h1>
</div>

{#if storage === null}
  <p class="muted">Loading…</p>
{:else}
  <section class="panel">
    <h2>Storage</h2>
    <div class="stats">
      <div class="stat">
        <span class="stat-label">cache total</span>
        <span class="stat-val">{formatBytes(storage.total_bytes)}</span>
        <span class="stat-sub muted">{storage.entry_count} entries</span>
      </div>
      <div class="stat">
        <span class="stat-label">unreachable</span>
        <span class="stat-val">{formatBytes(storage.unreachable_bytes)}</span>
        <span class="stat-sub muted">{storage.unreachable_count} dead entries</span>
      </div>
      <div class="stat">
        <span class="stat-label">cache root</span>
        <code class="stat-path muted">{storage.cache_root}</code>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>Cache budget</h2>
    <p class="muted small">
      Eviction sweeps will trim the cache to this size. Order: dead
      entries first; reachable entries by ascending
      <em>cost × recency</em> score (cheap outputs from old projects
      first, expensive recent ones last).
    </p>
    <div class="slider-row">
      <input
        type="range"
        min={1}
        max={sliderMaxGiB}
        step={1}
        value={cacheMaxGiB}
        oninput={(e) => setSliderGiB(parseFloat((e.currentTarget as HTMLInputElement).value))}
      />
      <input
        type="number"
        class="num"
        min={1}
        max={sliderMaxGiB}
        step={1}
        value={cacheMaxGiB}
        oninput={(e) => setSliderGiB(parseFloat((e.currentTarget as HTMLInputElement).value))}
      />
      <span class="unit">GiB</span>
    </div>
    <div class="actions">
      <button type="button" class="btn primary" onclick={save} disabled={saving}>
        {saving ? 'Saving…' : 'Save budget'}
      </button>
      <button type="button" class="btn" onclick={runCleanup} disabled={cleaning}>
        {cleaning ? 'Cleaning…' : 'Run cleanup now'}
      </button>
    </div>
    {#if storage.total_bytes > cacheMaxBytes}
      <p class="warn">
        Current usage ({formatBytes(storage.total_bytes)}) exceeds this
        budget ({formatBytes(cacheMaxBytes)}). Click <strong>Run
        cleanup now</strong> to evict.
      </p>
    {/if}
  </section>
{/if}

<style>
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin-bottom: 1rem;
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
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute, #888);
  }

  .panel {
    margin-bottom: 1.5rem;
    padding: 0.85rem 1rem;
    background: var(--bg-elev, #14171d);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
  }
  .panel h2 {
    margin: 0 0 0.5rem;
    font-size: 1rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
  }

  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    margin-top: 0.5rem;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 8rem;
  }
  .stat-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-mute, #888);
  }
  .stat-val {
    font-size: 1.4rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .stat-sub {
    font-size: 0.75rem;
  }
  .stat-path {
    font-family: ui-monospace, monospace;
    font-size: 0.75rem;
    word-break: break-all;
  }

  .slider-row {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    margin: 0.6rem 0;
  }
  .slider-row input[type='range'] {
    flex: 1;
  }
  .slider-row .num {
    width: 5rem;
    padding: 0.25rem 0.4rem;
    background: var(--bg, #0a0c10);
    color: var(--fg, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    font: inherit;
  }
  .slider-row .unit {
    color: var(--fg-mute, #888);
    font-variant-numeric: tabular-nums;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }
  .btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    padding: 0.35rem 0.9rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .btn:hover:not(:disabled) {
    border-color: var(--accent, #7aa2ff);
    color: var(--accent, #7aa2ff);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn.primary {
    background: var(--accent, #7aa2ff);
    color: #0a0c10;
    border-color: var(--accent, #7aa2ff);
    font-weight: 600;
  }
  .btn.primary:hover:not(:disabled) {
    color: #0a0c10;
  }

  .warn {
    margin-top: 0.5rem;
    font-size: 0.85rem;
    color: var(--warn, #f0b35e);
  }
</style>
