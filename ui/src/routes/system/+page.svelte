<!--
  System dashboard. Polls GET /api/system every 1.5s, pushes the new
  sample onto rolling sparkline buffers, and renders the same layout as
  the playground mock at /playground/system.

  When the poll fails we mark the page disconnected and stop pushing new
  samples; the buffers freeze on the last good values and the live dot
  goes quiet so the user can see that the data on screen is stale.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type SystemSnapshot } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  // ---------- live state ----------
  let snap = $state<SystemSnapshot | null>(null);
  let disconnected = $state(false);
  // Suppress the second-and-Nth toast so a downed backend doesn't spam.
  let warnedOnce = false;

  // Rolling history. ~120 samples at 1.5s = 3 min of sparkline.
  const N = 120;
  // Buffers stay empty until the first successful poll, then we backfill
  // with that sample so the sparkline starts as a flat line rather than
  // a single point spike.
  let cpuHist = $state<number[]>([]);
  let memHist = $state<number[]>([]);
  let gpuHist = $state<number[]>([]);
  let diskReadHist = $state<number[]>([]);
  let diskWriteHist = $state<number[]>([]);

  // For the disk sparkline we want a stable y-scale, not "scale to the
  // max we've seen". Anything above 200 MiB/s saturates the line; that's
  // already an unusual workload on this box.
  const DISK_FULLSCALE_BPS = 200 * 1024 ** 2;

  // ---------- timers ----------
  let pollHandle: ReturnType<typeof setInterval> | null = null;
  let clockHandle: ReturnType<typeof setInterval> | null = null;
  let nowTick = $state(Date.now());

  async function load() {
    try {
      const next = await api.getSystem();
      const firstSample = snap === null;
      snap = next;
      if (disconnected) {
        disconnected = false;
        warnedOnce = false;
      }
      pushHistory(next, firstSample);
    } catch (e) {
      // First failure: surface it. Subsequent failures: stay quiet but
      // keep the disconnected indicator on so the page itself tells the
      // story.
      if (!warnedOnce) {
        toast.error(`Failed to load system info: ${(e as Error).message}`);
        warnedOnce = true;
      }
      disconnected = true;
    }
  }

  function pushHistory(s: SystemSnapshot, firstSample: boolean) {
    const cpuPct = s.cpu.percent;
    const memPct = s.mem.total > 0 ? (s.mem.used / s.mem.total) * 100 : 0;
    const gpuUtil = s.gpu ? s.gpu.util : 0;
    const readPct = (s.disk.read_bps / DISK_FULLSCALE_BPS) * 100;
    const writePct = (s.disk.write_bps / DISK_FULLSCALE_BPS) * 100;

    if (firstSample) {
      // Backfill so the spark doesn't draw as a single dot.
      cpuHist = Array(N).fill(cpuPct);
      memHist = Array(N).fill(memPct);
      gpuHist = Array(N).fill(gpuUtil);
      diskReadHist = Array(N).fill(readPct);
      diskWriteHist = Array(N).fill(writePct);
      return;
    }
    cpuHist = [...cpuHist.slice(1), cpuPct];
    memHist = [...memHist.slice(1), memPct];
    gpuHist = [...gpuHist.slice(1), gpuUtil];
    diskReadHist = [...diskReadHist.slice(1), readPct];
    diskWriteHist = [...diskWriteHist.slice(1), writePct];
  }

  onMount(() => {
    load();
    pollHandle = setInterval(load, 1500);
    // Separate clock so elapsed-time labels tick smoothly between polls.
    clockHandle = setInterval(() => (nowTick = Date.now()), 1000);
  });
  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
    if (clockHandle) clearInterval(clockHandle);
  });

  // ---------- formatters ----------
  function fmtBytes(b: number): string {
    if (b >= 1024 ** 4) return (b / 1024 ** 4).toFixed(2) + ' TiB';
    if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(1) + ' GiB';
    if (b >= 1024 ** 2) return (b / 1024 ** 2).toFixed(0) + ' MiB';
    if (b >= 1024) return (b / 1024).toFixed(0) + ' KiB';
    return b.toFixed(0) + ' B';
  }

  function fmtRate(b: number): string {
    if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(2) + ' GiB/s';
    if (b >= 1024 ** 2) return (b / 1024 ** 2).toFixed(1) + ' MiB/s';
    if (b >= 1024) return (b / 1024).toFixed(0) + ' KiB/s';
    return b.toFixed(0) + ' B/s';
  }

  function fmtUptime(s: number): string {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function fmtElapsed(iso: string | null): string {
    if (!iso) return '-';
    const ms = Date.parse(iso);
    if (Number.isNaN(ms)) return '-';
    const s = Math.max(0, Math.floor((nowTick - ms) / 1000));
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m === 0) return `${r}s`;
    return `${m}m ${r}s`;
  }

  // Some backends emit sampled_at in seconds, others in ms. Anything
  // above ~year 33658 (in seconds) is unrealistic, so values past 1e12
  // are clearly already-ms.
  function toMs(n: number): number {
    return n > 1e12 ? n : n * 1000;
  }

  // Pressure tier for the big CPU/RAM/Disk numbers. Drives the color.
  function tier(pct: number): 'ok' | 'warn' | 'bad' {
    if (pct >= 90) return 'bad';
    if (pct >= 70) return 'warn';
    return 'ok';
  }

  // ---------- sparkline geometry ----------
  function sparkPath(values: number[], w = 200, h = 40, min = 0, max = 100): string {
    if (values.length === 0) return '';
    const range = Math.max(1, max - min);
    const dx = w / Math.max(1, values.length - 1);
    return values
      .map((v, i) => {
        const x = (i * dx).toFixed(1);
        const y = (h - ((v - min) / range) * h).toFixed(1);
        return (i === 0 ? 'M' : 'L') + x + ',' + y;
      })
      .join(' ');
  }

  function sparkArea(values: number[], w = 200, h = 40, min = 0, max = 100): string {
    if (values.length === 0) return '';
    return sparkPath(values, w, h, min, max) + ` L${w},${h} L0,${h} Z`;
  }

  // ---------- derived ----------
  const memPct = $derived(
    snap && snap.mem.total > 0 ? (snap.mem.used / snap.mem.total) * 100 : 0
  );
  const diskPct = $derived(
    snap && snap.disk.total > 0 ? (snap.disk.used / snap.disk.total) * 100 : 0
  );
  const gpuMemPct = $derived(
    snap && snap.gpu && snap.gpu.vram_total > 0
      ? (snap.gpu.vram_used / snap.gpu.vram_total) * 100
      : 0
  );
  const thru = $derived(snap ? snap.jobs.throughput_6h : []);
  const thruMax = $derived(Math.max(4, ...thru));
  const thruTotal = $derived(thru.reduce((a, b) => a + b, 0));
