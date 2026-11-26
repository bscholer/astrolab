<!--
  Storage: what takes up space on the NAS, by kind, as a donut beside a table.
  The server walks the share in the background every few hours (a walk takes
  a minute or more over NFS); this shows the last walk and can start a new one.
  Data: GET /api/storage/nas (server/nas_usage.py).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { api, type NasKind, type NasKindId, type NasUsageResponse } from '$lib/api';
  import { formatBytes, shortAgo } from '$lib/format';
  import { toast } from '$lib/toast.svelte';

  let data = $state<NasUsageResponse | null>(null);
  let failed = $state(false);
  let hovered = $state<NasKindId | null>(null);
  let nowTick = $state(Date.now());

  // Fixed per kind, in slice order, so a kind keeps its color when others
  // come and go. Categorical steps for a dark surface; "Other" is neutral.
  const COLOR: Record<NasKindId, string> = {
    lights: '#3987e5',
    failed: '#d95926',
    scope_outputs: '#199e70',
    calibration: '#c98500',
    images: '#d55181',
    db: '#008300',
    other: '#7d8796',
  };

  const HELP: Record<NasKindId, string> = {
    lights: 'Light frames in scope session folders',
    failed: 'failed_* frames the scope could not register stars in',
    scope_outputs: "The scope's own stacks, previews and thumbnails",
    calibration: 'Darks and factory calibration frames',
    images: 'Finished pictures and FITS that astrolab backed up',
    db: 'Daily database snapshots',
    other: 'Everything else on the share',
  };

  async function load() {
    try {
      data = await api.getNasUsage();
      failed = false;
    } catch {
      failed = true;
    }
  }

  async function refresh() {
    try {
      const r = await api.refreshNasUsage();
      data = r;
      if (!r.started && !r.measuring) toast.error('Could not start a NAS walk');
    } catch (e) {
      toast.error(`Could not start a NAS walk: ${(e as Error).message}`);
    }
  }

  onMount(() => {
    load();
    const tick = setInterval(() => (nowTick = Date.now()), 30_000);
    return () => clearInterval(tick);
  });
  // Poll quickly while a walk runs, so the chart fills in when it ends.
  $effect(() => {
    if (!data?.measuring) return;
    const h = setInterval(load, 3000);
    return () => clearInterval(h);
  });

  const kinds = $derived((data?.last?.kinds ?? []).filter((k) => k.bytes > 0));
  const total = $derived(kinds.reduce((a, k) => a + k.bytes, 0));
  const largestFirst = $derived([...kinds].sort((a, b) => b.bytes - a.bytes));

  // Donut geometry. Slices keep the fixed kind order around the ring.
  const SIZE = 200;
  const R = 84;
  const STROKE = 30;
  const C = 2 * Math.PI * R;
  // A sliver still gets a visible arc; the table carries its real share.
  const MIN_ARC = 2;

  const arcs = $derived.by(() => {
    let at = 0;
    return kinds.map((k) => {
      const len = total > 0 ? Math.max(MIN_ARC, (k.bytes / total) * C) : 0;
      const arc = { kind: k, len, offset: at };
      at += (k.bytes / total) * C;
      return arc;
    });
  });

  const focus = $derived(hovered ? kinds.find((k) => k.id === hovered) ?? null : null);

  function share(k: NasKind): string {
    if (total <= 0) return '';
    const p = (k.bytes / total) * 100;
    return p < 1 ? '<1%' : `${Math.round(p)}%`;
  }

  const measuredLabel = $derived.by(() => {
    void nowTick;
    return data?.last ? shortAgo(data.last.measured_at) : null;
  });
</script>

