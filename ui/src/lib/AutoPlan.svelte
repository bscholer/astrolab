<!--
  The plan: what Run all night works through, in order, with each target's
  settings and the time it has so far. A mosaic shows as one row with its
  panels under it. Lives on the Tonight page; the Scope page links here.
-->
<script lang="ts">
  import { api, type ScopeAutoTarget, type ScopeState } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  let {
    scope,
    onchanged,
    onshow,
  }: {
    scope: ScopeState | null;
    /** Called after a change is saved, to refetch the scope state. */
    onchanged: () => Promise<void> | void;
    /** Show a row's target on the map. */
    onshow?: (t: ScopeAutoTarget) => void;
  } = $props();

  let saving = $state(false);

  const sched = $derived(scope?.scheduler ?? null);
  const obs = $derived(scope?.observe ?? null);

  async function setAuto(enabled: boolean, targets?: ScopeAutoTarget[]) {
    saving = true;
    try {
      await api.setScopeAuto({ enabled, targets });
      await onchanged();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      saving = false;
    }
  }

  function fmtHours(seconds: number): string {
    const h = seconds / 3600;
    return h < 1 ? `${Math.round(seconds / 60)} min` : `${h.toFixed(1)} h`;
  }

  /** A row of the plan: one target, or every panel of one mosaic. */
  interface PlanRow {
    key: string;
    first: ScopeAutoTarget;
    names: string[];
    panels: ScopeAutoTarget[] | null;
  }

  const rows = $derived.by(() => {
    const out: PlanRow[] = [];
    const byMosaic = new Map<string, PlanRow>();
    for (const t of scope?.targets ?? []) {
      if (!t.mosaic) {
        out.push({ key: t.name, first: t, names: [t.name], panels: null });
        continue;
      }
      let row = byMosaic.get(t.mosaic);
      if (!row) {
        row = { key: `mosaic:${t.mosaic}`, first: t, names: [], panels: [] };
        byMosaic.set(t.mosaic, row);
        out.push(row);
      }
      row.names.push(t.name);
      row.panels!.push(t);
    }
    return out;
  });

  /** Time on a target, counting other sessions that cover it. */
  function timeOn(name: string): number {
    return sched?.time_on_s?.[name] ?? sched?.integrated_s?.[name] ?? 0;
  }

  function panelNumber(t: ScopeAutoTarget): string {
    return (t.name.split(' panel ')[1] ?? '').split(' of ')[0];
  }

  /** Change a setting on a row: one target, or every panel of a mosaic. */
  function editRow(row: PlanRow, patch: Partial<ScopeAutoTarget>) {
    const names = new Set(row.names);
    const targets = (scope?.targets ?? []).map((t) => (names.has(t.name) ? { ...t, ...patch } : t));
    return setAuto(!!sched?.enabled, targets);
  }

  function removeRow(row: PlanRow) {
    const names = new Set(row.names);
    return setAuto(!!sched?.enabled, scope?.targets?.filter((o) => !names.has(o.name)));
  }

  /** Move a row up or down; a mosaic's panels move together. */
  function moveRow(i: number, by: -1 | 1) {
    const order = [...rows];
    const j = i + by;
    if (j < 0 || j >= order.length) return;
    [order[i], order[j]] = [order[j], order[i]];
    const byName = new Map((scope?.targets ?? []).map((t) => [t.name, t]));
    const targets = order.flatMap((r) => r.names.map((n) => byName.get(n)!));
    return setAuto(!!sched?.enabled, targets);
  }
</script>

