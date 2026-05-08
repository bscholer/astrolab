<!--
  Rendering list: every editable stack-in-progress, newest first. Clicking a
  row opens the detail page where the user can tweak params and watch the
  pipeline re-run.

  This is a thin wrapper over GET /api/renderings; the heavy lifting lives
  on the detail page.
-->
<script lang="ts">
  import { api, type Rendering } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo } from '$lib/format';

  let renderings = $state<Rendering[] | null>(null);

  async function load() {
    try {
      renderings = await api.listRenderings();
    } catch (e) {
      toast.error(`Couldn't load renderings: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    load();
  });
</script>

<div class="header">
  <a href="/" class="back">← library</a>
  <h1>Renderings</h1>
  <a href="/jobs" class="muted small debug">debug: jobs</a>
</div>

{#if renderings === null}
  <p class="muted">Loading…</p>
{:else if renderings.length === 0}
  <p class="muted">
    No renderings yet. Click "Run…" on a session in the library to start one.
  </p>
{:else}
  <ul class="list">
    {#each renderings as r (r.id)}
      <li>
        <a class="row" href="/renderings/{r.id}">
          <div class="row-name">{r.name}</div>
          <div class="row-meta muted small">
            <span title={r.template_id}>{r.template_id}</span>
            <span aria-hidden="true">·</span>
            <span>v{r.current_seq + 1} of {r.history.length}</span>
            <span aria-hidden="true">·</span>
            <span title={r.updated_at}>{shortAgo(r.updated_at)}</span>
          </div>
        </a>
      </li>
    {/each}
  </ul>
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
  .debug {
    text-decoration: none;
  }
  .debug:hover {
    text-decoration: underline;
  }
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute, #888);
  }
  .list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .row {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.6rem 0.8rem;
    background: var(--bg-elev, #14171d);
    border: 1px solid var(--border, #333);
    border-radius: 8px;
    text-decoration: none;
    color: inherit;
  }
  .row:hover {
    border-color: var(--accent, #7aa2ff);
    background: rgba(122, 162, 255, 0.05);
  }
  .row-name {
    font-weight: 600;
    font-size: 1rem;
  }
  .row-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }
</style>