</script>

<svelte:head>
  <title>System · astrolab</title>
</svelte:head>

{#if snap === null && !disconnected}
  <p class="muted">Loading…</p>
{:else if snap === null && disconnected}
  <div class="page-head">
    <h1>System</h1>
    <span class="dot dead" aria-hidden="true"></span>
    <span class="muted small mono">disconnected</span>
  </div>
  <p class="muted">
    Couldn't reach <span class="mono">/api/system</span>. The backend may be
    starting up, or the system endpoint isn't wired yet. We'll keep retrying
    in the background.
  </p>
{:else if snap}
  <div class="page-head">
    <h1>System</h1>
    <span class="dot" class:live={!disconnected} class:dead={disconnected} aria-hidden="true"></span>
    <span class="muted small mono">{snap.host.hostname} · up {fmtUptime(snap.host.uptime_s)}</span>
    <span class="sep" aria-hidden="true"></span>
    {#if disconnected}
      <span class="muted small mono">disconnected · last sample {new Date(toMs(snap.sampled_at)).toLocaleTimeString()}</span>
    {:else}
      <span class="muted small mono">sampled {new Date(toMs(snap.sampled_at)).toLocaleTimeString()}</span>
    {/if}
  </div>

  <p class="host-line muted small">
    <span class="mono">{snap.host.os}</span>
    <span aria-hidden="true">·</span>
    <span class="mono">{snap.host.kernel}</span>
    <span aria-hidden="true">·</span>
    <span class="mono">{snap.host.cpu_model} ({snap.host.cores}c)</span>
    {#if snap.host.gpu_model}
      <span aria-hidden="true">·</span>
      <span class="mono">{snap.host.gpu_model}</span>
    {/if}
  </p>

  <!-- Top metric row: CPU / RAM / Disk are the always-there trio. -->
  <section class="grid metrics" aria-label="Primary metrics">
    <article class="card metric" data-tier={tier(snap.cpu.percent)}>
      <header class="m-head">
        <span class="m-label">CPU</span>
        <span class="m-sub muted small mono">load {snap.cpu.load_avg[0].toFixed(2)} / {snap.cpu.load_avg[1].toFixed(2)} / {snap.cpu.load_avg[2].toFixed(2)}</span>
      </header>
      <div class="m-value mono">{snap.cpu.percent.toFixed(0)}<span class="m-unit">%</span></div>
      <svg class="spark" viewBox="0 0 200 40" preserveAspectRatio="none" aria-hidden="true">
        <path class="spark-fill" d={sparkArea(cpuHist)} />
        <path class="spark-line" d={sparkPath(cpuHist)} />
      </svg>
      <footer class="m-foot muted small mono">
        {#if snap.cpu.temp_c != null}{snap.cpu.temp_c.toFixed(0)}°C · {/if}{snap.host.cores} cores
      </footer>
    </article>

    <article class="card metric" data-tier={tier(memPct)}>
      <header class="m-head">
        <span class="m-label">RAM</span>
        <span class="m-sub muted small mono">{fmtBytes(snap.mem.used)} / {fmtBytes(snap.mem.total)}</span>
      </header>
      <div class="m-value mono">{memPct.toFixed(0)}<span class="m-unit">%</span></div>
      <svg class="spark" viewBox="0 0 200 40" preserveAspectRatio="none" aria-hidden="true">
        <path class="spark-fill" d={sparkArea(memHist)} />
        <path class="spark-line" d={sparkPath(memHist)} />
      </svg>
      <footer class="m-foot muted small mono">
        swap {fmtBytes(snap.mem.swap_used)}
      </footer>
    </article>

    <article class="card metric" data-tier={tier(diskPct)}>
      <header class="m-head">
        <span class="m-label">Disk</span>
        <span class="m-sub muted small mono">{snap.disk.mount}</span>
      </header>
      <div class="m-value mono">{diskPct.toFixed(0)}<span class="m-unit">%</span></div>
      <div class="io-row mono small">
        <span class="io io-r">R <span class="muted">{fmtRate(snap.disk.read_bps)}</span></span>
        <span class="io io-w">W <span class="muted">{fmtRate(snap.disk.write_bps)}</span></span>
      </div>
      <svg class="spark spark-dual" viewBox="0 0 200 40" preserveAspectRatio="none" aria-hidden="true">
        <path class="spark-line spark-r" d={sparkPath(diskReadHist)} />
        <path class="spark-line spark-w" d={sparkPath(diskWriteHist)} />
      </svg>
      <footer class="m-foot muted small mono">
        {fmtBytes(snap.disk.used)} / {fmtBytes(snap.disk.total)}
      </footer>
    </article>

    {#if snap.gpu}
      <article class="card metric" data-tier={tier(snap.gpu.util)}>
        <header class="m-head">
          <span class="m-label">GPU</span>
          <span class="m-sub muted small mono">VRAM {fmtBytes(snap.gpu.vram_used)} / {fmtBytes(snap.gpu.vram_total)}</span>
        </header>
        <div class="m-value mono">{snap.gpu.util.toFixed(0)}<span class="m-unit">%</span></div>
        <svg class="spark" viewBox="0 0 200 40" preserveAspectRatio="none" aria-hidden="true">
          <path class="spark-fill" d={sparkArea(gpuHist)} />
          <path class="spark-line" d={sparkPath(gpuHist)} />
        </svg>
        <footer class="m-foot muted small mono">
          {snap.gpu.temp_c.toFixed(0)}°C · {gpuMemPct.toFixed(0)}% VRAM
        </footer>
      </article>
    {/if}
  </section>

  <!-- Per-core grid. Bars wrap at narrow widths. -->
  <section class="card cores-card">
    <header class="card-head">
      <h2>Per-core</h2>
      <span class="muted small mono">{snap.host.cores} cores</span>
    </header>
    <div class="cores" style="--cores: {snap.cpu.per_core.length}">
      {#each snap.cpu.per_core as v, i}
        <div class="core" title="core {i}: {v.toFixed(0)}%">
          <div class="core-bar" style="--pct: {v}%"></div>
          <span class="core-idx mono small muted">{i}</span>
        </div>
      {/each}
    </div>
  </section>

  <!-- Jobs panel + throughput share a row on desktop, stack on phone. -->
  <section class="grid jobs-row">
    <article class="card jobs-card">
      <header class="card-head">
        <h2>Now running</h2>
        <a class="link mono small" href="/jobs">/jobs →</a>
      </header>

      <div class="counters">
        <div class="counter">
          <span class="c-num mono">{snap.jobs.running}</span>
          <span class="c-lbl muted small">running</span>
        </div>
        <div class="counter">
          <span class="c-num mono">{snap.jobs.queued}</span>
          <span class="c-lbl muted small">queued</span>
        </div>
        <div class="counter">
          <span class="c-num mono">{snap.jobs.completed_24h}</span>
          <span class="c-lbl muted small">done · 24h</span>
        </div>
        <div class="counter">
          <span class="c-num mono c-bad">{snap.jobs.failed_24h}</span>
          <span class="c-lbl muted small">failed · 24h</span>
        </div>
      </div>

      {#if snap.jobs.active.length === 0}
        <p class="muted small idle">Idle. Nothing on the wire.</p>
      {:else}
        <ul class="active-list">
          {#each snap.jobs.active as j (j.id)}
            <li class="active-job">
              <div class="aj-top">
                <a class="aj-target" href="/jobs/{j.id}">{j.target_name ?? j.id}</a>
                <span class="muted small mono">{fmtElapsed(j.started_at)}</span>
              </div>
              {#if j.template_name}
                <div class="aj-meta muted small mono">{j.template_name}</div>
              {/if}
              <div class="progress" aria-label="progress">
                <div class="progress-fill" style="--pct: {(j.progress * 100).toFixed(1)}%"></div>
              </div>
              <div class="aj-pct muted small mono">{(j.progress * 100).toFixed(0)}%</div>
            </li>
          {/each}
        </ul>
      {/if}
    </article>

    <article class="card thru-card">
      <header class="card-head">
        <h2>Throughput</h2>
        <span class="muted small mono">last 6h · {thruTotal} jobs</span>
      </header>
      <div class="thru-bars">
        {#each thru as v, i}
          <div
            class="thru-bar"
            style="--h: {(v / thruMax) * 100}%"
            title="{Math.round(v)} jobs"
            data-now={i === thru.length - 1 ? 'true' : 'false'}
          ></div>
        {/each}
      </div>
      <div class="thru-axis muted small mono">
        <span>6h ago</span>
        <span>now</span>
      </div>
    </article>
  </section>
{/if}

<style>
  /* ---------- page head ---------- */
  .page-head {
    display: flex;
    align-items: baseline;
    gap: 0.7rem;
    margin: 0.5rem 0 0.35rem;
    flex-wrap: wrap;
  }
  .page-head h1 { margin: 0; font-size: 1.5rem; }
  .sep {
    width: 3px; height: 3px; border-radius: 50%;
    background: var(--fg-mute); opacity: 0.5;
    transform: translateY(-2px);
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%;
    transform: translateY(-2px);
  }
  .dot.live {
    background: var(--accent);
    box-shadow: 0 0 0 0 var(--accent-soft);
    animation: pulse 2.2s ease-out infinite;
  }
  .dot.dead {
    background: var(--bad);
    opacity: 0.7;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(94, 234, 212, 0.45); }
    70% { box-shadow: 0 0 0 8px rgba(94, 234, 212, 0); }
    100% { box-shadow: 0 0 0 0 rgba(94, 234, 212, 0); }
  }
  .host-line {
    margin: 0 0 1.1rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: baseline;
  }

  /* ---------- card primitive (matches the visual weight of the jobs page) ---------- */
  .card {
    background: var(--bg-elev);
    border: 1px solid var(--hairline);
    border-radius: var(--radius-card);
    padding: 0.95rem 1rem 0.85rem;
  }
  .card-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin: 0 0 0.7rem;
  }
  .card-head h2 {
    margin: 0;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .link { color: var(--fg-mute); }
  .link:hover { color: var(--accent); }

  /* ---------- top metrics grid ---------- */
  .grid { display: grid; gap: 0.85rem; }
  .metrics {
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    margin-bottom: 0.85rem;
  }
  .metric { display: flex; flex-direction: column; gap: 0.3rem; }
  .m-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.5rem;
  }
  .m-label {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg-mute);
  }
  .m-sub { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .m-value {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 500;
    font-size: 2.4rem;
    line-height: 1.05;
    letter-spacing: -0.015em;
    color: var(--fg);
    /* Tabular feel inside the serif: pad the percent so 9 -> 10 doesn't jiggle. */
    font-variant-numeric: tabular-nums;
  }
  .m-unit { font-size: 1rem; color: var(--fg-mute); margin-left: 0.15rem; font-style: normal; }
  .m-foot { margin-top: 0.1rem; }

  /* tier coloring: ok = teal, warn = amber, bad = magenta. The spark
     fill and value tint pick up the tier. */
  .metric[data-tier='ok'] .spark-line { stroke: var(--accent); }
  .metric[data-tier='ok'] .spark-fill { fill: var(--accent-soft); }
  .metric[data-tier='warn'] .spark-line { stroke: var(--warn); }
  .metric[data-tier='warn'] .spark-fill { fill: color-mix(in oklab, var(--warn) 14%, transparent); }
  .metric[data-tier='warn'] .m-value { color: var(--warn); }
  .metric[data-tier='bad'] .spark-line { stroke: var(--bad); }
  .metric[data-tier='bad'] .spark-fill { fill: color-mix(in oklab, var(--bad) 16%, transparent); }
  .metric[data-tier='bad'] .m-value { color: var(--bad); }

  /* ---------- sparkline ---------- */
  .spark {
    width: 100%;
    height: 44px;
    display: block;
    overflow: visible;
  }
  .spark-line {
    fill: none;
    stroke-width: 1.4;
    stroke-linejoin: round;
    stroke-linecap: round;
    vector-effect: non-scaling-stroke;
  }
  .spark-fill { stroke: none; }
  .spark-dual .spark-r { stroke: var(--accent); }
  .spark-dual .spark-w { stroke: var(--bad); opacity: 0.85; }

  /* ---------- disk IO row ---------- */
  .io-row { display: flex; gap: 0.85rem; margin-top: -0.1rem; }
  .io { display: inline-flex; gap: 0.3rem; align-items: baseline; }
  .io-r { color: var(--accent); }
  .io-w { color: var(--bad); }

  /* ---------- per-core grid ---------- */
  .cores-card { margin-bottom: 0.85rem; }
  .cores {
    /* Default columns = however many cores the host reports. The mobile
       breakpoints clamp this to 8 so a 32-core box doesn't shrink to
       sub-pixel bars. */
    display: grid;
    grid-template-columns: repeat(var(--cores, 16), minmax(0, 1fr));
    gap: 0.35rem;
    align-items: end;
  }
  .core {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.25rem;
    min-width: 0;
  }
  .core-bar {
    width: 100%;
    height: 48px;
    background: linear-gradient(
      to top,
      var(--accent) 0%,
      var(--accent) var(--pct),
      rgba(94, 234, 212, 0.07) var(--pct),
      rgba(94, 234, 212, 0.07) 100%
    );
    border-radius: 3px;
    transition: background 220ms linear;
  }
  .core-idx { font-size: 0.65rem; }

  /* ---------- jobs row ---------- */
  .jobs-row { grid-template-columns: 1.4fr 1fr; }
  .counters {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.6rem;
    margin-bottom: 0.85rem;
  }
  .counter {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    padding: 0.55rem 0.65rem;
    background: var(--bg-elev-2);
    border-radius: var(--radius);
    border: 1px solid var(--hairline);
  }
  .c-num {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 500;
    font-size: 1.55rem;
    line-height: 1;
    color: var(--fg);
    font-variant-numeric: tabular-nums;
  }
  .c-bad { color: var(--bad); }
  .c-lbl {
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.6rem;
  }

  .idle { margin: 0.4rem 0 0.1rem; }
  .active-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }
  .active-job {
    padding: 0.55rem 0.7rem 0.65rem;
    background: var(--bg-elev-2);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    display: grid;
    grid-template-columns: 1fr auto;
    column-gap: 0.6rem;
    row-gap: 0.25rem;
  }
  .aj-top { display: flex; justify-content: space-between; gap: 0.6rem; grid-column: 1 / -1; }
  .aj-target {
    font-family: var(--font-display);
    font-weight: 500;
    color: var(--fg);
    letter-spacing: -0.01em;
  }
  .aj-target:hover { color: var(--accent); }
  .aj-meta { grid-column: 1 / -1; }
  .progress {
    grid-column: 1 / 2;
    height: 4px;
    background: rgba(94, 234, 212, 0.08);
    border-radius: 999px;
    overflow: hidden;
    align-self: center;
  }
  .progress-fill {
    width: var(--pct);
    height: 100%;
    background: linear-gradient(90deg, var(--accent), color-mix(in oklab, var(--accent) 60%, var(--bad)));
    transition: width 600ms ease-out;
  }
  .aj-pct { grid-column: 2 / 3; align-self: center; min-width: 2.6rem; text-align: right; }

  /* ---------- throughput ---------- */
  .thru-bars {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 4px;
    align-items: end;
    height: 90px;
    margin: 0.2rem 0 0.35rem;
  }
  .thru-bar {
    height: var(--h);
    min-height: 2px;
    background: rgba(94, 234, 212, 0.22);
    border-radius: 2px 2px 0 0;
    transition: height 380ms cubic-bezier(0.2, 0.8, 0.2, 1), background 200ms ease;
  }
  .thru-bar[data-now='true'] {
    background: var(--accent);
  }
  .thru-axis {
    display: flex;
    justify-content: space-between;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  /* ---------- responsive ---------- */
  @media (max-width: 760px) {
    .jobs-row { grid-template-columns: 1fr; }
    .cores { grid-template-columns: repeat(8, minmax(0, 1fr)); }
    .core-bar { height: 38px; }
    .counters { grid-template-columns: repeat(2, 1fr); }
    .m-value { font-size: 2rem; }
  }
  @media (max-width: 420px) {
    .cores { grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 0.3rem; }
    .core-idx { display: none; }
    .core-bar { height: 32px; }
    .page-head h1 { font-size: 1.3rem; }
  }
</style>
