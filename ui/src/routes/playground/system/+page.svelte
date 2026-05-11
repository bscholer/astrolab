<!--
  System dashboard mock. Mirrors the real /system route we'll build next.
  All data fabricated client-side and ticked on a 1.5s interval, matching
  the /jobs polling cadence. Playground is `bare` so we paint our own
  topbar to match the real layout.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  // ---------- types (mirror the eventual /api/system shape) ----------
  type CpuSnapshot = {
    percent: number;          // 0..100, overall
    per_core: number[];       // length = cores
    load_avg: [number, number, number];
    temp_c: number | null;
  };
  type MemSnapshot = {
    used: number;             // bytes
    total: number;            // bytes
    swap_used: number;        // bytes
  };
  type DiskSnapshot = {
    mount: string;
    used: number;
    total: number;
    read_bps: number;
    write_bps: number;
  };
  type GpuSnapshot = {
    model: string;
    util: number;             // 0..100
    vram_used: number;
    vram_total: number;
    temp_c: number;
  } | null;
  type RunningJob = {
    id: string;
    target: string;
    template: string;
    progress: number;         // 0..1
    started_at: number;       // ms
  };
  type SystemSnapshot = {
    host: {
      hostname: string;
      os: string;
      kernel: string;
      cpu_model: string;
      cores: number;
      ram_total: number;
      disk_total: number;
      gpu_model: string | null;
      uptime_s: number;
    };
    cpu: CpuSnapshot;
    mem: MemSnapshot;
    disk: DiskSnapshot;
    gpu: GpuSnapshot;
    jobs: {
      queued: number;
      running: number;
      completed_24h: number;
      failed_24h: number;
      active: RunningJob[];
      throughput_6h: number[]; // 12 buckets, jobs/30min
    };
    sampled_at: number;        // ms epoch
  };

  // ---------- static host facts (faked once) ----------
  const host = {
    hostname: 'astrolab-stack',
    os: 'Debian 12 (bookworm)',
    kernel: '6.1.0-26-amd64',
    cpu_model: 'AMD Ryzen 7 5700X',
    cores: 16,
    ram_total: 64 * 1024 ** 3,
    disk_total: 4 * 1024 ** 4,
    gpu_model: 'NVIDIA RTX 4060 Ti 16GB',
    uptime_s: 412_337
  };

  // ---------- mutable snapshot ----------
  let snap = $state<SystemSnapshot>(initial());

  // Rolling history. ~120 samples at 1.5s = 3 min of sparkline.
  const N = 120;
  let cpuHist = $state<number[]>(seedHist(N, 18, 8));
  let memHist = $state<number[]>(seedHist(N, 42, 4));
  let gpuHist = $state<number[]>(seedHist(N, 35, 18));
  let diskReadHist = $state<number[]>(seedHist(N, 12, 10));
  let diskWriteHist = $state<number[]>(seedHist(N, 6, 6));

  // ---------- timers ----------
  let tickHandle: ReturnType<typeof setInterval> | null = null;
  let clockHandle: ReturnType<typeof setInterval> | null = null;
  let nowTick = $state(Date.now());

  onMount(() => {
    tickHandle = setInterval(step, 1500);
    clockHandle = setInterval(() => (nowTick = Date.now()), 1000);
  });
  onDestroy(() => {
    if (tickHandle) clearInterval(tickHandle);
    if (clockHandle) clearInterval(clockHandle);
  });

  // ---------- helpers ----------
  function seedHist(n: number, base: number, spread: number): number[] {
    const out: number[] = [];
    let v = base;
    for (let i = 0; i < n; i++) {
      v = clamp(v + (Math.random() - 0.5) * spread, 0, 100);
      out.push(v);
    }
    return out;
  }

  function clamp(x: number, lo: number, hi: number) {
    return Math.max(lo, Math.min(hi, x));
  }

  function initial(): SystemSnapshot {
    const cores = host.cores;
    const per_core = Array.from({ length: cores }, () => clamp(15 + Math.random() * 25, 0, 100));
    return {
      host,
      cpu: {
        percent: avg(per_core),
        per_core,
        load_avg: [2.1, 1.8, 1.6],
        temp_c: 54
      },
      mem: {
        used: 0.42 * host.ram_total,
        total: host.ram_total,
        swap_used: 0
      },
      disk: {
        mount: '/srv/astrolab',
        used: 0.61 * host.disk_total,
        total: host.disk_total,
        read_bps: 12 * 1024 ** 2,
        write_bps: 4 * 1024 ** 2
      },
      gpu: {
        model: host.gpu_model!,
        util: 28,
        vram_used: 4.2 * 1024 ** 3,
        vram_total: 16 * 1024 ** 3,
        temp_c: 49
      },
      jobs: {
        queued: 2,
        running: 1,
        completed_24h: 37,
        failed_24h: 2,
        active: [
          {
            id: 'j-1031',
            target: 'NGC 7000',
            template: 'calibrate_register_stack',
            progress: 0.34,
            started_at: Date.now() - 6 * 60_000
          }
        ],
        throughput_6h: [3, 5, 4, 7, 6, 4, 8, 5, 6, 4, 3, 5]
      },
      sampled_at: Date.now()
    };
  }

  function avg(xs: number[]) {
    return xs.reduce((a, b) => a + b, 0) / xs.length;
  }

  // Drift a value with a target band. A "stacker" job will push it higher.
  function drift(v: number, target: number, spread: number) {
    const pull = (target - v) * 0.08;
    const jitter = (Math.random() - 0.5) * spread;
    return clamp(v + pull + jitter, 0, 100);
  }

  // 1.5s tick: nudge all metrics, advance jobs, rotate history.
  function step() {
    // Job under load drives CPU/GPU/disk targets up.
    const loaded = snap.jobs.active.length > 0;
    const cpuTarget = loaded ? 72 : 14;
    const gpuTarget = loaded ? 65 : 12;
    const memTarget = loaded ? 58 : 38;

    const per_core = snap.cpu.per_core.map((v) => drift(v, cpuTarget, 14));
    const cpuPct = avg(per_core);
    cpuHist = [...cpuHist.slice(1), cpuPct];

    const memUsedFrac = drift((snap.mem.used / snap.mem.total) * 100, memTarget, 1.5);
    const memUsed = (memUsedFrac / 100) * snap.mem.total;
    memHist = [...memHist.slice(1), memUsedFrac];

    let gpu = snap.gpu;
    if (gpu) {
      const util = drift(gpu.util, gpuTarget, 12);
      const vramFrac = drift((gpu.vram_used / gpu.vram_total) * 100, loaded ? 55 : 26, 1.5);
      gpu = {
        ...gpu,
        util,
        vram_used: (vramFrac / 100) * gpu.vram_total,
        temp_c: clamp(gpu.temp_c + (Math.random() - 0.5) * 0.6 + (loaded ? 0.05 : -0.05), 38, 78)
      };
      gpuHist = [...gpuHist.slice(1), util];
    }

    const read_bps = clamp(snap.disk.read_bps + (Math.random() - 0.5) * 8e6, 0, 800e6);
    const write_bps = clamp(snap.disk.write_bps + (Math.random() - 0.5) * 5e6, 0, 800e6);
    diskReadHist = [...diskReadHist.slice(1), (read_bps / 200e6) * 100];
    diskWriteHist = [...diskWriteHist.slice(1), (write_bps / 200e6) * 100];

    const active = snap.jobs.active
      .map((j) => ({ ...j, progress: clamp(j.progress + 0.005 + Math.random() * 0.004, 0, 1) }))
      .filter((j) => j.progress < 1);

    // Occasionally spawn a new job to keep the dashboard interesting.
    let queued = snap.jobs.queued;
    let running = active.length;
    if (active.length === 0 && Math.random() < 0.04) {
      active.push({
        id: 'j-' + Math.floor(1000 + Math.random() * 9000),
        target: pick(['M 31', 'NGC 7000', 'IC 1396', 'M 42', 'Veil Nebula', 'M 81']),
        template: pick(['calibrate_register_stack', 'hoo_dwarf3_dualband']),
        progress: 0.02,
        started_at: Date.now()
      });
      running = 1;
      queued = Math.max(0, queued - 1);
    }

    snap = {
      ...snap,
      cpu: {
        percent: cpuPct,
        per_core,
        load_avg: [
          drift01(snap.cpu.load_avg[0], loaded ? 6.5 : 1.4, 0.4),
          drift01(snap.cpu.load_avg[1], loaded ? 5.5 : 1.6, 0.2),
          drift01(snap.cpu.load_avg[2], loaded ? 4.5 : 1.7, 0.1)
        ],
        temp_c: snap.cpu.temp_c == null
          ? null
          : clamp(snap.cpu.temp_c + (Math.random() - 0.5) * 0.8 + (loaded ? 0.06 : -0.06), 38, 84)
      },
      mem: { ...snap.mem, used: memUsed },
      disk: { ...snap.disk, read_bps, write_bps },
      gpu,
      jobs: {
        ...snap.jobs,
        queued,
        running,
        active,
        // Drift the latest bucket so the histogram visibly breathes.
        throughput_6h: snap.jobs.throughput_6h.map((v, i) =>
          i === snap.jobs.throughput_6h.length - 1
            ? clamp(v + (Math.random() - 0.4) * 1.2, 0, 12)
            : v
        )
      },
      sampled_at: Date.now()
    };
  }

  function drift01(v: number, target: number, spread: number) {
    const pull = (target - v) * 0.05;
    return Math.max(0, v + pull + (Math.random() - 0.5) * spread);
  }

  function pick<T>(xs: T[]): T {
    return xs[Math.floor(Math.random() * xs.length)];
  }

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

  function fmtElapsed(ms: number): string {
    const s = Math.floor((nowTick - ms) / 1000);
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m === 0) return `${r}s`;
    return `${m}m ${r}s`;
  }

  // Pressure tier for the big CPU/RAM/Disk numbers. Drives the color.
  function tier(pct: number): 'ok' | 'warn' | 'bad' {
    if (pct >= 90) return 'bad';
    if (pct >= 70) return 'warn';
    return 'ok';
  }

  // ---------- sparkline geometry ----------
  // Tight 30-line polyline for the small charts. We use only viewBox so
  // they scale to whatever the card gives them.
  function sparkPath(values: number[], w = 200, h = 40, min = 0, max = 100): string {
    if (values.length === 0) return '';
    const range = Math.max(1, max - min);
    const dx = w / (values.length - 1);
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
    return (
      sparkPath(values, w, h, min, max) +
      ` L${w},${h} L0,${h} Z`
    );
  }

  // ---------- derived ----------
  const memPct = $derived((snap.mem.used / snap.mem.total) * 100);
  const diskPct = $derived((snap.disk.used / snap.disk.total) * 100);
  const gpuMemPct = $derived(snap.gpu ? (snap.gpu.vram_used / snap.gpu.vram_total) * 100 : 0);
  const thru = $derived(snap.jobs.throughput_6h);
  const thruMax = $derived(Math.max(4, ...thru));
  const thruTotal = $derived(thru.reduce((a, b) => a + b, 0));
