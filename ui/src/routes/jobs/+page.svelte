<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type JobSummary } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { formatDuration, formatExposure, shortAgo } from '$lib/format';

  let jobs = $state<JobSummary[] | null>(null);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  async function load() {
    try {
      jobs = await api.listJobs();
    } catch (e) {
      toast.error(`Failed to load jobs: ${(e as Error).message}`);
    }
  }

  onMount(() => {
    load();
    // Poll while any jobs are still active. The list endpoint is cheap.
    pollHandle = setInterval(() => {
      const anyActive = jobs?.some((j) => j.status === 'queued' || j.status === 'running');
      if (anyActive || jobs === null) load();
    }, 1500);
  });

  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
  });
</script>

<h1>Jobs</h1>

{#if jobs === null}
  <p class="muted">Loading…</p>
{:else if jobs.length === 0}
  <p class="muted">
    No jobs yet. Open a session in the <a href="/">Library</a> and click
    <strong>Run…</strong> to start one.
  </p>
{:else}
  <table class="jobs">
    <thead>
      <tr>
        <th>Target</th>
        <th>Capture</th>
        <th>Status</th>
        <th>Duration</th>
        <th>Submitted</th>
      </tr>
    </thead>
    <tbody>
      {#each jobs as j, i (j.id)}
        <tr style="--stagger: {i}">
          <td>
            <a class="target-link" href="/jobs/{j.id}">
              {#if j.capture?.target_name}
                {j.capture.target_name}
              {:else}
                <span class="muted">—</span>
              {/if}
            </a>
          </td>
          <td class="muted small num">{formatExposure(j) || '—'}</td>
          <td><span class="status status-{j.status}">{j.status}</span></td>
          <td class="muted small num">{formatDuration(j.started_at, j.finished_at)}</td>
          <td class="muted small num" title={j.submitted_at}>{shortAgo(j.submitted_at)}</td>
        </tr>
      {/each}
    </tbody>
  </table>
{/if}

<style>
  table.jobs {
    width: 100%;
    border-collapse: collapse;
    margin-top: 1rem;
  }
  table.jobs th,
  table.jobs td {
    padding: 0.55rem 0.85rem;
    text-align: left;
    border-bottom: 1px solid var(--hairline);
    font-size: 0.9rem;
  }
  table.jobs th {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  table.jobs tbody tr {
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 40ms + 80ms);
  }
  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-queued {
    background: rgba(255, 255, 255, 0.08);
    color: var(--fg-mute);
  }
  .status-running {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .status-completed {
    background: color-mix(in oklab, var(--good) 18%, transparent);
    color: var(--good);
  }
  .status-failed {
    background: color-mix(in oklab, var(--bad) 18%, transparent);
    color: var(--bad);
  }
  .muted { color: var(--fg-mute); }
  .small { font-size: 0.82rem; }
  /* Target column gets the serif treatment per design.md. */
  .target-link {
    color: var(--fg);
    text-decoration: none;
    font-family: var(--font-display);
    font-weight: 500;
    letter-spacing: -0.01em;
  }
  .target-link:hover {
    color: var(--accent);
  }

  /* Drop Duration + Submitted on phones; Target / Capture / Status are
     the load-bearing columns and the timestamps live on the job detail
     page anyway. */
  @media (max-width: 500px) {
    table.jobs th:nth-child(4),
    table.jobs td:nth-child(4),
    table.jobs th:nth-child(5),
    table.jobs td:nth-child(5) {
      display: none;
    }
  }
</style>
