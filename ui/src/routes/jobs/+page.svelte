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
        <th class="muted">Submitted</th>
      </tr>
    </thead>
    <tbody>
      {#each jobs as j (j.id)}
        <tr>
          <td>
            <a class="target" href="/jobs/{j.id}">
              {#if j.capture?.target_name}
                {j.capture.target_name}
              {:else}
                <span class="muted">—</span>
              {/if}
            </a>
          </td>
          <td class="muted">{formatExposure(j) || '—'}</td>
          <td><span class="status status-{j.status}">{j.status}</span></td>
          <td class="muted">{formatDuration(j.started_at, j.finished_at)}</td>
          <td class="muted small" title={j.submitted_at}>{shortAgo(j.submitted_at)}</td>
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
    padding: 0.5rem 0.75rem;
    text-align: left;
    border-bottom: 1px solid var(--border, #333);
    font-size: 0.9rem;
  }
  .status {
    display: inline-block;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .status-queued {
    background: #444;
    color: #ccc;
  }
  .status-running {
    background: #234;
    color: #6cf;
  }
  .status-completed {
    background: #243;
    color: #6c9;
  }
  .status-failed {
    background: #422;
    color: #f88;
  }
  .err {
    color: #f88;
  }
  .muted {
    color: var(--muted, #888);
  }
  .small {
    font-size: 0.8rem;
  }
  .target {
    color: var(--fg, #ddd);
    text-decoration: none;
    font-weight: 600;
  }
  .target:hover {
    color: var(--accent, #7aa2ff);
  }
</style>
