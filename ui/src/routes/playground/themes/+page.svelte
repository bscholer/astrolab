<!--
  Theme playground. Pick a theme up top, eyeball the components below.
  All dummy data, no API calls — works on Mac with just `vite dev`.
-->
<script lang="ts">
  type Theme = 'astrolab' | 'observatory' | 'linear-noir' | 'narrowband';
  let theme = $state<Theme>('astrolab');

  const themes: { id: Theme; name: string; tagline: string }[] = [
    { id: 'astrolab', name: 'Astrolab', tagline: 'narrowband palette · serif highlights' },
    { id: 'observatory', name: 'Observatory', tagline: 'serif headings · warm amber · roomy' },
    { id: 'linear-noir', name: 'Linear-noir', tagline: 'tight grotesk · violet · dense' },
    { id: 'narrowband', name: 'Narrowband', tagline: 'glow & gradient · teal+magenta' }
  ];
</script>

<div class="root" data-theme={theme}>
  <div class="shell">
    <header class="top">
      <div class="brand">
        <span class="logo">astrolab</span>
        <span class="muted small">theme playground</span>
      </div>
      <div class="theme-picker" role="tablist">
        {#each themes as t (t.id)}
          <button
            type="button"
            class="theme-tab"
            aria-pressed={theme === t.id}
            onclick={() => (theme = t.id)}
          >
            <span class="theme-name">{t.name}</span>
            <span class="theme-tag muted small">{t.tagline}</span>
          </button>
        {/each}
      </div>
    </header>

    <!-- Buttons -->
    <section class="block">
      <h3>Buttons</h3>
      <div class="row">
        <button class="btn primary">Run pipeline</button>
        <button class="btn">Save budget</button>
        <button class="btn ghost">Run cleanup now</button>
        <button class="btn danger">Delete</button>
        <button class="btn primary" disabled>Submitting…</button>
      </div>
    </section>

    <!-- Inputs -->
    <section class="block">
      <h3>Inputs</h3>
      <div class="row">
        <input class="text" placeholder="Capture root (e.g. ~/Pictures/Siril)" />
        <select class="select">
          <option>calibrate_register_stack</option>
          <option>hoo_dwarf3_dualband</option>
        </select>
        <input class="num" type="number" value="1000" />
        <span class="muted small">GiB</span>
      </div>
      <div class="row">
        <input class="range" type="range" min="0" max="100" value="60" />
      </div>
    </section>

    <!-- Cards (target / session / project) -->
    <section class="block">
      <h3>Library — target card (open)</h3>
      <div class="target open">
        <div class="target-row">
          <div class="target-name">
            <span class="target-common">Andromeda Galaxy</span>
            <span class="target-cat muted">M 31</span>
          </div>
          <div class="target-meta muted small">
            <span>2 sessions</span><span aria-hidden="true">·</span>
            <span>112 frames</span><span aria-hidden="true">·</span>
            <span class="fail-pct">3% failed</span><span aria-hidden="true">·</span>
            <span>last 2025-10-21</span>
          </div>
        </div>
        <div class="target-detail">
          <div class="session">
            <div class="session-head">
              <span class="session-when">2025-10-21</span>
              <span class="session-tags muted small">30s · gain 60 · Astro</span>
            </div>
            <div class="session-body">
              <span class="small">111 frames · <span class="fail-pct">3% failed</span></span>
              <span class="cal-row">
                <span class="cal cal-exact" title="dark · exact">D</span>
                <span class="cal cal-exact" title="flat · exact">F</span>
                <span class="cal cal-approx" title="bias · approx">B</span>
              </span>
              <button class="run-btn">Run…</button>
            </div>
          </div>
          <div class="session">
            <div class="session-head">
              <span class="session-when">2025-11-21</span>
              <span class="session-tags muted small">60s · gain 60 · Duo-Band</span>
            </div>
            <div class="session-body">
              <span class="small">1,167 frames · <span class="fail-pct fail-high">65% failed</span></span>
              <span class="cal-row">
                <span class="cal cal-none" title="dark · none">D</span>
                <span class="cal cal-exact" title="flat · exact">F</span>
                <span class="cal cal-none" title="bias · none">B</span>
              </span>
              <button class="run-btn">Run…</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="block">
      <h3>Project row</h3>
      <ul class="list">
          <li class="prow" style="--stagger: 0">
            <div class="prow-link">
              <div class="prow-name">
                Triangulum Galaxy
                <span class="prow-cat muted">M 33</span>
                <span class="version-chip">v14</span>
              </div>
              <div class="prow-template">Calibrate · Register · Stack</div>
              <div class="prow-meta muted small">
                <span class="meta-strong">185 frames</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="fail-pct">2% failed</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>2 sessions</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>30s · gain 60</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="filter-pill astro">Astro</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>Oct 21 → Nov 21</span>
              </div>
              <div class="prow-foot muted small">
                <span>edited 2h ago</span>
              </div>
            </div>
            <div class="prow-side">
              <div class="prow-storage">
                <span class="storage-main">123 GiB</span>
                <span class="storage-sub muted">+17 GiB shared</span>
              </div>
              <div class="prow-actions">
              <button class="action-btn icon-btn" aria-label="Free intermediates" title="Free intermediates">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <!-- handle -->
                  <path d="M20 3 L12 11" />
                  <!-- band where stick meets bristles -->
                  <path d="M10 9 L14 13" />
                  <!-- brush wedge hanging down-left -->
                  <path d="M10 9 L3 16" />
                  <path d="M14 13 L8 21" />
                  <path d="M3 16 L8 21" />
                  <!-- a couple inner bristles -->
                  <path d="M7 13 L5 18" />
                  <path d="M10 16 L8 20" />
                </svg>
              </button>
              <button class="action-btn icon-btn danger" aria-label="Delete" title="Delete">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  <path d="M10 11v6" />
                  <path d="M14 11v6" />
                </svg>
              </button>
              </div>
            </div>
          </li>
          <li class="prow" style="--stagger: 1">
            <div class="prow-link">
              <div class="prow-name">
                Wizard Nebula
                <span class="prow-cat muted">NGC 7380</span>
                <span class="version-chip">v2</span>
              </div>
              <div class="prow-template">HOO Recombine</div>
              <div class="prow-meta muted small">
                <span class="meta-strong">79 frames</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="fail-pct fail-zero">0% failed</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>1 session</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>60s · gain 60</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="filter-pill duoband">Duo-Band</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>Oct 18</span>
              </div>
              <div class="prow-foot muted small">
                <span>edited 3h ago</span>
              </div>
            </div>
            <div class="prow-side">
              <div class="prow-storage">
                <span class="storage-main">22 GiB</span>
              </div>
              <div class="prow-actions">
              <button class="action-btn icon-btn" aria-label="Free intermediates" title="Free intermediates">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <!-- handle -->
                  <path d="M20 3 L12 11" />
                  <!-- band where stick meets bristles -->
                  <path d="M10 9 L14 13" />
                  <!-- brush wedge hanging down-left -->
                  <path d="M10 9 L3 16" />
                  <path d="M14 13 L8 21" />
                  <path d="M3 16 L8 21" />
                  <!-- a couple inner bristles -->
                  <path d="M7 13 L5 18" />
                  <path d="M10 16 L8 20" />
                </svg>
              </button>
              <button class="action-btn icon-btn danger" aria-label="Delete" title="Delete">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  <path d="M10 11v6" />
                  <path d="M14 11v6" />
                </svg>
              </button>
              </div>
            </div>
          </li>
          <li class="prow" style="--stagger: 2">
            <div class="prow-link">
              <div class="prow-name">
                Andromeda Galaxy
                <span class="prow-cat muted">M 31</span>
                <span class="version-chip">v5</span>
              </div>
              <div class="prow-template">Calibrate · Register · Stack</div>
              <div class="prow-meta muted small">
                <span class="meta-strong">108 frames</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="fail-pct">3% failed</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>1 session</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>15s · gain 60</span>
                <span class="dot" aria-hidden="true">·</span>
                <span class="filter-pill astro">Astro</span>
                <span class="dot" aria-hidden="true">·</span>
                <span>Oct 21</span>
              </div>
              <div class="prow-foot muted small">
                <span>edited 1d ago</span>
              </div>
            </div>
            <div class="prow-side">
              <div class="prow-storage">
                <span class="storage-main">252 KiB</span>
              </div>
              <div class="prow-actions">
              <button class="action-btn icon-btn" aria-label="Free intermediates" title="Free intermediates">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <!-- handle -->
                  <path d="M20 3 L12 11" />
                  <!-- band where stick meets bristles -->
                  <path d="M10 9 L14 13" />
                  <!-- brush wedge hanging down-left -->
                  <path d="M10 9 L3 16" />
                  <path d="M14 13 L8 21" />
                  <path d="M3 16 L8 21" />
                  <!-- a couple inner bristles -->
                  <path d="M7 13 L5 18" />
                  <path d="M10 16 L8 20" />
                </svg>
              </button>
              <button class="action-btn icon-btn danger" aria-label="Delete" title="Delete">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M3 6h18" />
                  <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                  <path d="M10 11v6" />
                  <path d="M14 11v6" />
                </svg>
              </button>
              </div>
            </div>
          </li>
      </ul>
    </section>

    <section class="block">
      <h3>Pipeline strip card</h3>
      <ol class="flow-strip">
          <li class="flow-card flow-completed">
            <div class="flow-preview" style="background: linear-gradient(135deg, #2a1f10, #694e26 60%, #b58a3f);"></div>
            <div class="flow-overlay">
              <span class="flow-title">Calibrate</span>
              <span class="status status-mini status-completed">cached</span>
            </div>
          </li>
          <li class="flow-card flow-running">
            <div class="flow-preview" style="background: radial-gradient(circle at 30% 40%, #1a3550, #060a14 70%);"></div>
            <div class="flow-overlay">
              <span class="flow-title">Stack</span>
              <span class="status status-mini status-running">running</span>
            </div>
            <div class="flow-progress" style="width: 64%"></div>
          </li>
          <li class="flow-card flow-pending">
            <div class="flow-preview placeholder muted small">pending</div>
            <div class="flow-overlay">
              <span class="flow-title">Stretch</span>
              <span class="status status-mini status-pending">pending</span>
            </div>
          </li>
      </ol>
    </section>

    <!-- Status pills + cal badges + modified pill -->
    <section class="block">
      <h3>Pills & badges</h3>
      <div class="row">
        <span class="status status-pending">pending</span>
        <span class="status status-running">running</span>
        <span class="status status-cached">cached</span>
        <span class="status status-completed">completed</span>
        <span class="status status-failed">failed</span>
        <span class="badge-modified">2 modified</span>
        <span class="storage-pill">cache 236 GiB</span>
      </div>
      <div class="row">
        <span class="cal cal-exact">D</span>
        <span class="cal cal-approx">F</span>
        <span class="cal cal-none">B</span>
        <span class="muted small">calibration: exact / approx / none</span>
      </div>
    </section>

    <!-- Settings panel -->
    <section class="block">
      <h3>Settings panel sample</h3>
      <section class="panel">
        <h4 class="panel-h">Storage</h4>
        <div class="stats">
          <div class="stat">
            <span class="stat-label">cache total</span>
            <span class="stat-val">236 GiB</span>
            <span class="stat-sub muted small">32 entries</span>
          </div>
          <div class="stat">
            <span class="stat-label">unreachable</span>
            <span class="stat-val">0 B</span>
            <span class="stat-sub muted small">0 dead entries</span>
          </div>
          <div class="stat">
            <span class="stat-label">cache root</span>
            <code class="stat-path muted small">/home/bscholer/.local/share/astrolab/cache</code>
          </div>
        </div>
      </section>
    </section>

    <!-- Jobs table -->
    <section class="block">
      <h3>Jobs table</h3>
      <table class="jobs">
        <thead>
          <tr>
            <th>Target</th>
            <th>Capture</th>
            <th>Status</th>
            <th>Duration</th>
            <th class="muted">Submitted</th>
          </tr>
        </thead>
        <tbody>
          <tr style="--stagger: 0"><td><a class="target-link" href="/playground/themes#">Triangulum Galaxy <span class="muted small target-cat-inline">M 33</span></a></td><td class="muted small">185 × 30s · gain 60 · Astro</td><td><span class="status status-completed">completed</span></td><td class="muted small">3m 45s</td><td class="muted small">2h ago</td></tr>
          <tr style="--stagger: 1"><td><a class="target-link" href="/playground/themes#">Wizard Nebula <span class="muted small target-cat-inline">NGC 7380</span></a></td><td class="muted small">79 × 30s · gain 60 · Duo-Band</td><td><span class="status status-running">running</span></td><td class="muted small">1m 24s</td><td class="muted small">4h ago</td></tr>
          <tr style="--stagger: 2"><td><a class="target-link" href="/playground/themes#">Triangulum Galaxy <span class="muted small target-cat-inline">M 33</span></a></td><td class="muted small">185 × 30s · gain 60 · Astro</td><td><span class="status status-failed">failed</span></td><td class="muted small">&lt;1s</td><td class="muted small">3h ago</td></tr>
        </tbody>
      </table>
    </section>

    <!-- Type sample -->
    <section class="block">
      <h3>Type</h3>
      <div class="type-sample">
        <h1>Andromeda Galaxy</h1>
        <p class="muted">2 sessions · 109 frames captured · last 2025-10-21</p>
        <p>
          A typed DAG of cacheable nodes. Stacking is just one (expensive) node;
          stretches, palette recombines, denoise, etc. are cheap downstream nodes.
        </p>
        <code class="path">/home/bscholer/.local/share/astrolab/cache/5b4dc1d9d562caa406bef4d38c10120557ff/stage.png</code>
      </div>
    </section>
  </div>
</div>

<style>
  /* =========================================================
     ASTROLAB — narrowband palette, Fraunces used sparingly
     ========================================================= */

  .root[data-theme='astrolab'] {
    --bg: #02030a;
    --bg-elev: #0a0f1c;
    --bg-elev-2: #111a2e;
    --fg: #e9f1ff;
    --fg-mute: #6f8099;
    --accent: #5eead4;
    --accent-soft: rgba(94, 234, 212, 0.14);
    --accent-ink: #022a23;
    --good: #34d399;
    --warn: #fbbf24;
    --bad: #ec4899;
    --border: rgba(94, 234, 212, 0.14);
    --border-strong: rgba(236, 72, 153, 0.45);
    --hairline: rgba(94, 234, 212, 0.06);

    --radius: 8px;
    --radius-card: 12px;
    --pad-card: 1rem 1.15rem;
    --pad-block: 1.15rem;
    --gap-rows: 0.55rem;
    --shell-pad: 2rem 1.5rem 5rem;
    --shell-max: 1080px;

    --font-display: 'Fraunces', 'Times New Roman', serif;
    --font-body: 'Inter', -apple-system, system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, monospace;

    --shadow:
      0 0 0 1px rgba(94, 234, 212, 0.04),
      0 16px 36px rgba(0, 0, 0, 0.5),
      0 0 32px rgba(94, 234, 212, 0.025);
    --bg-decoration:
      radial-gradient(1100px 550px at 8% -10%, rgba(94, 234, 212, 0.07), transparent 60%),
      radial-gradient(900px 500px at 95% 110%, rgba(236, 72, 153, 0.05), transparent 65%);
  }

  /* Sparing Fraunces: only on the things that benefit from serif character —
     page H1, target/project names, primary panel headings. Block sub-heads,
     buttons, body, numbers stay sans/mono. */
  .root[data-theme='astrolab'] .target-name,
  .root[data-theme='astrolab'] .prow-name,
  .root[data-theme='astrolab'] .target-link,
  .root[data-theme='astrolab'] .type-sample h1 {
    font-family: var(--font-display);
    font-weight: 500;
    letter-spacing: -0.01em;
  }
  .root[data-theme='astrolab'] .target-name { font-size: 1.35rem; }
  .root[data-theme='astrolab'] .prow-name { font-size: 1.15rem; }
  .root[data-theme='astrolab'] .type-sample h1 {
    font-size: 2.4rem;
    /* keep the gradient from narrowband but anchored on serif */
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    font-weight: 600;
  }
  .root[data-theme='astrolab'] .target.open {
    box-shadow:
      0 0 0 1px var(--accent-soft),
      0 0 28px rgba(94, 234, 212, 0.08),
      0 16px 36px rgba(0, 0, 0, 0.5);
  }
  .root[data-theme='astrolab'] .flow-card.flow-running {
    box-shadow: 0 0 0 1px var(--accent), 0 0 24px var(--accent-soft);
  }
  .root[data-theme='astrolab'] .prow,
  .root[data-theme='astrolab'] .panel {
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
  }
  .root[data-theme='astrolab'] .btn.primary {
    background: linear-gradient(135deg, var(--accent), #34d399);
    box-shadow:
      0 0 0 1px rgba(94, 234, 212, 0.3),
      0 0 18px rgba(94, 234, 212, 0.22);
    border-color: transparent;
  }
  .root[data-theme='astrolab'] .logo {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 500;
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }

  /* =========================================================
     OBSERVATORY — warm slate, serif headings, roomy hairlines
     ========================================================= */

  .root[data-theme='observatory'] {
    --bg: #0a0907;
    --bg-elev: #14110d;
    --bg-elev-2: #1c1812;
    --fg: #ece7dd;
    --fg-mute: #968b7a;
    --accent: #f3b14a;
    --accent-soft: rgba(243, 177, 74, 0.16);
    --accent-ink: #1a1206;
    --good: #c5d28a;
    --warn: #f3b14a;
    --bad: #d97a6a;
    --border: rgba(243, 177, 74, 0.12);
    --border-strong: rgba(243, 177, 74, 0.28);
    --hairline: rgba(255, 255, 255, 0.05);

    --radius: 4px;
    --radius-card: 6px;
    --pad-card: 1rem 1.1rem;
    --pad-block: 1.1rem;
    --gap-rows: 0.55rem;
    --shell-pad: 2rem 2rem 5rem;
    --shell-max: 1080px;

    --font-display: 'Fraunces', 'Times New Roman', serif;
    --font-body: 'Inter', -apple-system, system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, monospace;

    --shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset, 0 12px 30px rgba(0, 0, 0, 0.45);
    --bg-decoration:
      radial-gradient(800px 400px at 80% -10%, rgba(243, 177, 74, 0.05), transparent 70%);
  }

  /* =========================================================
     LINEAR-NOIR — charcoal, dense, sharp corners
     ========================================================= */

  .root[data-theme='linear-noir'] {
    --bg: #08090b;
    --bg-elev: #111317;
    --bg-elev-2: #181b21;
    --fg: #f4f5f7;
    --fg-mute: #6e7682;
    --accent: #a78bfa;
    --accent-soft: rgba(167, 139, 250, 0.14);
    --accent-ink: #1a1530;
    --good: #4ade80;
    --warn: #fbbf24;
    --bad: #f87171;
    --border: rgba(255, 255, 255, 0.06);
    --border-strong: rgba(255, 255, 255, 0.14);
    --hairline: rgba(255, 255, 255, 0.05);

    --radius: 4px;
    --radius-card: 4px;
    --pad-card: 0.55rem 0.75rem;
    --pad-block: 0.6rem;
    --gap-rows: 0.25rem;
    --shell-pad: 1rem 1.1rem 4rem;
    --shell-max: 1100px;

    --font-display: 'Inter', -apple-system, system-ui, sans-serif;
    --font-body: 'Inter', -apple-system, system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, monospace;

    --shadow: 0 1px 0 rgba(255, 255, 255, 0.03) inset;
    --bg-decoration: none;
  }

  /* =========================================================
     NARROWBAND — deep void with glow + gradients
     ========================================================= */

  .root[data-theme='narrowband'] {
    --bg: #02030a;
    --bg-elev: #0a0f1c;
    --bg-elev-2: #111a2e;
    --fg: #e9f1ff;
    --fg-mute: #6f8099;
    --accent: #5eead4;
    --accent-soft: rgba(94, 234, 212, 0.14);
    --accent-ink: #022a23;
    --good: #34d399;
    --warn: #fbbf24;
    --bad: #ec4899;
    --border: rgba(94, 234, 212, 0.14);
    --border-strong: rgba(236, 72, 153, 0.45);
    --hairline: rgba(94, 234, 212, 0.06);

    --radius: 8px;
    --radius-card: 14px;
    --pad-card: 1.1rem 1.25rem;
    --pad-block: 1.25rem;
    --gap-rows: 0.6rem;
    --shell-pad: 2rem 1.5rem 5rem;
    --shell-max: 1080px;

    --font-display: 'Inter', -apple-system, system-ui, sans-serif;
    --font-body: 'Inter', -apple-system, system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, monospace;

    --shadow:
      0 0 0 1px rgba(94, 234, 212, 0.05),
      0 20px 40px rgba(0, 0, 0, 0.5),
      0 0 36px rgba(94, 234, 212, 0.03);
    --bg-decoration:
      radial-gradient(1200px 600px at 8% -10%, rgba(94, 234, 212, 0.08), transparent 60%),
      radial-gradient(900px 500px at 95% 110%, rgba(236, 72, 153, 0.06), transparent 65%),
      radial-gradient(600px 400px at 50% 50%, rgba(94, 234, 212, 0.02), transparent 70%);
  }

  /* =========================================================
     SHELL
     ========================================================= */

  .root {
    min-height: 100dvh;
    background: var(--bg-decoration), var(--bg);
    color: var(--fg);
    font-family: var(--font-body);
    -webkit-font-smoothing: antialiased;
    /* Cross-fade colors when the theme changes. */
    transition:
      background-color 360ms ease,
      color 360ms ease;
  }
  /* Smooth theme cross-fade for surfaces / borders too. */
  .target,
  .prow,
  .panel,
  .btn,
  .text,
  .select,
  .num,
  .status,
  .badge-modified,
  .storage-pill,
  .cal,
  .flow-card,
  .session,
  .action-btn,
  .run-btn {
    transition:
      background-color 320ms ease,
      border-color 220ms ease,
      color 220ms ease,
      box-shadow 220ms ease,
      transform 180ms ease;
  }

  /* Stagger fade-up: each row reads in turn so the page lands instead
     of slamming. --stagger inline-style indexes the row's delay. */
  @keyframes rise-in {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .prow,
  .jobs tbody tr {
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 60ms + 80ms);
  }

  @media (prefers-reduced-motion: reduce) {
    .prow,
    .jobs tbody tr { animation: none; }
    .root,
    .target, .prow, .panel, .btn, .text, .select, .num, .status,
    .badge-modified, .storage-pill, .cal, .flow-card, .session,
    .action-btn, .run-btn {
      transition: none;
    }
  }
  .shell {
    max-width: var(--shell-max);
    margin: 0 auto;
    padding: var(--shell-pad);
  }

  .top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 2rem;
    flex-wrap: wrap;
  }
  .brand { display: flex; align-items: baseline; gap: 0.6rem; }
  .logo {
    font-family: var(--font-display);
    font-weight: 600;
    font-size: 1.15rem;
    letter-spacing: 0.01em;
  }
  .root[data-theme='observatory'] .logo {
    font-style: italic;
    letter-spacing: -0.005em;
  }
  .root[data-theme='narrowband'] .logo {
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    font-weight: 700;
  }

  .theme-picker {
    display: flex;
    gap: 0.4rem;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    padding: 0.25rem;
    border-radius: 999px;
  }
  .theme-tab {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    padding: 0.4rem 0.9rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    line-height: 1.2;
    align-items: flex-start;
    gap: 0.05rem;
  }
  .theme-tab:hover { color: var(--fg); }
  .theme-tab[aria-pressed='true'] {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .theme-name { font-weight: 600; }
  .theme-tag { font-size: 0.65rem; }

  .block { margin-bottom: 2rem; }
  .block h3 {
    margin: 0 0 0.7rem;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg-mute);
    font-weight: 600;
  }
  .root[data-theme='observatory'] .block h3 {
    font-family: var(--font-display);
    font-style: italic;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.95rem;
    font-weight: 500;
    color: var(--fg);
  }

  .two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.25rem;
  }
  @media (max-width: 800px) {
    .two-col { grid-template-columns: 1fr; }
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }

  .muted { color: var(--fg-mute); }
  .small { font-size: 0.82rem; }

  /* =========================================================
     BUTTONS
     ========================================================= */

  .btn {
    appearance: none;
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    padding: 0.4rem 0.9rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
    transition: all 120ms ease;
  }
  .btn:hover:not(:disabled) {
    border-color: var(--border-strong);
  }
  .btn.primary {
    background: var(--accent);
    color: var(--accent-ink);
    border-color: var(--accent);
    font-weight: 600;
  }
  .btn.primary:hover:not(:disabled) {
    filter: brightness(1.08);
    transform: translateY(-1px);
  }
  .btn:active:not(:disabled) { transform: translateY(0); }
  .btn.ghost { background: transparent; }
  .btn.danger { color: var(--bad); }
  .btn.danger:hover:not(:disabled) { border-color: var(--bad); }
  .btn:disabled { opacity: 0.55; cursor: progress; }

  .root[data-theme='linear-noir'] .btn {
    border-radius: 5px;
    padding: 0.3rem 0.7rem;
    font-size: 0.8rem;
  }
  .root[data-theme='linear-noir'] .btn.primary {
    background: var(--accent);
    color: var(--accent-ink);
    box-shadow: 0 1px 0 rgba(255, 255, 255, 0.15) inset;
  }
  .root[data-theme='narrowband'] .btn.primary {
    background: linear-gradient(135deg, var(--accent), #34d399);
    box-shadow:
      0 0 0 1px rgba(94, 234, 212, 0.3),
      0 0 20px rgba(94, 234, 212, 0.25);
    border-color: transparent;
    border-radius: 999px;
  }

  /* =========================================================
     INPUTS
     ========================================================= */

  .text, .select, .num {
    background: var(--bg-elev);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.45rem 0.7rem;
    font: inherit;
    font-size: 0.9rem;
  }
  .text { flex: 1 1 280px; min-width: 0; }
  .num { width: 6rem; }
  .text:focus, .select:focus, .num:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  .range { flex: 1; accent-color: var(--accent); }

  .root[data-theme='observatory'] .text {
    border-radius: 999px;
    padding: 0.5rem 0.95rem;
  }

  /* =========================================================
     LIBRARY / TARGET / SESSION
     ========================================================= */

  .target {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    overflow: hidden;
    box-shadow: var(--shadow);
  }
  .target:hover {
    transform: translateY(-1px);
    border-color: var(--border-strong);
  }
  .target.open {
    border-color: var(--border-strong);
  }
  .root[data-theme='narrowband'] .target.open {
    box-shadow:
      0 0 0 1px var(--accent-soft),
      0 0 32px rgba(94, 234, 212, 0.1),
      0 20px 40px rgba(0, 0, 0, 0.5);
  }
  .target-row {
    padding: var(--pad-card);
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .target-name {
    font-weight: 600;
    font-size: 1.05rem;
    display: flex;
    gap: 0.5rem;
    align-items: baseline;
  }
  .root[data-theme='observatory'] .target-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.4rem;
    letter-spacing: -0.01em;
  }
  .root[data-theme='linear-noir'] .target-name {
    font-size: 0.92rem;
    font-weight: 600;
  }
  .root[data-theme='narrowband'] .target-name {
    font-size: 1.2rem;
    font-weight: 700;
  }
  .target-common { font-weight: inherit; }
  .target-cat {
    font-weight: 500;
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
  }
  .target-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; font-variant-numeric: tabular-nums; }

  .target-detail {
    border-top: 1px solid var(--hairline);
    padding: 0.6rem 1.1rem 1.1rem;
    display: flex;
    flex-direction: column;
    gap: var(--gap-rows);
  }
  .session {
    padding: 0.55rem 0.7rem;
    border-radius: var(--radius);
    background: var(--bg-elev-2);
    border: 1px solid var(--hairline);
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .root[data-theme='linear-noir'] .session {
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--hairline);
    border-radius: 0;
    padding: 0.4rem 0;
  }
  .root[data-theme='linear-noir'] .session:last-child { border-bottom: none; }
  .session-head { display: flex; gap: 0.6rem; align-items: baseline; flex-wrap: wrap; }
  .session-when {
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    font-family: var(--font-mono);
  }
  .session-tags { font-size: 0.78rem; }
  .session-body { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; font-size: 0.85rem; }
  .cal-row { display: inline-flex; gap: 0.3rem; margin-left: auto; }

  .run-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--accent);
    padding: 0.2rem 0.7rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .run-btn:hover { background: var(--accent-soft); border-color: var(--accent); }

  /* =========================================================
     CALIBRATION BADGES
     ========================================================= */

  .cal {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 50%;
    font-size: 0.65rem;
    font-weight: 600;
    border: 1px solid currentColor;
    font-family: var(--font-mono);
  }
  .cal-exact { color: var(--good); background: color-mix(in oklab, var(--good) 12%, transparent); }
  .cal-approx { color: var(--warn); background: color-mix(in oklab, var(--warn) 12%, transparent); }
  .cal-none { color: var(--bad); background: color-mix(in oklab, var(--bad) 10%, transparent); }

  .root[data-theme='linear-noir'] .cal {
    width: 1.1rem;
    height: 1.1rem;
    font-size: 0.55rem;
    border-radius: 3px;
  }

  /* =========================================================
     PROJECT ROWS
     ========================================================= */

  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: var(--gap-rows); }
  .prow {
    display: flex;
    align-items: flex-start;
    gap: 1rem;
    padding: var(--pad-card);
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
  }
  .prow-actions { align-items: flex-start; }
  .prow-side {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.5rem;
    flex-shrink: 0;
    padding-left: 0.5rem;
    border-left: 1px solid var(--hairline);
    align-self: stretch;
    justify-content: space-between;
  }
  .prow-storage {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.05rem;
    line-height: 1.15;
    text-align: right;
  }
  .storage-main {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 500;
    font-size: 0.9rem;
    color: var(--fg);
  }
  .storage-sub {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-size: 0.7rem;
  }

  /* Failure percentage — shouts at high values, whispers at low. */
  .fail-pct {
    color: var(--warn);
    font-variant-numeric: tabular-nums;
  }
  .fail-pct.fail-zero {
    color: var(--good);
    opacity: 0.85;
  }
  .fail-pct.fail-high {
    color: var(--bad);
    font-weight: 500;
  }
  .prow:hover {
    border-color: var(--border-strong);
    transform: translateY(-1px);
  }

  /* Version chip — sits inline next to the name. Stays compact so it
     reads as metadata, not a primary affordance. */
  .version-chip {
    margin-left: 0.5rem;
    padding: 0.05rem 0.45rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 500;
    color: var(--accent);
    background: var(--accent-soft);
    border: 1px solid var(--border);
    border-radius: 999px;
    line-height: 1.4;
    vertical-align: middle;
  }
  .prow-cat {
    margin-left: 0.45rem;
    font-size: 0.78rem;
    font-weight: 500;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0;
  }
  .target-cat-inline {
    margin-left: 0.4rem;
    font-family: var(--font-mono);
    font-weight: 500;
  }
  /* In the Astrolab/observatory themes the project name is a serif —
     pull the catalog code's baseline back so it doesn't visually
     dangle below the descenders. */
  .root[data-theme='astrolab'] .prow-cat,
  .root[data-theme='observatory'] .prow-cat {
    position: relative;
    top: -0.1em;
  }
  .root[data-theme='linear-noir'] .prow {
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--hairline);
    border-radius: 0;
    padding: 0.5rem 0.25rem;
  }
  .root[data-theme='linear-noir'] .prow:hover {
    background: var(--bg-elev);
    border-bottom-color: transparent;
  }
  .root[data-theme='narrowband'] .prow {
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
  }
  .prow-link { display: flex; flex-direction: column; gap: 0.18rem; flex: 1; min-width: 0; }
  .prow-name {
    font-weight: 600;
    font-size: 1.05rem;
  }
  .prow-template {
    font-size: 0.88rem;
    color: var(--fg);
    opacity: 0.85;
    font-variant: small-caps;
    letter-spacing: 0.02em;
  }
  .prow-foot {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    font-size: 0.78rem;
    opacity: 0.85;
  }
  .prow-meta .dot,
  .prow-foot .dot {
    opacity: 0.5;
  }
  .meta-strong {
    color: var(--fg);
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }
  /* Filter chip — narrowband / broadband at a glance. */
  .filter-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.7rem;
    letter-spacing: 0.02em;
    border: 1px solid currentColor;
    line-height: 1.4;
  }
  .filter-pill.astro {
    color: #a3d8ff;
    background: rgba(163, 216, 255, 0.10);
  }
  .filter-pill.duoband {
    color: var(--bad);
    background: color-mix(in oklab, var(--bad) 12%, transparent);
  }
  .root[data-theme='observatory'] .prow-name {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.15rem;
  }
  .prow-meta { display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .prow-actions { display: flex; gap: 0.4rem; flex-shrink: 0; }
  .action-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.6rem;
    border-radius: var(--radius);
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .action-btn:hover { color: var(--accent); border-color: var(--accent); }
  .action-btn.danger:hover { color: var(--bad); border-color: var(--bad); }

  .icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0;
  }
  .icon-btn svg { display: block; }
  .icon-btn:hover svg { transform: rotate(-6deg); }
  .icon-btn.danger:hover svg { transform: translateY(-1px); }
  .icon-btn svg { transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1); }

  /* =========================================================
     PIPELINE STRIP
     ========================================================= */

  .flow-strip {
    list-style: none; padding: 0; margin: 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.6rem;
  }
  .flow-card {
    position: relative;
    aspect-ratio: 16 / 9;
    background: var(--bg-elev-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    overflow: hidden;
  }
  .flow-card.flow-running {
    border-color: var(--accent);
  }
  /* Soft breathing glow on the running step — communicates "this is where
     attention is now" without being a hard spinner. */
  @keyframes flow-pulse {
    0%, 100% { box-shadow: 0 0 0 1px var(--accent), 0 0 16px var(--accent-soft); }
    50%      { box-shadow: 0 0 0 1px var(--accent), 0 0 32px var(--accent-soft); }
  }
  .flow-card.flow-running { animation: flow-pulse 2.4s ease-in-out infinite; }
  /* Indeterminate shimmer along the progress bar so it reads as alive
     even between progress updates. */
  @keyframes flow-shimmer {
    0%   { background-position: -120% 0; }
    100% { background-position: 220% 0; }
  }
  .flow-progress {
    background:
      linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.45), transparent) 0 0 / 30% 100% no-repeat,
      var(--accent);
    animation: flow-shimmer 1.6s linear infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .flow-card.flow-running,
    .flow-progress { animation: none; }
  }
  .root[data-theme='narrowband'] .flow-card.flow-running {
    box-shadow:
      0 0 0 1px var(--accent),
      0 0 24px var(--accent-soft);
  }
  .flow-card.flow-completed { border-color: var(--good); }
  .flow-card.flow-pending { opacity: 0.55; }
  .flow-preview { width: 100%; height: 100%; }
  .flow-preview.placeholder { display: flex; align-items: center; justify-content: center; }
  .flow-overlay {
    position: absolute; top: 0; left: 0; right: 0;
    padding: 0.4rem 0.6rem 0.6rem;
    background: linear-gradient(to bottom, rgba(0,0,0,0.7), rgba(0,0,0,0));
    color: #fff;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    pointer-events: none;
  }
  .flow-title {
    font-weight: 600;
    font-size: 0.95rem;
    text-shadow: 0 1px 2px rgba(0,0,0,0.6);
  }
  .root[data-theme='observatory'] .flow-title {
    font-family: var(--font-display);
    font-weight: 500;
  }
  .flow-progress {
    position: absolute; left: 0; bottom: 0; height: 3px; background: var(--accent);
  }

  /* =========================================================
     STATUS PILLS
     ========================================================= */

  .status {
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-family: var(--font-mono);
  }
  .root[data-theme='linear-noir'] .status {
    border-radius: 4px;
    text-transform: lowercase;
    letter-spacing: 0;
    font-size: 0.7rem;
    padding: 0.1rem 0.45rem;
  }
  .status-mini { font-size: 0.6rem; padding: 0.05rem 0.4rem; margin-left: auto; }
  .status-pending { background: rgba(255,255,255,0.06); color: var(--fg-mute); }
  .status-running { background: var(--accent-soft); color: var(--accent); }
  .status-cached { background: rgba(255,255,255,0.10); color: var(--fg-mute); }
  .status-completed { background: color-mix(in oklab, var(--good) 18%, transparent); color: var(--good); }
  .status-failed { background: color-mix(in oklab, var(--bad) 18%, transparent); color: var(--bad); }

  .badge-modified {
    background: var(--accent-soft);
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .storage-pill {
    background: var(--bg-elev);
    border: 1px solid var(--border);
    padding: 0.2rem 0.65rem;
    border-radius: 999px;
    font-size: 0.8rem;
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
  }

  /* =========================================================
     SETTINGS PANEL
     ========================================================= */

  .panel {
    padding: var(--pad-block) 1.2rem;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
  }
  .root[data-theme='linear-noir'] .panel {
    padding: 0.8rem 1rem;
  }
  .root[data-theme='narrowband'] .panel {
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
  }
  .panel-h {
    margin: 0 0 0.5rem;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
  }
  .root[data-theme='observatory'] .panel-h {
    font-family: var(--font-display);
    text-transform: none;
    letter-spacing: 0;
    font-style: italic;
    font-size: 1.05rem;
    color: var(--fg);
  }
  .stats { display: flex; flex-wrap: wrap; gap: 1.5rem; }
  .stat { display: flex; flex-direction: column; gap: 0.15rem; min-width: 8rem; }
  .stat-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-mute);
  }
  .stat-val {
    font-size: 1.6rem;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    font-family: var(--font-mono);
  }
  .root[data-theme='observatory'] .stat-val {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 2rem;
  }
  .root[data-theme='linear-noir'] .stat-val { font-size: 1.25rem; }
  .stat-path { font-family: var(--font-mono); word-break: break-all; }

  /* =========================================================
     JOBS TABLE
     ========================================================= */

  .jobs { width: 100%; border-collapse: collapse; }
  .jobs th, .jobs td {
    padding: 0.5rem 0.75rem;
    text-align: left;
    border-bottom: 1px solid var(--hairline);
    font-size: 0.9rem;
  }
  .root[data-theme='linear-noir'] .jobs th,
  .root[data-theme='linear-noir'] .jobs td {
    padding: 0.3rem 0.5rem;
    font-size: 0.82rem;
  }
  .jobs th {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-mute);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
  }
  .target-link {
    color: var(--fg);
    text-decoration: none;
    font-weight: 600;
  }
  .root[data-theme='observatory'] .target-link {
    font-family: var(--font-display);
    font-weight: 500;
  }
  .target-link:hover { color: var(--accent); }

  /* =========================================================
     TYPE SAMPLE
     ========================================================= */

  .type-sample h1 {
    margin: 0 0 0.25rem;
    font-size: 2rem;
    letter-spacing: -0.01em;
    font-weight: 600;
  }
  .root[data-theme='observatory'] .type-sample h1 {
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 2.4rem;
    letter-spacing: -0.02em;
  }
  .root[data-theme='linear-noir'] .type-sample h1 {
    font-size: 1.4rem;
    font-weight: 600;
  }
  .root[data-theme='narrowband'] .type-sample h1 {
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    font-weight: 700;
  }
  .type-sample p { margin: 0 0 0.6rem; line-height: 1.6; max-width: 60ch; }
  .path { font-family: var(--font-mono); word-break: break-all; font-size: 0.85rem; color: var(--fg-mute); }
</style>