</script>

<svelte:head>
  <title>System · astrolab</title>
</svelte:head>

<header class="container topbar">
  <a href="/" class="logo">astrolab</a>
  <nav class="nav">
    <a href="/">Library</a>
    <a href="/tonight">Tonight</a>
    <a href="/projects">Projects</a>
    <a href="/gallery">Gallery</a>
    <a href="/settings">Settings</a>
    <a class="active" href="/playground/system">System</a>
  </nav>
</header>

<main class="container">
  <div class="page-head">
    <h1>System</h1>
    <span class="dot live" aria-hidden="true"></span>
    <span class="muted small mono">{snap.host.hostname} · up {fmtUptime(snap.host.uptime_s)}</span>
    <span class="sep" aria-hidden="true"></span>
    <span class="muted small mono">sampled {new Date(snap.sampled_at).toLocaleTimeString()}</span>
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
    <div class="cores">
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
                <a class="aj-target" href="/jobs/{j.id}" title={j.id}>{j.target}</a>
                <span class="muted small mono">{fmtElapsed(j.started_at)}</span>
              </div>
              <div class="aj-meta muted small mono">{j.template}</div>
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
</main>

<style>
  /* ---------- topbar (copy of real layout, since playground is bare) ---------- */
  .topbar {
    padding-bottom: 0.25rem;
    padding-top: 0.85rem;
    display: flex;
    align-items: baseline;
    gap: 1.5rem;
  }
  .logo {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 500;
    font-size: 1.25rem;
    letter-spacing: -0.005em;
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .nav { display: flex; gap: 1.25rem; flex: 1; flex-wrap: wrap; }
  .nav a {
    color: var(--fg-mute);
    font-size: 0.9rem;
    position: relative;
    padding: 0.25rem 0;
    transition: color 160ms ease;
  }
  .nav a:hover { color: var(--fg); }
  .nav a.active { color: var(--fg); }
  .nav a.active::after {
    content: '';
    position: absolute;
    left: 0; right: 0; bottom: -2px;
    height: 1px;
    background: linear-gradient(90deg, var(--accent), var(--bad));
    opacity: 0.7;
  }

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
  .dot.live {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 0 var(--accent-soft);
    animation: pulse 2.2s ease-out infinite;
    transform: translateY(-2px);
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
    display: grid;
    grid-template-columns: repeat(16, minmax(0, 1fr));
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
    .nav { gap: 0.85rem; }
    .nav a { font-size: 0.82rem; }
    .page-head h1 { font-size: 1.3rem; }
  }
</style>
