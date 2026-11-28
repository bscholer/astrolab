<!--
  Storage on the NAS, by kind. The server walks the share in the background
  every few hours (a walk takes a minute or more over NFS); this shows the
  last walk and can start a new one. Data: GET /api/storage/nas
  (server/nas_usage.py).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type NasKindId, type NasUsageResponse } from '$lib/api';
  import { formatBytes, shortAgo } from '$lib/format';
  import StorageDonut from '$lib/StorageDonut.svelte';
  import { toast } from '$lib/toast.svelte';

  let data = $state<NasUsageResponse | null>(null);
  let failed = $state(false);
  let nowTick = $state(Date.now());

  // Fixed per kind, in slice order, so a kind keeps its color when others
  // come and go. Categorical steps for a dark surface; "Other" is neutral and
  // matches the local chart (LocalStorage), as do lights and images.
  const COLOR: Record<NasKindId, string> = {
    lights: '#3987e5',
    failed: '#d95926',
    scope_outputs: '#199e70',
    calibration: '#c98500',
    images: '#d55181',
    db: '#008300',
    other: '#c3c9d4',
  };

  const HELP: Record<NasKindId, string> = {
    lights: 'Light frames in scope session folders',
    failed: 'failed_* frames the scope could not register stars in',
    scope_outputs: "The scope's own stacks, previews and thumbnails",
    calibration: 'Darks and factory calibration frames',
    images: 'Finished pictures and FITS that astrolab backed up',
    db: 'Daily database snapshots',
    other: 'Everything else on the share',
  };

  async function load() {
    try {
      data = await api.getNasUsage();
      failed = false;
    } catch {
      failed = true;
    }
  }

  async function refresh() {
    try {
      const r = await api.refreshNasUsage();
      data = r;
      if (!r.started && !r.measuring) toast.error('Could not start a NAS walk');
    } catch (e) {
      toast.error(`Could not start a NAS walk: ${(e as Error).message}`);
    }
  }

  onMount(() => {
    load();
    const tick = setInterval(() => (nowTick = Date.now()), 30_000);
    return () => clearInterval(tick);
  });
  // Poll quickly while a walk runs, so the chart fills in when it ends.
  $effect(() => {
    if (!data?.measuring) return;
    const h = setInterval(load, 3000);
    return () => clearInterval(h);
  });

  const measuredLabel = $derived.by(() => {
    void nowTick;
    return data?.last ? shortAgo(data.last.measured_at) : null;
  });
</script>

<section class="nas" aria-label="NAS storage">
  {#if failed && !data}
    <p class="muted">Couldn't load NAS storage.</p>
  {:else if !data}
    <p class="muted">Loading…</p>
  {:else if !data.configured}
    <p class="muted">
      No NAS is set up. Set a NAS folder in <a href="/settings" class="link">Settings</a> and astrolab
      will keep the reference copy of captures, finished images and the database there, and show what
      takes up the space here.
    </p>
  {:else if !data.last}
    <p class="muted">
      {#if data.measuring}
        <span class="spinner" aria-hidden="true"></span> Measuring the NAS. A large share takes a minute or two.
      {:else}
        The NAS hasn't been measured yet.
        <button type="button" class="link-btn" onclick={refresh}>Measure now</button>
      {/if}
    </p>
  {:else}
    {@const last = data.last}
    <StorageDonut
      kinds={last.kinds}
      colors={COLOR}
      help={HELP}
      center="on the NAS"
      ariaLabel="NAS storage by kind"
    />

    <p class="foot muted small">
      <span title={last.root}>{last.root}</span>
      · {formatBytes(last.disk.free_bytes)} free of {formatBytes(last.disk.total_bytes)} on the share
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
  .nas {
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
  .link {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent-soft);
    text-underline-offset: 2px;
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
