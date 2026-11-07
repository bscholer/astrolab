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
  const sched = $derived(scope?.scheduler ?? null);
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

<div class="top">
  <div class="shot">
    {#if previewFailed}
      <div class="empty">No stack yet</div>
    {:else}
      <img
        class="preview"
        src="/api/scope/preview?t={tick}"
        alt="Latest stack"
        onerror={() => (previewFailed = true)}
      />
    {/if}
  </div>

  <div class="stats">
    {#if fetchError}
      <p class="bad">Astrolab server unreachable: {fetchError}</p>
    {:else if !scope}
      <p class="muted">Connecting…</p>
    {:else if !scope.configured}
      <p class="muted">No scope service set. Add one in <a href="/settings">Settings</a>.</p>
    {:else if !scope.reachable}
      <p class="bad">Cannot reach service{scope.error ? `: ${scope.error}` : ''}</p>
    {:else}
      <dl class="kv">
        <dt>Scope</dt>
        <dd>{scope.status?.app_connected ? 'app attached' : 'free'}</dd>
        <dt>Pitch</dt>
        <dd>{fmtDeg(scope.status?.pitch)}</dd>
        <dt>Rotation</dt>
        <dd>{fmtDeg(scope.status?.rotation)}</dd>
        {#if job}
          <dt>Capture</dt>
          <dd>
            {job.state} · {job.captured ?? 0}{job.frames !== undefined ? `/${job.frames}` : ''}
            {#if job.failed}· {job.failed} failed{/if}
          </dd>
        {/if}
        {#if obs && obs.state !== 'idle'}
          <dt>Observe</dt>
          <dd>{obs.target ?? ''} · {obs.step ?? obs.state}</dd>
        {/if}
        {#if sched?.enabled}
          <dt>Auto</dt>
          <dd>{sched.state}{sched.target ? ` · ${sched.target}` : ''}</dd>
        {/if}
        {#if mirror}
          <dt>Mirror</dt>
          <dd>{mirror.state} · {mirror.files_copied} files{mirror.current ? ` · ${mirror.current}` : ''}</dd>
        {/if}
      </dl>
      {#each [job?.error, obs?.error, mirror?.error, sched?.reason && sched.state === 'error' ? sched.reason : null] as err}
        {#if err}<p class="bad small">{err}</p>{/if}
      {/each}
      {#if job?.state === 'running'}
        <button type="button" class="btn warn" onclick={stopCapture} disabled={stopping}>
          {stopping ? 'Stopping…' : 'Stop capture'}
        </button>
      {/if}
    {/if}
  </div>
</div>

<style>
  .top {
    display: grid;
    grid-template-columns: minmax(0, 360px) minmax(0, 1fr);
    gap: 1rem;
    align-items: start;
    margin-top: 0.5rem;
  }
  @media (max-width: 640px) {
    .top {
      grid-template-columns: minmax(0, 1fr);
    }
  }
  .shot,
  .stats {
    padding: 0.75rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .shot {
    padding: 0.4rem;
  }
  .preview {
    display: block;
    width: 100%;
    max-height: 45vh;
    object-fit: contain;
    border-radius: var(--radius);
  }
  .empty {
    padding: 2rem 0;
    text-align: center;
    color: var(--fg-mute);
  }
  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.3rem 0.9rem;
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
  .muted {
    color: var(--fg-mute);
    margin: 0;
  }
  .small {
    font-size: 0.85em;
  }
  .bad {
    color: var(--bad);
    margin: 0.5rem 0 0;
  }
  .btn {
    margin-top: 0.75rem;
    min-height: 44px;
    width: 100%;
  }
</style>
