<!--
  What more data would buy for one target master, and one click to ask the
  scheduler for it. Used on a master's project page (full) and in lists on
  Tonight (compact). Data: /api/target-masters/advice (server/capture_loop.py).
-->
<script lang="ts">
  import { api, type MasterAdviceRow } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  type Props = { row: MasterAdviceRow; compact?: boolean; onUpdated?: (row: MasterAdviceRow) => void };
  let { row, compact = false, onUpdated }: Props = $props();
  let busy = $state(false);

  const a = $derived(row.advice);
  const pct = (x: number) => `${Math.round(x * 100)}%`;
  const hrs = (h: number) => (h < 1 ? `${Math.round(h * 60)} min` : `${h.toFixed(1).replace(/\.0$/, '')} h`);

  async function more(hours: number) {
    busy = true;
    try {
      const res = await api.queueMoreData(row.project_id, hours);
      onUpdated?.(res.advice);
      toast.success(`${row.target}: goal now ${res.target.goal_hours} h in the scheduler`);
    } catch (e) {
      toast.error(`Couldn't add time: ${(e as Error).message}`);
    } finally {
      busy = false;
    }
  }
</script>

<div class="more" class:compact data-verdict={a.verdict}>
  {#if !compact}<h2 class="section-h">More data</h2>{/if}
  <div class="head">
    <b>{a.headline}</b>
    <span class="facts">
      {hrs(a.hours)} so far
      {#if a.snr !== null}· signal-to-noise {a.snr.toFixed(1)}{/if}
    </span>
  </div>
  <div class="opts">
    {#each a.options.slice(0, compact ? 3 : 4) as o (o.hours)}
      <button type="button" disabled={busy} onclick={() => more(o.hours)}
        title="Raise the scheduler's goal for {row.target} by {o.hours} h">
        +{o.hours} h <span>noise −{pct(o.noise_drop)}</span>
      </button>
    {/each}
  </div>
  <div class="plan">
    {#if row.plan}
      {`In the scheduler: ${hrs(row.plan.shot_hours)} shot` +
        (row.plan.goal_hours ? ` of a ${row.plan.goal_hours} h goal` : ', no goal set') +
        (row.plan.remaining_hours === 0 ? ', goal reached' : '')}
      {#if row.plan.remaining_hours && row.plan.remaining_noise_drop}
        <br /><b class="planned">Already planned: {hrs(row.plan.remaining_hours)} more, noise −{pct(row.plan.remaining_noise_drop)} when done.</b>
      {/if}
    {:else}
      Not in the scheduler's targets; adding time puts it there.
    {/if}
  </div>
</div>

<style>
  .more {
    margin: 1.25rem 0;
  }
  .more.compact {
    margin: 0;
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.9rem;
    align-items: baseline;
  }
  .facts,
  .plan {
    color: var(--fg-mute);
    font-size: 0.85rem;
  }
  .plan {
    margin-top: 0.4rem;
  }
  [data-verdict='diminishing'] b {
    color: var(--fg-mute);
  }
  .planned {
    color: var(--fg);
    font-weight: 500;
  }
  .opts {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-top: 0.5rem;
  }
  .opts button {
    appearance: none;
    border: 1px solid var(--border);
    background: var(--bg-elev);
    color: var(--fg);
    border-radius: 999px;
    padding: 0.25rem 0.75rem;
    font-size: 0.85rem;
    cursor: pointer;
  }
  .opts button:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .opts button:disabled {
    opacity: 0.5;
  }
  .opts span {
    color: var(--fg-mute);
    margin-left: 0.25rem;
  }
</style>
