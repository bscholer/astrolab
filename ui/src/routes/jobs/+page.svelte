<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type JobSummary } from '$lib/api';

  let jobs = $state<JobSummary[] | null>(null);
  let error = $state<string | null>(null);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  async function load() {
    try {
      jobs = await api.listJobs();
      error = null;
    } catch (e) {
      error = (e as Error).message;
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

  function formatDuration(start: string | null, end: string | null): string {
    if (!start) return '';
    const t0 = new Date(start).getTime();
    const t1 = end ? new Date(end).getTime() : Date.now();
    const sec = (t1 - t0) / 1000;
    if (sec < 1) return '<1s';
    if (sec < 60) return `${sec.toFixed(1)}s`;
    return `${Math.floor(sec / 60)}m ${Math.floor(sec % 60)}s`;
  }
</script>

<h1>Jobs</h1>

{#if error}
  <p class="err">Error: {error}</p>
{:else if jobs === null}
  <p class="muted">Loading…</p>
{:else if jobs.length === 0}
  <p class="muted">
    No jobs yet. Submit one with <code>POST /api/jobs</code>; the
    <code>scripts/smoke_pipeline.py</code> example is the easiest way to kick one off.
  </p>
{:else}
  <table class="jobs">
    <thead>
      <tr>
        <th>Status</th>
        <th>Template</th>
        <th>Submitted</th>
        <th>Duration</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      {#each jobs as j (j.id)}
        <tr>
          <td><span class="status status-{j.status}">{j.status}</span></td>
          <td><code>{j.template_id}</code> v{j.template_version}</td>
          <td class="muted">{j.submitted_at.replace('T', ' ').slice(0, 19)}</td>
          <td class="muted">{formatDuration(j.started_at, j.finished_at)}</td>
          <td><a href="/jobs/{j.id}">open →</a></td>
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
</style>
