<!--
  Storage on the disk the cache lives on, by kind: raw captures, kept results,
  the pipeline cache, everything else on the disk, and free space (the part
  jobs never use, hatched, and the rest). The server walks it every half hour;
  this shows the last walk and can start a new one.
  Data: GET /api/storage/local (server/local_usage.py).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type LocalKindId, type LocalUsageResponse } from '$lib/api';
  import { formatBytes, shortAgo } from '$lib/format';
  import StorageDonut from '$lib/StorageDonut.svelte';
  import { toast } from '$lib/toast.svelte';

  let data = $state<LocalUsageResponse | null>(null);
  let failed = $state(false);
  let nowTick = $state(Date.now());

  // Ring order: captures, results, cache, other, reserved, free. Captures,
  // results and other share their NAS-chart colors (lights, images, other).
  // The two free-space kinds are dark neutrals, told apart by the hatch.
  const COLOR: Record<LocalKindId, string> = {
    captures: '#3987e5',
    results: '#d55181',
    cache: '#9085e9',
    other: '#c3c9d4',
    reserved: '#2b3550',
    free: '#2b3550',
  };

  const HELP: Record<LocalKindId, string> = {
    captures: 'Raw frames, darks and calibration. With a NAS, old nights move there when space runs short',
    results: "Every version's picture, and the stacked and final FITS of each project's first, latest and cover versions. Never cleared",
    cache: 'Pipeline steps astrolab can rebuild; cleared first when space runs short',
    other: 'Everything else on this disk: container images, other apps',
    reserved: 'Free space jobs never use, so the node keeps running',
    free: 'Free space jobs can use',
  };

  async function load() {
    try {
      data = await api.getLocalUsage();
      failed = false;
    } catch {
      failed = true;
    }
  }

  async function refresh() {
    try {
      data = await api.refreshLocalUsage();
    } catch (e) {
      toast.error(`Could not measure the disk: ${(e as Error).message}`);
    }
  }

  onMount(() => {
    load();
    const tick = setInterval(() => (nowTick = Date.now()), 30_000);
    return () => clearInterval(tick);
  });
  $effect(() => {
    if (!data?.measuring) return;
    const h = setInterval(load, 1500);
    return () => clearInterval(h);
  });

  const measuredLabel = $derived.by(() => {
    void nowTick;
    return data?.last ? shortAgo(data.last.measured_at) : null;
  });
</script>

<section class="local" aria-label="Local storage">
  {#if failed && !data}
    <p class="muted">Couldn't load local storage.</p>
  {:else if !data}
    <p class="muted">Loading…</p>
  {:else if !data.last}
    <p class="muted">
      {#if data.measuring}
        <span class="spinner" aria-hidden="true"></span> Measuring the disk…
      {:else}
        The disk hasn't been measured yet.
        <button type="button" class="link-btn" onclick={refresh}>Measure now</button>
      {/if}
    </p>
  {:else}
    {@const last = data.last}
    <StorageDonut
      kinds={last.kinds}
      colors={COLOR}
      help={HELP}
      hatched={['reserved']}
      center="on this disk"
      ariaLabel="Local storage by kind"
    />

    <p class="foot muted small">
      <span title={last.cache_root}>{last.cache_root}</span>
      · {formatBytes(last.disk.free_bytes)} free of {formatBytes(last.disk.total_bytes)},
      {formatBytes(last.floor_bytes)} of it kept free for the node
      {#if last.captures_root && !last.captures_on_disk}
        · captures (<span title={last.captures_root}>{last.captures_root}</span>) are on another disk
      {/if}
      · measured <span title={last.measured_at}>{measuredLabel}</span>
      {#if data.measuring}
        · <span class="spinner" aria-hidden="true"></span> measuring again…
      {:else}
        · <button type="button" class="link-btn" onclick={refresh}>measure now</button>
      {/if}
      {#if data.error}<br /><span class="err">Last walk failed: {data.error}</span>{/if}
    </p>
  {/if}
</section>

<style>
  .local {
    margin: 0.5rem 0 1.5rem;
  }
  .foot {
    margin-top: 1rem;
    overflow-wrap: anywhere;
  }
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute);
  }
  .err {
    color: var(--bad);
  }
  .link-btn {
    appearance: none;
    background: none;
    border: 0;
    padding: 0;
    min-height: 0;
    font: inherit;
    color: var(--accent);
    cursor: pointer;
    text-decoration: underline;
  }
  .spinner {
    display: inline-block;
    width: 10px;
    height: 10px;
    border: 2px solid var(--fg-mute);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
    vertical-align: -1px;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