<div class="plan">
  <div class="run">
    <div class="run-text">
      <div class="run-title">Run all night</div>
      <div class="run-sub">
        {#if !scope?.reachable}
          The scope is not reachable. The plan is kept for when it is.
        {:else if sched?.enabled}
          On. Works down this plan, moving on when the sky or a target fails.
        {:else}
          Off. Turn it on to work through this plan after dark.
        {/if}
      </div>
    </div>
    <button
      type="button"
      role="switch"
      aria-checked={!!sched?.enabled}
      aria-label="Run all night"
      class="switch"
      class:on={!!sched?.enabled}
      disabled={saving || !scope?.reachable}
      onclick={() => setAuto(!sched?.enabled)}
    ><span class="knob"></span></button>
  </div>

  {#if rows.length}
    <ul class="queue">
      {#each rows as row, i (row.key)}
        {@const t = row.first}
        {@const running = row.names.includes(obs?.target ?? '')}
        <li class:running>
          <span class="order">
            <button type="button" class="nudge" aria-label="Move {t.mosaic ?? t.name} earlier" disabled={saving || i === 0} onclick={() => moveRow(i, -1)}>▲</button>
            <span class="num">{i + 1}</span>
            <button type="button" class="nudge" aria-label="Move {t.mosaic ?? t.name} later" disabled={saving || i === rows.length - 1} onclick={() => moveRow(i, 1)}>▼</button>
          </span>
          <span class="tname">
            {#if onshow}
              <button type="button" class="name-btn" title="Show on the map" onclick={() => onshow(t)}>
                {t.common_name ?? t.mosaic ?? t.display_name ?? t.name}
              </button>
            {:else}
              {t.common_name ?? t.mosaic ?? t.display_name ?? t.name}
            {/if}
            {#if row.panels}
              <span class="muted small">{t.mosaic} mosaic, {row.panels.length} panels</span>
            {:else if t.common_name}
              <span class="muted small">{t.display_name ?? t.name}</span>
            {/if}
            {#if row.panels}
              {#if t.goal_hours}<span class="muted small">goal {fmtHours(t.goal_hours * 3600)} each</span>{/if}
            {:else if t.goal_hours}
              <span class="muted small">{fmtHours(timeOn(t.name))} / {fmtHours(t.goal_hours * 3600)}</span>
            {:else if timeOn(t.name)}
              <span class="muted small">{fmtHours(timeOn(t.name))}</span>
            {/if}
          </span>
          {#if running}
            <span class="tag">now{#if row.panels} · panel {panelNumber(row.panels.find((p) => p.name === obs?.target) ?? t)}{/if}</span>
          {/if}
          <span class="opts">
            <select
              class="opt"
              aria-label="Filter for {t.name}"
              disabled={saving}
              value={t.filter ?? ''}
              onchange={(e) => editRow(row, { filter: e.currentTarget.value || null } as Partial<ScopeAutoTarget>)}
            >
              <option value="">Filter: auto</option>
              <option value="duo">Duo-band</option>
              <option value="astro">Astro</option>
              <option value="vis">Vis</option>
            </select>
            <select
              class="opt"
              aria-label="Exposure for {t.name}"
              disabled={saving}
              value={t.exposure_s ?? ''}
              onchange={(e) => editRow(row, { exposure_s: (Number(e.currentTarget.value) || null) as ScopeAutoTarget['exposure_s'] })}
            >
              <option value="">15 s</option>
              <option value="30">30 s</option>
              <option value="60">60 s</option>
            </select>
            <input
              class="opt num-in"
              type="number"
              min="0"
              step="0.5"
              aria-label="Integration goal in hours for {row.panels ? 'each panel of ' : ''}{t.name}"
              placeholder={row.panels ? 'goal h each' : 'goal h'}
              disabled={saving}
              value={t.goal_hours ?? ''}
              onchange={(e) => editRow(row, { goal_hours: Number(e.currentTarget.value) > 0 ? Number(e.currentTarget.value) : null })}
            />
            <input
              class="opt num-in"
              type="number"
              min="0"
              aria-label="Gain for {t.name}"
              placeholder="gain 60"
              disabled={saving}
              value={t.gain ?? ''}
              onchange={(e) => editRow(row, { gain: e.currentTarget.value === '' ? null : Number(e.currentTarget.value) })}
            />
            <button
              type="button"
              class="x"
              aria-label="Remove {row.panels ? `the ${t.mosaic} mosaic` : t.name}"
              disabled={saving}
              onclick={() => removeRow(row)}>×</button
            >
          </span>
          {#if row.panels}
            <span class="panels" aria-label="Time per panel">
              {#each row.panels as p (p.name)}
                {@const done = !!p.goal_hours && timeOn(p.name) >= p.goal_hours * 3600}
                <span
                  class="panel"
                  class:done
                  class:now={obs?.target === p.name}
                  title="{p.name}: {fmtHours(timeOn(p.name))}, counting every kept session over it"
                >
                  {panelNumber(p)}<span class="ptime">{fmtHours(timeOn(p.name))}</span>
                </span>
              {/each}
            </span>
          {/if}
        </li>
      {/each}
    </ul>
  {:else}
    <p class="muted small empty">Nothing planned yet. Add targets with + in the list or on the map.</p>
  {/if}
</div>

<style>
  .plan {
    display: grid;
    gap: 0.75rem;
  }
  .run {
    display: flex;
    align-items: center;
    gap: 1rem;
    justify-content: space-between;
  }
  .run-title {
    font-weight: 600;
  }
  .run-sub {
    font-size: 0.82rem;
    color: var(--fg-mute);
  }
  .switch {
    position: relative;
    flex: none;
    width: 46px;
    height: 26px;
    padding: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--bg);
    cursor: pointer;
  }
  .knob {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 18px;
    height: 18px;
    background: var(--fg-mute);
    border-radius: 50%;
    transition: transform 160ms ease, background 160ms ease;
  }
  .switch.on {
    background: color-mix(in srgb, var(--accent) 25%, transparent);
    border-color: var(--accent);
  }
  .switch.on .knob {
    transform: translateX(20px);
    background: var(--accent);
  }
  .switch:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .queue {
    margin: 0;
    padding: 0;
    list-style: none;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .queue li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.2rem 0.7rem;
    padding: 0.2rem 0.4rem 0.2rem 0.4rem;
    border-bottom: 1px solid var(--border);
  }
  .queue li:last-child {
    border-bottom: 0;
  }
  .order {
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    width: 1.6rem;
  }
  .nudge {
    border: 0;
    background: none;
    padding: 0;
    min-height: 0;
    font-size: 0.55rem;
    line-height: 1.1;
    color: var(--fg-mute);
    cursor: pointer;
  }
  .nudge:disabled {
    opacity: 0.25;
    cursor: default;
  }
  .num {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--fg-mute);
  }
  .tname {
    flex: 1 1 12rem;
    min-width: 0;
    overflow-wrap: anywhere;
    font-family: var(--font-mono);
    font-size: 0.9rem;
  }
  .name-btn {
    border: 0;
    background: none;
    padding: 0;
    min-height: 0;
    font: inherit;
    color: inherit;
    text-align: left;
    cursor: pointer;
  }
  .name-btn:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  .opts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  /* On a phone the settings go under the name instead of squeezing it. */
  @media (max-width: 640px) {
    .opts {
      flex-basis: 100%;
      padding-left: 2.3rem;
    }
    .opts .x {
      margin-left: auto;
    }
  }
  .opt {
    font-size: 0.78rem;
    padding: 0.1rem 0.3rem;
  }
  .num-in {
    width: 5.5rem;
  }
  .panels {
    flex-basis: 100%;
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin: 0.35rem 0 0.2rem 2.3rem;
  }
  .panel {
    display: inline-flex;
    align-items: baseline;
    gap: 0.35rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid var(--border);
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.75rem;
  }
  .panel .ptime {
    color: var(--fg-mute);
  }
  .panel.done {
    border-color: var(--good);
  }
  .panel.done .ptime {
    color: var(--good);
  }
  .panel.now {
    border-color: var(--accent);
  }
  li.running .tname {
    color: var(--accent);
  }
  .tag {
    padding: 0.05rem 0.5rem;
    font-size: 0.68rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--accent-ink);
    background: var(--accent);
    border-radius: 999px;
  }
  .x {
    background: none;
    border: 0;
    color: var(--fg-mute);
    min-width: 40px;
    min-height: 40px;
    cursor: pointer;
    font-size: 1.2rem;
  }
  .x:hover {
    color: var(--bad);
  }
  .muted {
    color: var(--fg-mute);
  }
  .small {
    font-size: 0.85em;
  }
  .empty {
    margin: 0;
  }
</style>
