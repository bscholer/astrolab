<script lang="ts">
  import { api, type TargetSummary, type TargetDetail, type CalibrationStatus } from '$lib/api';

  let targets = $state<TargetSummary[] | null>(null);
  let error = $state<string | null>(null);
  let openTargetId = $state<number | null>(null);
  let openTarget = $state<TargetDetail | null>(null);
  let openLoading = $state(false);
  let scanning = $state(false);
  let scanRoot = $state('');
  let scanResult = $state<string | null>(null);

  async function load() {
    error = null;
    try {
      targets = await api.listTargets();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function openTarget_(id: number) {
    if (openTargetId === id) {
      openTargetId = null;
      openTarget = null;
      return;
    }
    openTargetId = id;
    openTarget = null;
    openLoading = true;
    try {
      openTarget = await api.getTarget(id);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      openLoading = false;
    }
  }

  async function rescan() {
    if (!scanRoot.trim()) return;
    scanning = true;
    scanResult = null;
    try {
      const r = await api.scan(scanRoot.trim(), 'dwarf3');
      scanResult = `+${r.inserted} frames, +${r.masters_inserted} masters, -${r.removed} orphans`;
      await load();
    } catch (e) {
      scanResult = `error: ${(e as Error).message}`;
    } finally {
      scanning = false;
    }
  }

  $effect(() => {
    load();
  });

  function shortDate(iso: string | null): string {
    if (!iso) return '';
    return iso.slice(0, 10);
  }

  function calBadgeClass(c: CalibrationStatus): string {
    return `cal cal-${c.quality}`;
  }

  function calLabel(kind: string): string {
    return kind[0].toUpperCase();
  }
</script>

<section class="scan-bar">
  <input
    type="text"
    placeholder="Capture root (e.g. ~/Pictures/Siril)"
    bind:value={scanRoot}
    autocomplete="off"
    spellcheck="false"
  />
  <button onclick={rescan} disabled={scanning || !scanRoot.trim()}>
    {scanning ? 'Scanning…' : 'Scan'}
  </button>
  {#if scanResult}
    <span class="muted scan-result">{scanResult}</span>
  {/if}
</section>

{#if error}
  <p class="error">{error}</p>
{/if}

{#if targets === null}
  <p class="muted">Loading targets…</p>
{:else if targets.length === 0}
  <p class="muted">
    No targets yet. Point the scan field at a Dwarf 3 capture root and hit Scan.
  </p>
{:else}
  <ul class="target-list">
    {#each targets as t (t.id)}
      <li class="target" class:open={openTargetId === t.id}>
        <button class="target-row" onclick={() => openTarget_(t.id)}>
          <div class="target-name">{t.name}</div>
          <div class="target-meta muted">
            <span>{t.session_count} session{t.session_count === 1 ? '' : 's'}</span>
            <span aria-hidden="true">·</span>
            <span>{t.frame_count.toLocaleString()} frames</span>
            {#if t.failed_count > 0}
              <span aria-hidden="true">·</span>
              <span class="bad">{t.failed_count} failed</span>
            {/if}
            {#if t.last_session_at}
              <span aria-hidden="true">·</span>
              <span>last {shortDate(t.last_session_at)}</span>
            {/if}
          </div>
        </button>

        {#if openTargetId === t.id}
          <div class="target-detail">
            {#if openLoading}
              <p class="muted">Loading…</p>
            {:else if openTarget}
              <ul class="session-list">
                {#each openTarget.sessions as s (s.id)}
                  <li class="session">
                    <div class="session-head">
                      <span class="session-when">{shortDate(s.started_at)}</span>
                      <span class="session-tags muted">
                        {s.exptime ?? '?'}s · gain {s.gain ?? '?'} · {s.filter ?? '—'}
                      </span>
                    </div>
                    <div class="session-body">
                      <span>{s.frame_count} frame{s.frame_count === 1 ? '' : 's'}</span>
                      {#if s.failed_count > 0}
                        <span class="bad">{s.failed_count} failed</span>
                      {/if}
                      <span class="cal-row">
                        {#each s.calibration as c (c.kind)}
                          <span class={calBadgeClass(c)} title="{c.kind}: {c.quality}">
                            {calLabel(c.kind)}
                          </span>
                        {/each}
                      </span>
                    </div>
                  </li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .scan-bar {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    margin: 1rem 0 1.5rem;
    flex-wrap: wrap;
  }

  .scan-bar input {
    flex: 1 1 240px;
    min-width: 0;
    padding: 0.5rem 0.8rem;
    border-radius: 999px;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    font: inherit;
  }

  .scan-bar input:focus {
    outline: none;
    border-color: var(--accent);
  }

  .scan-result {
    font-size: 0.85rem;
    flex-basis: 100%;
  }

  .error {
    color: var(--bad);
    background: rgba(255, 122, 138, 0.1);
    border: 1px solid var(--bad);
    padding: 0.5rem 0.8rem;
    border-radius: var(--radius);
  }

  .target-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .target {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    transition: border-color 120ms ease;
  }

  .target.open {
    border-color: var(--accent);
  }

  .target-row {
    width: 100%;
    background: transparent;
    border: none;
    padding: 0.85rem 1rem;
    border-radius: 0;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    cursor: pointer;
  }

  .target-row:hover {
    background: rgba(122, 162, 255, 0.05);
  }

  .target-name {
    font-weight: 600;
    font-size: 1.05rem;
  }

  .target-meta {
    font-size: 0.85rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .bad {
    color: var(--bad);
  }

  .target-detail {
    border-top: 1px solid var(--border);
    padding: 0.5rem 1rem 1rem;
  }

  .session-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .session {
    padding: 0.55rem 0.6rem;
    border-radius: 8px;
    background: rgba(0, 0, 0, 0.2);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .session-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  .session-when {
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  .session-tags {
    font-size: 0.8rem;
  }

  .session-body {
    font-size: 0.85rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .cal-row {
    display: inline-flex;
    gap: 0.3rem;
    margin-left: auto;
  }

  .cal {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.6rem;
    height: 1.6rem;
    border-radius: 50%;
    font-size: 0.75rem;
    font-weight: 600;
    border: 1px solid var(--border);
  }

  .cal-exact {
    background: rgba(94, 211, 168, 0.18);
    border-color: var(--good);
    color: var(--good);
  }

  .cal-approx {
    background: rgba(240, 179, 94, 0.18);
    border-color: var(--warn);
    color: var(--warn);
  }

  .cal-none {
    background: rgba(255, 122, 138, 0.12);
    border-color: var(--bad);
    color: var(--bad);
  }

  @media (max-width: 600px) {
    .target-row {
      padding: 0.7rem 0.8rem;
    }
    .scan-bar {
      gap: 0.4rem;
    }
  }
</style>
