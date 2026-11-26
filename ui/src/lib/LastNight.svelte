<!--
  Last night, at the top of the Projects page: what the scope shot and what
  each target's master gained, as one line of totals and one card per
  target. A card opens the target's project. Nights whose data didn't make
  it into a master collapse into a single line at the end.
  Data: GET /api/morning (server/morning.py).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type MorningResponse, type MorningTarget } from '$lib/api';
  import { formatIntegrationTime } from '$lib/format';

  let { initial = null }: { initial?: MorningResponse | null } = $props();

  // svelte-ignore state_referenced_locally
  let data = $state<MorningResponse | null>(initial);
  let night = $state<string | undefined>(undefined);

  async function load() {
    try {
      data = await api.getMorning(night);
    } catch {
      // Keep what we have; the next poll tries again.
    }
  }

  // Poll faster while something is still moving (judging, queued, stacking).
  const busy = $derived(
    !!data?.targets.some(
      (t) =>
        t.state === 'judging' ||
        t.state === 'queued' ||
        t.masters.some((m) => m.status === 'running' || m.status === 'queued')
    )
  );

  onMount(() => {
    if (!data) load();
  });
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
    const iso = (x: Date) =>
      `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
    const today = new Date();
    const yesterday = new Date(today.getTime() - 86_400_000);
    // A night is named for the evening it began.
    const day = d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    if (iso(d) === iso(yesterday)) return `Last night · ${day}`;
    if (iso(d) === iso(today)) return `Tonight · ${day}`;
    return day;
  }

  const time = (s: number) => formatIntegrationTime(s) || '0m';
  const usable = (t: MorningTarget) => t.good_s + t.rescued_s;

  /** Targets with something to show: a master update, or still in progress. */
  const shown = $derived((data?.targets ?? []).filter((t) => t.masters.length > 0 || t.state === 'judging' || t.state === 'queued'));
  /** The rest, as one line. */
  const leftOver = $derived((data?.targets ?? []).filter((t) => !shown.includes(t)));

  function chip(t: MorningTarget): { text: string; busy: boolean } {
    const m = t.masters[0];
    if (m) {
      if (m.status === 'running' || m.status === 'queued') return { text: 'Stacking', busy: true };
      if (m.status === 'failed') return { text: 'Stack failed', busy: false };
      return { text: m.before_seq === null ? 'New master' : `+${time(m.added_s)}`, busy: false };
    }
    return t.state === 'judging' ? { text: 'Checking frames', busy: true } : { text: 'Waiting for the night to end', busy: true };
  }

  function leftOverText(t: MorningTarget): string {
    const name = t.common_name ?? t.name;
    if (t.state === 'short') return `${name} (${time(usable(t))}, too short)`;
    if (t.state === 'kept') return `${name} (${time(usable(t))})`;
    return `${name} (${time(usable(t))} usable of ${time(t.shot_s)})`;
  }
</script>

{#if data?.night}
  {@const i = data.nights.indexOf(data.night)}
  <section class="night" aria-label="Last night">
    <header>
      <h2>{nightLabel(data.night)}</h2>
      <span class="navs">
        <button class="nav" onclick={() => step(-1)} disabled={i <= 0} aria-label="Earlier night">‹</button>
        <button class="nav" onclick={() => step(1)} disabled={i >= data.nights.length - 1} aria-label="Later night">›</button>
      </span>
    </header>
    {#if data.totals && data.totals.shot_s > 0}
      {@const tt = data.totals}
      <p class="sum" title="Usable: frames that were good, plus failed frames our own check rescued">
        {time(tt.shot_s)} shot · <b>{time(tt.good_s + tt.rescued_s)} usable</b> · {data.targets.length}
        target{data.targets.length === 1 ? '' : 's'}
        {#if tt.untriaged_s > 0}· {time(tt.untriaged_s)} still being checked{/if}
      </p>
    {/if}

    {#if shown.length}
      <ul class="cards">
        {#each shown as t (t.target_id ?? t.name)}
          {@const m = t.masters[0]}
          {@const c = chip(t)}
          {@const pic = m?.after ?? m?.before ?? null}
          <li>
            <svelte:element this={m ? 'a' : 'div'} class="card" href={m ? `/projects/${m.project_id}` : undefined}>
              <span class="pic">
                {#if pic}<img src={api.previewUrl(pic.hash, pic.port)} alt="" loading="lazy" class:stale={!m?.after} />{/if}
                <span class="chip" class:bad={c.text === 'Stack failed'}>{#if c.busy}<span class="spin"></span>{/if}{c.text}</span>
                <span class="name">{t.common_name ?? t.name}</span>
              </span>
              <span class="facts">
                {time(usable(t))} usable{m ? ` · master ${time(m.total_s)}` : ''}
              </span>
            </svelte:element>
          </li>
        {/each}
      </ul>
    {/if}

    {#if leftOver.length}
      <p class="left">Not added: {leftOver.map(leftOverText).join(' · ')}</p>
    {/if}
  </section>
{/if}

<style>
  .night {
    margin: 0 0 1.75rem;
  }
  header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  h2 {
    margin: 0;
    font-size: 1.1rem;
    font-weight: 600;
    flex: 1;
  }
  .navs {
    display: inline-flex;
    gap: 0.3rem;
  }
  .nav {
    appearance: none;
    width: 30px;
    height: 30px;
    min-height: 0;
    padding: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg);
    font-size: 1.1rem;
    line-height: 1;
    cursor: pointer;
  }
  .nav:disabled {
    opacity: 0.3;
    cursor: default;
  }
  .sum {
    margin: 0.2rem 0 0.8rem;
    color: var(--fg-mute);
    font-size: 0.88rem;
  }
  .sum b {
    color: var(--fg);
    font-weight: 600;
  }
  .cards {
    list-style: none;
    margin: 0;
    padding: 0 0 0.2rem;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(190px, 46%);
    gap: 0.75rem;
    overflow-x: auto;
    scroll-snap-type: x proximity;
    scrollbar-width: none;
  }
  @media (min-width: 760px) {
    .cards {
      grid-auto-flow: row;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
      overflow: visible;
    }
  }
  .cards li {
    scroll-snap-align: start;
  }
  .card {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: var(--radius-card, 12px);
    background: var(--bg-elev);
    overflow: hidden;
    color: inherit;
    text-decoration: none;
    transition: border-color 160ms ease, transform 160ms ease;
  }
  a.card:hover {
    border-color: var(--border-strong);
    transform: translateY(-1px);
  }
  .pic {
    position: relative;
    display: block;
    aspect-ratio: 4 / 3;
    background: var(--bg-elev-2);
  }
  .pic img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .pic img.stale {
    opacity: 0.55;
  }
  .pic::after {
    content: '';
    position: absolute;
    inset: 50% 0 0 0;
    background: linear-gradient(180deg, transparent, rgba(2, 3, 10, 0.85));
    pointer-events: none;
  }
  .name {
    position: absolute;
    left: 0.65rem;
    right: 0.65rem;
    bottom: 0.5rem;
    z-index: 1;
    font-family: var(--font-display);
    font-size: 1rem;
    line-height: 1.2;
    color: #fff;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.6);
  }
  .chip {
    position: absolute;
    top: 0.5rem;
    left: 0.5rem;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.12rem 0.55rem;
    border-radius: 999px;
    background: rgba(0, 0, 0, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.15);
    color: #fff;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }
  .chip.bad {
    background: rgba(236, 72, 153, 0.75);
    border-color: transparent;
  }
  .spin {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    border: 2px solid currentColor;
    border-top-color: transparent;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  .facts {
    padding: 0.5rem 0.65rem 0.6rem;
    font-size: 0.8rem;
    color: var(--fg-mute);
  }
  .left {
    margin: 0.7rem 0 0;
    font-size: 0.8rem;
    color: var(--fg-mute);
  }
</style>
