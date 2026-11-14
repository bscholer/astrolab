<!--
  "Last night": the home page's first card. For the most recent night with
  captures, each target shot, how much of it was kept, and what its target
  master gained, with a before/after wipe once the new master has rendered.
  Data: GET /api/morning (server/morning.py).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type MorningResponse, type MorningTarget, type MorningMasterUpdate } from '$lib/api';
  import { formatIntegrationTime } from '$lib/format';
  import CompareSlider from '$lib/CompareSlider.svelte';

  let data = $state<MorningResponse | null>(null);
  let failed = $state(false);
  let night = $state<string | undefined>(undefined);

  async function load() {
    try {
      data = await api.getMorning(night);
      failed = false;
    } catch {
      failed = true;
    }
  }

  // Poll while something is still moving (judging, queued, stacking).
  const busy = $derived(
    !!data?.targets.some(
      (t) =>
        t.state === 'judging' ||
        t.state === 'queued' ||
        t.masters.some((m) => m.status === 'running' || m.status === 'queued')
    )
  );

  onMount(load);
  $effect(() => {
    const h = setInterval(load, busy ? 20_000 : 120_000);
    return () => clearInterval(h);
  });

  function step(delta: number) {
    if (!data?.night) return;
    const i = data.nights.indexOf(data.night) + delta;
    if (i < 0 || i >= data.nights.length) return;
    night = data.nights[i];
    load();
  }

  function nightLabel(n: string): string {
    const d = new Date(`${n}T12:00:00`);
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86_400_000);
    // A night is named for the evening it began.
    const iso = (x: Date) =>
      `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
    const prefix = iso(d) === iso(yesterday) ? 'Last night' : iso(d) === iso(today) ? 'Tonight' : 'Night of';
    return `${prefix} · ${d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}`;
  }

  function stateText(t: MorningTarget, m: MorningMasterUpdate | undefined): string {
    if (m) {
      if (m.status === 'running' || m.status === 'queued') return 'Stacking into the master…';
      if (m.status === 'failed') return 'Master stack failed';
      return 'Master updated';
    }
    switch (t.state) {
      case 'judging':
        return 'Still judging frames';
      case 'queued':
        return 'Joins its master once the night settles';
      case 'kept':
        return 'Kept';
      default:
        return 'Scrap: too little usable data';
    }
  }

  const totals = $derived.by(() => {
    const t = data?.targets ?? [];
    return {
      kept: t.reduce((a, x) => a + x.kept_s, 0),
      scrap: t.reduce((a, x) => a + x.scrap_s, 0),
    };
  });

  const RECIPES: Record<string, string> = {
    nebula: 'nebula',
    'nebula-duo': 'duo-band nebula',
    galaxy: 'galaxy',
    stars: 'star field',
  };

  function pct(x: number): string {
    return `${Math.round(x * 100)}%`;
  }
</script>

{#if data?.night}
  <section class="lastnight" aria-label="Last night">
    <header>
      <button class="nav" onclick={() => step(-1)} disabled={data.nights.indexOf(data.night) <= 0} aria-label="Earlier night">‹</button>
      <h2>{nightLabel(data.night)}</h2>
      <button class="nav" onclick={() => step(1)} disabled={data.nights.indexOf(data.night) >= data.nights.length - 1} aria-label="Later night">›</button>
      <span class="sum">
        {data.targets.length} target{data.targets.length === 1 ? '' : 's'}
        · {formatIntegrationTime(totals.kept) || '0 min'} kept
        {#if totals.scrap > 0}· {formatIntegrationTime(totals.scrap)} scrap{/if}
      </span>
    </header>

    <ul>
      {#each data.targets as t (t.target_id ?? t.name)}
        {@const m = t.masters[0]}
        <li class:scrap={t.state === 'scrap'} class:compact={!m && t.state !== 'judging'}>
          {#if m || t.state === 'judging'}
          <div class="pic">
            {#if m?.after && m?.before}
              <CompareSlider
                aSrc={api.previewUrl(m.before.hash, m.before.port)}
                bSrc={api.previewUrl(m.after.hash, m.after.port)}
                aLabel="Before"
                bLabel="With this night"
                aspect="3 / 2"
              />
            {:else if m?.after}
              <img src={api.previewUrl(m.after.hash, m.after.port)} alt="{t.name} master" loading="lazy" />
            {:else if m?.before}
              <img class="stale" src={api.previewUrl(m.before.hash, m.before.port)} alt="{t.name} before this night" loading="lazy" />
              <span class="overlay">{m.status === 'failed' ? 'failed' : 'stacking…'}</span>
            {:else}
              <div class="empty">{m ? 'stacking…' : 'judging…'}</div>
            {/if}
          </div>
          {/if}
          <div class="info">
            <div class="name">
              {#if m}<a href="/projects/{m.project_id}">{t.common_name ?? t.name}</a>{:else}{t.common_name ?? t.name}{/if}
              {#if t.common_name}<span class="canon">{t.name}</span>{/if}
              <span class="state" data-state={m ? (m.status === 'completed' ? 'done' : m.status) : t.state}>{stateText(t, m)}</span>
            </div>
            <div class="facts">
              {#if t.kept_s > 0}<span>{formatIntegrationTime(t.kept_s)} kept</span>{/if}
              {#if t.scrap_s > 0}<span>{formatIntegrationTime(t.scrap_s)} scrap</span>{/if}
              {#if t.judging_s > 0}<span>{formatIntegrationTime(t.judging_s)} judging</span>{/if}
              <span>{t.frames} frames</span>
            </div>
            {#if m}
              <div class="gain">
                <b>{m.before_seq === null ? `New master${m.recipe ? `, ${RECIPES[m.recipe] ?? m.recipe} recipe` : ''}` : m.label}</b>
                <span>master now {formatIntegrationTime(m.total_s)}</span>
                {#if m.noise_drop}<span title="Expected from integration time: noise falls with its square root">noise −{pct(m.noise_drop)}</span>{/if}
              </div>
              <a class="open" href="/projects/{m.project_id}">Finish it →</a>
            {/if}
          </div>
        </li>
      {/each}
    </ul>
  </section>
{:else if failed}
  <!-- The library below still works; stay quiet. -->
{/if}

<style>
  .lastnight {
    border: 1px solid var(--border);
    border-radius: var(--radius-card, 12px);
    background: var(--bg-elev);
    padding: 0.9rem 1rem 0.4rem;
    margin: 0.5rem 0 1.5rem;
  }
  header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.6rem;
  }
  h2 {
    margin: 0;
    font-size: 1.15rem;
  }
  .nav {
    appearance: none;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg-mute);
    border-radius: 6px;
    width: 26px;
    height: 26px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    display: grid;
    place-items: center;
    padding: 0 0 2px;
  }
  .nav:disabled {
    opacity: 0.35;
    cursor: default;
  }
  .sum {
    color: var(--fg-mute);
    font-size: 0.85rem;
    margin-left: auto;
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: grid;
    grid-template-columns: minmax(180px, 280px) 1fr;
    gap: 1rem;
    padding: 0.7rem 0;
    border-top: 1px solid var(--border);
  }
  li:first-child {
    border-top: 0;
  }
  li.scrap {
    opacity: 0.6;
  }
  li.compact {
    grid-template-columns: 1fr;
    padding: 0.45rem 0;
  }
  .canon {
    font-weight: 400;
    font-size: 0.78rem;
    color: var(--fg-mute);
    font-family: var(--font-mono);
  }
  @media (max-width: 640px) {
    li {
      grid-template-columns: 1fr;
    }
  }
  .pic {
    position: relative;
    border-radius: 8px;
    overflow: hidden;
    background: var(--bg);
    aspect-ratio: 3 / 2;
  }
  .pic img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }
  .pic img.stale {
    opacity: 0.45;
  }
  .overlay,
  .empty {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    color: var(--fg-mute);
    font-size: 0.85rem;
  }
  .info {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    min-width: 0;
  }
  .name {
    font-weight: 600;
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  .name a {
    color: inherit;
    text-decoration: none;
  }
  .state {
    font-weight: 500;
    font-size: 0.78rem;
    color: var(--fg-mute);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.05rem 0.5rem;
  }
  .state[data-state='done'] {
    color: var(--good);
    border-color: currentColor;
  }
  .state[data-state='failed'] {
    color: var(--bad);
    border-color: currentColor;
  }
  .state[data-state='running'],
  .state[data-state='queued'] {
    color: var(--accent);
    border-color: currentColor;
  }
  .facts,
  .gain {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem 0.9rem;
    font-size: 0.85rem;
    color: var(--fg-mute);
  }
  .gain b {
    color: var(--fg);
    font-weight: 600;
  }
  .open {
    font-size: 0.85rem;
    color: var(--accent);
    text-decoration: none;
    margin-top: auto;
  }
</style>