<section class="nas" aria-label="NAS storage">
  {#if failed && !data}
    <p class="muted">Couldn't load NAS storage.</p>
  {:else if !data}
    <p class="muted">Loading…</p>
  {:else if !data.configured}
    <p class="muted">
      No NAS is set up. Set <code>ASTROLAB_NAS_ROOT</code> to a mounted folder and astrolab will back up
      captures, images and the database there, and show what takes up the space here.
    </p>
  {:else if !data.last}
    <p class="muted">
      {#if data.measuring}
        <span class="spinner" aria-hidden="true"></span> Measuring the NAS. A large share takes a minute or two.
      {:else}
        The NAS hasn't been measured yet.
        <button type="button" class="link-btn" onclick={refresh}>Measure now</button>
      {/if}
    </p>
  {:else}
    {@const last = data.last}
    <div class="body">
      <svg
        class="donut"
        viewBox="0 0 {SIZE} {SIZE}"
        width={SIZE}
        height={SIZE}
        role="img"
        aria-label="NAS storage by kind: {largestFirst.map((k) => `${k.label} ${formatBytes(k.bytes)}`).join(', ')}"
      >
        <g transform="rotate(-90 {SIZE / 2} {SIZE / 2})">
          {#each arcs as a (a.kind.id)}
            <circle
              role="img"
              aria-label="{a.kind.label}: {formatBytes(a.kind.bytes)}"
              class="arc"
              class:dim={hovered !== null && hovered !== a.kind.id}
              cx={SIZE / 2}
              cy={SIZE / 2}
              r={R}
              fill="none"
              stroke={COLOR[a.kind.id]}
              stroke-width={STROKE}
              stroke-dasharray="{a.len} {C - a.len}"
              stroke-dashoffset={-a.offset}
              onmouseenter={() => (hovered = a.kind.id)}
              onmouseleave={() => (hovered = null)}
            >
              <title>{a.kind.label}: {formatBytes(a.kind.bytes)} ({share(a.kind)})</title>
            </circle>
          {/each}
          {#if arcs.length > 1}
            <!-- 2px surface gaps between slices -->
            {#each arcs as a (a.kind.id)}
              <line
                x1={SIZE / 2 + R - STROKE / 2 - 1}
                y1={SIZE / 2}
                x2={SIZE / 2 + R + STROKE / 2 + 1}
                y2={SIZE / 2}
                class="gap"
                transform="rotate({(a.offset / C) * 360} {SIZE / 2} {SIZE / 2})"
              />
            {/each}
          {/if}
        </g>
        <text x={SIZE / 2} y={SIZE / 2 - 4} class="center-value">
          {formatBytes(focus ? focus.bytes : total)}
        </text>
        <text x={SIZE / 2} y={SIZE / 2 + 16} class="center-label">
          {focus ? focus.label : 'on the NAS'}
        </text>
      </svg>

      <table class="legend">
        <thead>
          <tr>
            <th scope="col">Kind</th>
            <th scope="col" class="num">Size</th>
            <th scope="col" class="num">Share</th>
            <th scope="col" class="num files">Files</th>
          </tr>
        </thead>
        <tbody>
          {#each largestFirst as k (k.id)}
            <tr
              class:active={hovered === k.id}
              onmouseenter={() => (hovered = k.id)}
              onmouseleave={() => (hovered = null)}
              title={HELP[k.id]}
            >
              <td>
                <span class="swatch" style="background: {COLOR[k.id]}" aria-hidden="true"></span>
                {k.label}
              </td>
              <td class="num">{formatBytes(k.bytes)}</td>
              <td class="num muted">{share(k)}</td>
              <td class="num muted files">{k.files.toLocaleString()}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>

    <p class="foot muted small">
      <span title={last.root}>{last.root}</span>
      · {formatBytes(last.disk.free_bytes)} free of {formatBytes(last.disk.total_bytes)} on the share
      · measured <span title={last.measured_at}>{measuredLabel}</span>
      {#if data.measuring}
        · <span class="spinner" aria-hidden="true"></span> measuring again…
      {:else}
        · <button type="button" class="link-btn" onclick={refresh}>measure now</button>
      {/if}
      {#if data.error}<br /><span class="err">Last walk failed: {data.error}</span>{/if}
    </p>
  {/if}
</section>

<style>
  .nas {
    margin: 0.5rem 0 1.5rem;
  }
  .body {
    display: flex;
    align-items: center;
    gap: 2rem;
    flex-wrap: wrap;
  }
  .donut {
    flex: none;
    max-width: 100%;
    height: auto;
  }
  .arc {
    transition: opacity 120ms ease;
    cursor: default;
  }
  .arc.dim {
    opacity: 0.35;
  }
  .gap {
    stroke: var(--bg);
    stroke-width: 2;
    pointer-events: none;
  }
  .center-value {
    text-anchor: middle;
    fill: var(--fg);
    font-family: var(--font-mono);
    font-size: 20px;
    font-weight: 600;
  }
  .center-label {
    text-anchor: middle;
    fill: var(--fg-mute);
    font-size: 12px;
  }
  .legend {
    flex: 1 1 18rem;
    border-collapse: collapse;
    font-size: 0.9rem;
  }
  .legend th {
    text-align: left;
    font-weight: 500;
    font-size: 0.75rem;
    color: var(--fg-mute);
    padding: 0 0.5rem 0.4rem;
    border-bottom: 1px solid var(--border);
  }
  .legend td {
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  .legend tr.active td {
    background: var(--accent-soft);
  }
  .num {
    text-align: right;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }
  .legend th.num {
    text-align: right;
  }
  .swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 3px;
    margin-right: 0.5rem;
    vertical-align: -1px;
  }
  .foot {
    margin-top: 1rem;
    overflow-wrap: anywhere;
  }
  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute);
  }
  .err {
    color: var(--bad);
  }
  .link-btn {
    appearance: none;
    background: none;
    border: 0;
    padding: 0;
    min-height: 0;
    font: inherit;
    color: var(--accent);
    cursor: pointer;
    text-decoration: underline;
  }
  .spinner {
    display: inline-block;
    width: 10px;
    height: 10px;
    border: 2px solid var(--fg-mute);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.9s linear infinite;
    vertical-align: -1px;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
  @media (max-width: 560px) {
    .body {
      justify-content: center;
      gap: 1rem;
    }
    .files {
      display: none;
    }
  }
</style>
