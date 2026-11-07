<!--
  Scope: phone-friendly live view of the scope service. Polls status every
  5 s and refreshes the stack preview every 30 s.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type ScopeState } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  let scope = $state<ScopeState | null>(null);
  let fetchError = $state<string | null>(null);
  let stopping = $state(false);
  let tick = $state(Date.now());
  let previewFailed = $state(false);

  const job = $derived(scope?.status?.job ?? null);
  const obs = $derived(scope?.observe ?? null);
  const mirror = $derived(scope?.mirror ?? null);
  const darks = $derived(
    scope?.status?.tele_darks !== undefined || scope?.status?.wide_darks !== undefined,
  );

  async function refresh() {
    try {
      scope = await api.getScope();
      fetchError = null;
    } catch (e) {
      fetchError = e instanceof Error ? e.message : String(e);
    }
  }

  function bumpPreview() {
    tick = Date.now();
    previewFailed = false;
  }

  async function stopCapture() {
    stopping = true;
    try {
      await api.stopScope();
      toast.success('Stop sent.');
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      stopping = false;
    }
  }

  function fmtDeg(v: number | null | undefined): string {
    return typeof v === 'number' ? `${v.toFixed(1)}°` : '—';
  }

  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let previewTimer: ReturnType<typeof setInterval> | undefined;

  onMount(() => {
    refresh();
    pollTimer = setInterval(refresh, 5000);
    previewTimer = setInterval(bumpPreview, 30000);
  });

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(previewTimer);
  });
</script>

<div class="header">
  <h1>Scope</h1>
</div>

<section class="panel">
  <h2>Connection</h2>
  {#if fetchError}
    <p class="bad">Astrolab server unreachable: {fetchError}</p>
  {:else if !scope}
    <p class="muted">Connecting…</p>
  {:else if !scope.configured}
    <p class="muted">No scope service set. Add one in <a href="/settings">Settings</a>.</p>
  {:else if scope.reachable}
    <p class="ok">
      Reachable · {scope.service?.name ?? 'service'} · Scope
      {scope.status?.app_connected ? 'online' : 'offline'}
    </p>
  {:else}
    <p class="bad">Cannot reach service{scope.error ? `: ${scope.error}` : ''}</p>
  {/if}
</section>

{#if scope?.reachable}
  <section class="panel">
    <h2>Telescope</h2>
    <dl class="kv">
      <dt>Pitch</dt>
      <dd>{fmtDeg(scope.status?.pitch)}</dd>
      <dt>Rotation</dt>
      <dd>{fmtDeg(scope.status?.rotation)}</dd>
      <dt>App</dt>
      <dd>{scope.status?.app_connected ? 'connected' : 'disconnected'}</dd>
      {#if darks}
        <dt>Darks</dt>
        <dd>
          tele {scope.status?.tele_darks ?? 0} · wide {scope.status?.wide_darks ?? 0}
        </dd>
      {/if}
    </dl>
  </section>

  <section class="panel">
    <h2>Capture</h2>
    {#if job}
      <dl class="kv">
        <dt>State</dt>
        <dd>{job.state}</dd>
        <dt>Frames</dt>
        <dd>{job.captured ?? 0}{job.frames !== undefined ? ` of ${job.frames}` : ''}</dd>
        <dt>Failed</dt>
        <dd>{job.failed ?? 0}</dd>
      </dl>
      {#if job.error}<p class="bad small">{job.error}</p>{/if}
      {#if job.state === 'running'}
        <button type="button" class="btn warn full" onclick={stopCapture} disabled={stopping}>
          {stopping ? 'Stopping…' : 'Stop'}
        </button>
      {/if}
    {:else}
      <p class="muted">No capture job.</p>
    {/if}
  </section>

  <section class="panel">
    <h2>Observe</h2>
    {#if obs}
      <dl class="kv">
        <dt>State</dt>
        <dd>{obs.state}</dd>
        {#if obs.target}<dt>Target</dt><dd>{obs.target}</dd>{/if}
        {#if obs.step}<dt>Step</dt><dd>{obs.step}</dd>{/if}
      </dl>
      {#if obs.error}<p class="bad small">{obs.error}</p>{/if}
    {:else}
      <p class="muted">Idle.</p>
    {/if}
  </section>

  <section class="panel">
    <h2>Mirror</h2>
    {#if mirror}
      <dl class="kv">
        <dt>State</dt>
        <dd>{mirror.state}</dd>
        <dt>Files copied</dt>
        <dd>{mirror.files_copied}</dd>
        {#if mirror.current}<dt>Current</dt><dd class="wrap">{mirror.current}</dd>{/if}
      </dl>
      {#if mirror.error}<p class="bad small">{mirror.error}</p>{/if}
    {:else}
      <p class="muted">Not enabled.</p>
    {/if}
  </section>
{/if}

<section class="panel">
  <h2>Stack preview</h2>
  {#if previewFailed}
    <p class="muted">No stack yet</p>
  {:else}
    <img
      class="preview"
      src="/api/scope/preview?t={tick}"
      alt="Latest stack"
      onerror={() => (previewFailed = true)}
    />
  {/if}
</section>

<style>
  .header {
    margin: 0.5rem 0 1.25rem;
  }
  .header h1 {
    margin: 0;
    font-size: 1.5rem;
  }
  h2 {
    margin: 0 0 0.6rem;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
  }
  .panel {
    margin-bottom: 1rem;
    padding: 1rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    margin: 0;
    font-family: var(--font-mono);
    font-size: 0.88rem;
    font-variant-numeric: tabular-nums;
  }
  .kv dt {
    color: var(--fg-mute);
  }
  .kv dd {
    margin: 0;
    min-width: 0;
  }
  .wrap {
    overflow-wrap: anywhere;
  }
  .muted {
    color: var(--fg-mute);
  }
  .small {
    font-size: 0.85em;
  }
  .ok {
    color: var(--accent);
    margin: 0;
  }
  .bad {
    color: var(--bad);
    margin: 0.5rem 0 0;
  }
  .full {
    width: 100%;
    margin-top: 0.75rem;
    min-height: 44px;
  }
  .preview {
    display: block;
    max-width: 100%;
    height: auto;
    border-radius: var(--radius);
  }
</style>
