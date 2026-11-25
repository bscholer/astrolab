<!--
  Lay out a mosaic: the grid sits at the centre of the map, so panning the
  map moves it. Saving puts the panels in the plan.
-->
<script lang="ts">
  import { fmtHours, MOSAIC_MAX_SIDE, MOSAIC_OVERLAP, mosaicSize, type Grid } from './atlas';

  let {
    name,
    grid,
    compact = false,
    busy = false,
    replacing = false,
    inside = [],
    onchange,
    onsave,
    oncancel,
  }: {
    name: string;
    grid: Grid;
    compact?: boolean;
    busy?: boolean;
    /** True when this mosaic is already in the plan. */
    replacing?: boolean;
    /** Named objects inside the grid, with the time already shot on each. */
    inside?: { name: string; common: string | null; shot_s: number }[];
    onchange: (g: Grid) => void;
    onsave: () => void;
    oncancel: () => void;
  } = $props();

  const size = $derived(mosaicSize(grid));
  const panels = $derived(grid.cols * grid.rows);

  function step(key: keyof Grid, by: number) {
    const next = { ...grid, [key]: Math.max(1, Math.min(MOSAIC_MAX_SIDE, grid[key] + by)) };
    if (next.cols * next.rows >= 2) onchange(next);
  }
</script>

<section class="editor" class:compact aria-label="Mosaic of {name}">
  <header>
    <h2>Mosaic of {name}</h2>
    <p class="meta mono">
      {panels} panels · {size[0].toFixed(1)}° × {size[1].toFixed(1)}° · {Math.round(MOSAIC_OVERLAP * 100)}% overlap
    </p>
  </header>

  <div class="steppers">
    {#each [['cols', 'Across'], ['rows', 'Down']] as [key, label] (key)}
      {@const k = key as keyof Grid}
      <div class="stepper" role="group" aria-label={label}>
        <span class="label">{label}</span>
        <button type="button" aria-label="Fewer {label.toLowerCase()}" disabled={grid[k] <= 1 || (k === 'cols' ? grid.rows : grid.cols) * (grid[k] - 1) < 2} onclick={() => step(k, -1)}>−</button>
        <span class="value mono">{grid[k]}</span>
        <button type="button" aria-label="More {label.toLowerCase()}" disabled={grid[k] >= MOSAIC_MAX_SIDE} onclick={() => step(k, 1)}>+</button>
      </div>
    {/each}
  </div>

  {#if inside.length}
    <ul class="inside" aria-label="Already shot inside the grid">
      {#each inside as o (o.name)}
        <li>
          <span class="o-name">{o.common ?? o.name}</span>
          <span class="o-time mono" class:none={o.shot_s <= 0}>
            {o.shot_s > 0 ? `${fmtHours(o.shot_s)} shot` : 'not shot yet'}
          </span>
        </li>
      {/each}
    </ul>
  {/if}

  {#if !compact}
    <p class="hint">
      Drag the map to move the grid. The panels go in the plan and take turns, the one
      with the least time under it first, counting everything already shot there. Every panel
      stacks with anything else you have shot where it overlaps.
    </p>
  {/if}

  <div class="actions">
    <button type="button" class="primary" disabled={busy} onclick={onsave}>
      {replacing ? 'Replace in the plan' : 'Add to the auto list'}
    </button>
    <button type="button" disabled={busy} onclick={oncancel}>Cancel</button>
  </div>
</section>

<style>
  .editor {
    display: grid;
    gap: 0.75rem;
  }
  .compact {
    gap: 0.55rem;
  }
  h2 {
    margin: 0;
    font-size: 1.1rem;
  }
  .meta {
    margin: 0.2rem 0 0;
    font-size: 0.78rem;
    color: var(--accent);
  }
  .mono {
    font-family: var(--font-mono);
  }
  .steppers {
    display: flex;
    gap: 1.25rem;
    flex-wrap: wrap;
  }
  .stepper {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
  }
  .label {
    font-size: 0.85rem;
    color: var(--fg-mute);
    min-width: 3.2rem;
  }
  .stepper button {
    width: 34px;
    height: 34px;
    padding: 0;
    font-size: 1.1rem;
    line-height: 1;
  }
  .value {
    min-width: 1.2rem;
    text-align: center;
    font-size: 1rem;
  }
  .inside {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 0.2rem;
    font-size: 0.84rem;
  }
  .inside li {
    display: flex;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .o-time {
    color: #c4b5fd;
    font-size: 0.78rem;
  }
  .o-time.none {
    color: var(--fg-mute);
  }
  .hint {
    margin: 0;
    font-size: 0.82rem;
    color: var(--fg-mute);
    line-height: 1.45;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .actions button {
    font-size: 0.85rem;
    padding: 0.4rem 0.9rem;
  }
  .primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: var(--accent);
    font-weight: 600;
  }
</style>
