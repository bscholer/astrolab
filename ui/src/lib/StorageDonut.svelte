<!--
  A storage breakdown: a donut beside a table that carries the real numbers.
  Used for the NAS (NasStorage) and the local disk (LocalStorage). Slices keep
  the order and colors the caller gives, so a kind keeps its color when others
  come and go; a hatched kind is told apart by texture as well as color.
-->
<script lang="ts">
  import { formatBytes } from '$lib/format';

  export type DonutKind = { id: string; label: string; bytes: number; files?: number };

  let {
    kinds,
    colors,
    help = {},
    hatched = [],
    center,
    ariaLabel,
  }: {
    kinds: DonutKind[];
    colors: Record<string, string>;
    help?: Record<string, string>;
    hatched?: string[];
    /** Label under the total in the middle of the ring. */
    center: string;
    ariaLabel: string;
  } = $props();

  let hovered = $state<string | null>(null);
  const uid = `donut-${Math.random().toString(36).slice(2, 8)}`;

  const shown = $derived(kinds.filter((k) => k.bytes > 0));
  const total = $derived(shown.reduce((a, k) => a + k.bytes, 0));
  const largestFirst = $derived([...shown].sort((a, b) => b.bytes - a.bytes));
  const withFiles = $derived(shown.some((k) => k.files !== undefined));

  const SIZE = 200;
  const R = 84;
  const STROKE = 30;
  const C = 2 * Math.PI * R;
  // A sliver still gets a visible arc; the table carries its real share.
  const MIN_ARC = 2;

  const arcs = $derived.by(() => {
    let at = 0;
    return shown.map((k) => {
      const len = total > 0 ? Math.max(MIN_ARC, (k.bytes / total) * C) : 0;
      const arc = { kind: k, len, offset: at };
      at += (k.bytes / total) * C;
      return arc;
    });
  });

  const focus = $derived(hovered ? shown.find((k) => k.id === hovered) ?? null : null);

  function paint(id: string): string {
    return hatched.includes(id) ? `url(#${uid}-${id})` : colors[id];
  }

  function share(k: DonutKind): string {
    if (total <= 0) return '';
    const p = (k.bytes / total) * 100;
    return p < 1 ? '<1%' : `${Math.round(p)}%`;
  }
</script>

<div class="body">
  <svg
    class="donut"
    viewBox="0 0 {SIZE} {SIZE}"
    width={SIZE}
    height={SIZE}
    role="img"
    aria-label="{ariaLabel}: {largestFirst.map((k) => `${k.label} ${formatBytes(k.bytes)}`).join(', ')}"
  >
    <defs>
      {#each hatched as id (id)}
        <pattern id="{uid}-{id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="6" height="6" fill={colors[id]} />
          <line x1="0" y1="0" x2="0" y2="6" class="hatch" />
        </pattern>
      {/each}
    </defs>
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
          stroke={paint(a.kind.id)}
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
      {focus ? focus.label : center}
    </text>
  </svg>

  <table class="legend">
    <thead>
      <tr>
        <th scope="col">Kind</th>
        <th scope="col" class="num">Size</th>
        <th scope="col" class="num">Share</th>
        {#if withFiles}<th scope="col" class="num files">Files</th>{/if}
      </tr>
    </thead>
    <tbody>
      {#each largestFirst as k (k.id)}
        <tr
          class:active={hovered === k.id}
          onmouseenter={() => (hovered = k.id)}
          onmouseleave={() => (hovered = null)}
          title={help[k.id]}
        >
          <td>
            <svg class="swatch" width="10" height="10" aria-hidden="true">
              <rect width="10" height="10" rx="3" fill={paint(k.id)} />
            </svg>
            {k.label}
          </td>
          <td class="num">{formatBytes(k.bytes)}</td>
          <td class="num muted">{share(k)}</td>
          {#if withFiles}
            <td class="num muted files">{k.files !== undefined ? k.files.toLocaleString() : ''}</td>
          {/if}
        </tr>
      {/each}
    </tbody>
  </table>
</div>

<style>
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
  .hatch {
    stroke: var(--fg-mute);
    stroke-width: 1.5;
    opacity: 0.6;
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
    margin-right: 0.5rem;
    vertical-align: -1px;
  }
  .muted {
    color: var(--fg-mute);
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
