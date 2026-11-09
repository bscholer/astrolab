<!--
  Scope: one verdict (working or not) first, live pictures next, details last.
  Status polls every 5 s. Live frames reload one after another.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { api, type ScopeAutoTarget, type ScopeState } from '$lib/api';
  import { toast } from '$lib/toast.svelte';

  let scope = $state<ScopeState | null>(null);
  let fetchError = $state<string | null>(null);
  let angles = $state<{ pitch: number | null; rotation: number | null } | null>(null);
  let stopping = $state(false);
  let savingAuto = $state(false);
  let weather = $state<Awaited<ReturnType<typeof api.getScopeWeather>> | null>(null);

  let stackTick = $state(Date.now());
  let stackFailed = $state(false);
  let wideTick = $state(Date.now());
  let teleTick = $state(Date.now());
  let wideFailed = $state(false);
  let teleFailed = $state(false);

  const job = $derived(scope?.status?.job ?? null);
  const obs = $derived(scope?.observe ?? null);
  const mirror = $derived(scope?.mirror ?? null);
  const sched = $derived(scope?.scheduler ?? null);
  const hasPreview = $derived(!!scope?.capabilities?.includes('preview'));

  type Tone = 'good' | 'warn' | 'bad' | 'idle';
  const verdict = $derived.by((): { tone: Tone; word: string; line: string; frac: number | null } => {
    if (fetchError) return { tone: 'bad', word: 'No', line: 'Astrolab server unreachable', frac: null };
    if (!scope) return { tone: 'idle', word: '…', line: 'Connecting', frac: null };
    if (!scope.configured) return { tone: 'idle', word: 'Off', line: 'No scope service set', frac: null };
    if (!scope.reachable) {
      return { tone: 'bad', word: 'No', line: scope.error ?? 'Cannot reach the scope service', frac: null };
    }
    if (sched?.state === 'probing') {
      return { tone: 'warn', word: 'Waiting', line: 'Checking the sky', frac: null };
    }
    if (job?.state === 'running') {
      const total = job.frames ?? 0;
      const done = job.captured ?? 0;
      return {
        tone: 'good',
        word: 'Yes',
        line: `Shooting ${obs?.target ?? 'target'} · ${done}/${total} frames${job.failed ? ` · ${job.failed} rejected` : ''}`,
        frac: total ? done / total : null,
      };
    }
    if (obs?.state === 'running') {
      return { tone: 'good', word: 'Yes', line: `Starting ${obs.target ?? 'target'} · ${obs.step ?? 'working'}`, frac: null };
    }
    if (sched?.enabled && sched.state === 'waiting') {
      const why = sched.reason ?? '';
      if (/night window/.test(why)) return { tone: 'warn', word: 'Waiting', line: 'Waiting for dark', frac: null };
      if (/busy/.test(why)) {
        return { tone: 'warn', word: 'Waiting', line: 'Scope is shooting something else', frac: null };
      }
      return { tone: 'warn', word: 'Waiting', line: 'Waiting for a clear sky', frac: null };
    }
    if (sched?.enabled && sched.state === 'error') {
      return { tone: 'bad', word: 'No', line: sched.reason ?? 'Scheduler error', frac: null };
    }
    if (sched?.enabled) return { tone: 'good', word: 'Yes', line: 'Auto is on', frac: null };
    return { tone: 'idle', word: 'Idle', line: 'Auto is off', frac: null };
  });

  const problems = $derived(
    [...new Set([job?.error, obs?.state === 'failed' ? obs.error : null, mirror?.error])].filter(
      (e): e is string => !!e,
    ),
  );

  async function refresh() {
    try {
      scope = await api.getScope();
      fetchError = null;
      const st = scope.status;
      if (st && (st.pitch != null || st.rotation != null)) {
        angles = { pitch: st.pitch ?? null, rotation: st.rotation ?? null };
      }
    } catch (e) {
      fetchError = e instanceof Error ? e.message : String(e);
    }
  }

  let pano = $state<Awaited<ReturnType<typeof api.getScopePano>> | null>(null);
  let panoTick = $state(Date.now());

  let control = $state<Awaited<ReturnType<typeof api.getScopeControl>> | null>(null);

  async function refreshControl() {
    try {
      control = await api.getScopeControl();
    } catch {
      control = null;
    }
  }

  async function runControl(action: 'prepare' | 'calibrate' | 'close') {
    try {
      control = await api.runScopeControl(action);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  let compass = $state<Awaited<ReturnType<typeof api.getScopeCompass>> | null>(null);

  async function refreshCompass() {
    try {
      compass = await api.getScopeCompass();
    } catch {
      compass = null;
    }
  }

  async function startCompass() {
    try {
      compass = await api.startScopeCompass();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  async function refreshPano() {
    try {
      pano = await api.getScopePano();
      if (pano.state === 'done') {
        panoTick = Date.now();
        tiles = await api.getScopePanoTiles();
      }
    } catch {
      pano = null;
    }
  }

  let teleCovered = $state(false);
  let tiles = $state<{ pitches: number[]; rotations: number[]; excluded: string[] } | null>(null);

  async function toggleTile(e: MouseEvent) {
    if (!tiles || !tiles.pitches.length) return;
    const img = e.currentTarget as HTMLImageElement;
    const box = img.getBoundingClientRect();
    const col = Math.floor(((e.clientX - box.left) / box.width) * tiles.rotations.length);
    const row = Math.floor(((e.clientY - box.top) / box.height) * tiles.pitches.length);
    const pitch = [...tiles.pitches].sort((a, b) => b - a)[row];
    const rotation = tiles.rotations[col];
    if (pitch === undefined || rotation === undefined) return;
    const name = `p${pitch}_r${rotation}.jpg`;
    try {
      await api.setScopePanoTile({ pitch, rotation, excluded: !tiles.excluded.includes(name) });
      tiles = await api.getScopePanoTiles();
      panoTick = Date.now();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  async function startPano() {
    try {
      await api.startScopePano(teleCovered);
      await refreshPano();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  async function refreshWeather() {
    try {
      weather = await api.getScopeWeather();
    } catch {
      weather = null;
    }
  }

  async function setAuto(enabled: boolean, targets?: ScopeAutoTarget[], autoStack?: boolean) {
    savingAuto = true;
    try {
      await api.setScopeAuto({ enabled, targets, auto_stack: autoStack });
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      savingAuto = false;
    }
  }

  function editTarget(name: string, patch: Partial<ScopeAutoTarget>) {
    const targets = (scope?.targets ?? []).map((t) => (t.name === name ? { ...t, ...patch } : t));
    return setAuto(!!sched?.enabled, targets);
  }

  async function stopCapture() {
    stopping = true;
    try {
      await api.stopScope();
      toast.success('Stop sent.');
      await refresh();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      stopping = false;
    }
  }

  const fmtDeg = (v: number | null | undefined) => (typeof v === 'number' ? `${v.toFixed(1)}°` : '—');

  // Reload each live frame a few seconds after the last one finished. Skip while the tab is hidden.
  function next(kind: 'wide' | 'tele') {
    const delay = kind === 'wide' ? 8000 : 30000;
    setTimeout(() => {
      if (document.hidden) return next(kind);
      if (kind === 'wide') wideTick = Date.now();
      else teleTick = Date.now();
    }, delay);
  }

  // A failed frame retries later, so a scope that comes back gets its pictures back.
  function retry(kind: 'wide' | 'tele') {
    if (kind === 'wide') wideFailed = true;
    else teleFailed = true;
    setTimeout(() => {
      if (kind === 'wide') {
        wideFailed = false;
        wideTick = Date.now();
      } else {
        teleFailed = false;
        teleTick = Date.now();
      }
    }, 30000);
  }

  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let stackTimer: ReturnType<typeof setInterval> | undefined;
  let weatherTimer: ReturnType<typeof setInterval> | undefined;

  onMount(() => {
    refresh();
    refreshWeather();
    refreshPano();
    refreshCompass();
    refreshControl();
    pollTimer = setInterval(() => {
      refresh();
      if (pano?.state === 'running') refreshPano();
      if (compass?.state === 'running') refreshCompass();
      if (control?.state === 'running') refreshControl();
    }, 5000);
    stackTimer = setInterval(() => {
      stackTick = Date.now();
      stackFailed = false;
    }, 30000);
    weatherTimer = setInterval(refreshWeather, 900000);
  });

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(stackTimer);
    clearInterval(weatherTimer);
  });
</script>

<div class="header">
  <h1>Scope</h1>
</div>

<section class="panel status {verdict.tone}">
  <div class="dot" aria-hidden="true"></div>
  <div class="status-text">
    <div class="word">{verdict.word}</div>
    <div class="line">{verdict.line}</div>
    {#if scope?.reachable && angles}
      <div class="angles" title="Pitch and rotation. The last known values show while the scope moves.">
        <span>pitch {fmtDeg(angles.pitch)}</span>
        <span>rot {fmtDeg(angles.rotation)}</span>
      </div>
    {/if}
    {#if verdict.frac !== null}
      <div class="bar"><div class="fill" style="width: {verdict.frac * 100}%"></div></div>
    {/if}
  </div>
  {#if job?.state === 'running'}
    <button type="button" class="btn warn" onclick={stopCapture} disabled={stopping}>
      {stopping ? 'Stopping…' : 'Stop'}
    </button>
  {/if}
</section>

{#if scope?.reachable}
  <div class="gallery">
    <figure class="frame main">
      {#if stackFailed}
        <div class="empty">No stack yet</div>
      {:else}
        <img
          src="/api/scope/preview?t={stackTick}"
          alt="Latest stack"
          onerror={() => (stackFailed = true)}
        />
      {/if}
      <figcaption>Stack</figcaption>
    </figure>
    {#if hasPreview}
      <figure class="frame">
        {#if wideFailed}
          <div class="empty">No wide frame</div>
        {:else}
          <img
            src="/api/scope/frame?camera=wide&t={wideTick}"
            alt="Live wide view"
            onload={() => next('wide')}
            onerror={() => retry('wide')}
          />
        {/if}
        <figcaption>Wide · live</figcaption>
      </figure>
      <figure class="frame">
        {#if teleFailed}
          <div class="empty">No tele frame</div>
        {:else}
          <img
            src="/api/scope/frame?camera=tele&t={teleTick}"
            alt="Live tele view"
            onload={() => next('tele')}
            onerror={() => retry('tele')}
          />
        {/if}
        <figcaption>Tele · live</figcaption>
      </figure>
    {/if}
  </div>

  <section class="panel">
    <h2>All night</h2>
    <div class="switches">
      <div class="sw-row">
        <div class="sw-text">
          <div class="sw-title">Run all night</div>
          <div class="sw-sub">{sched?.enabled ? 'On. Picks a target and moves on when the sky or target fails.' : 'Off. The scope stays where it is.'}</div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={!!sched?.enabled}
          aria-label="Run all night"
          class="switch"
          class:on={!!sched?.enabled}
          disabled={savingAuto}
          onclick={() => setAuto(!sched?.enabled)}
        ><span class="knob"></span></button>
      </div>
      <div class="sw-row">
        <div class="sw-text">
          <div class="sw-title">Stack new sessions</div>
          <div class="sw-sub">{scope.auto_stack ? 'On. Stacks a session 5 minutes after its last frame.' : 'Off. Stack by hand from Projects.'}</div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={!!scope.auto_stack}
          aria-label="Stack new sessions"
          class="switch"
          class:on={!!scope.auto_stack}
          disabled={savingAuto}
          onclick={() => setAuto(!!sched?.enabled, undefined, !scope?.auto_stack)}
        ><span class="knob"></span></button>
      </div>
    </div>

    <div class="subhead">Targets, in order</div>
    {#if scope.targets?.length}
      <ul class="queue">
        {#each scope.targets as t, i (t.name)}
          <li class:running={obs?.target === t.name}>
            <span class="num">{i + 1}</span>
            <span class="tname">{t.name}</span>
            {#if obs?.target === t.name}<span class="tag">now</span>{/if}
            <select
              class="opt"
              aria-label="Filter for {t.name}"
              disabled={savingAuto}
              value={t.filter ?? ''}
              onchange={(e) => editTarget(t.name, { filter: e.currentTarget.value || null } as Partial<ScopeAutoTarget>)}
            >
              <option value="">Filter: auto</option>
              <option value="duo">Duo-band</option>
              <option value="astro">Astro</option>
              <option value="vis">Vis</option>
            </select>
            <select
              class="opt"
              aria-label="Exposure for {t.name}"
              disabled={savingAuto}
              value={t.exposure_s ?? ''}
              onchange={(e) => editTarget(t.name, { exposure_s: (Number(e.currentTarget.value) || null) as ScopeAutoTarget['exposure_s'] })}
            >
              <option value="">15 s</option>
              <option value="30">30 s</option>
              <option value="60">60 s</option>
            </select>
            <input
              class="opt gain"
              type="number"
              min="0"
              aria-label="Gain for {t.name}"
              placeholder="gain 60"
              disabled={savingAuto}
              value={t.gain ?? ''}
              onchange={(e) => editTarget(t.name, { gain: e.currentTarget.value === '' ? null : Number(e.currentTarget.value) })}
            />
            <button
              type="button"
              class="x"
              aria-label="Remove {t.name}"
              disabled={savingAuto}
              onclick={() => setAuto(!!sched?.enabled, scope?.targets?.filter((o) => o.name !== t.name))}
              >×</button
            >
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted small">No targets yet. Add some from Tonight.</p>
    {/if}
  </section>

  {#if scope.capabilities?.includes('setup')}
    <section class="panel">
      <h2>Controls</h2>
      <div class="controls">
        {#each [['prepare', 'Prepare'], ['calibrate', 'Calibrate'], ['close', 'Close barrel']] as [action, label] (action)}
          <button
            type="button"
            class="btn"
            disabled={control?.state === 'running' || job?.state === 'running' || !!sched?.enabled}
            onclick={() => runControl(action as 'prepare' | 'calibrate' | 'close')}
          >
            {label}
          </button>
        {/each}
      </div>
      <p class="muted small">
        {#if control?.state === 'running'}
          {control.label}…
        {:else if control?.state === 'failed'}
          {control.error}
        {:else if control?.state === 'done'}
          Done.
        {:else}
          Prepare homes the scope. Calibrate focuses and plate solves. Run all night must be off.
        {/if}
      </p>
    </section>
  {/if}

  {#if scope.capabilities?.includes('pointing')}
    <section class="panel">
      <h2>Surroundings</h2>
      <div class="sw-row">
        <div class="sw-text">
          <div class="sw-title">Scan with the wide camera</div>
          <div class="sw-sub">
            {#if pano?.state === 'running'}
              Scanning {pano.done}/{pano.total}. The scope turns on its own.
            {:else}
              Moves the scope through 40 positions, about 10 minutes. After dark, or with the tele lens covered. Run all night must be off.
            {/if}
          </div>
        </div>
        <button
          type="button"
          class="btn"
          disabled={pano?.state === 'running' || !!sched?.enabled}
          onclick={startPano}
        >
          {pano?.state === 'running' ? 'Scanning…' : 'Scan'}
        </button>
      </div>
      <label class="check">
        <input type="checkbox" bind:checked={teleCovered} />
        Tele lens is covered
      </label>
      {#if pano?.state === 'failed'}<p class="muted small">{pano.error}</p>{/if}
      <div class="sw-row">
        <div class="sw-text">
          <div class="sw-title">Learn the compass</div>
          <div class="sw-sub">
            {#if compass?.state === 'running'}
              {compass.step}. {compass.done}/{compass.total} points.
            {:else if compass?.state === 'done'}
              Done. {(compass.total ?? 0) - (compass.failed ?? 0)} of {compass.total} points saved.
            {:else}
              Needs a clear sky after dark. Calibrates, then slews to 5 spots, about 15 minutes.
              Run all night must be off.
            {/if}
          </div>
        </div>
        <button
          type="button"
          class="btn"
          disabled={compass?.state === 'running' || !!sched?.enabled}
          onclick={startCompass}
        >
          {compass?.state === 'running' ? 'Running…' : 'Start'}
        </button>
      </div>
      {#if compass?.state === 'failed'}<p class="muted small">{compass.error}</p>{/if}
      {#if pano?.id && pano.state !== 'running'}
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
        <img
          class="mosaic"
          src="/api/scope/pano/overlay?t={panoTick}"
          alt="Wide camera scan, sky in pink. Tap a tile to leave it out."
          onclick={toggleTile}
        />
        <p class="muted small">Pink is sky. Tap a picture that is wrong to leave it out.</p>
      {/if}
    </section>
  {/if}

  <details class="panel details">
    <summary>Details</summary>
    <dl class="kv">
      <dt>Phone app</dt>
      <dd>{scope.status?.app_connected ? 'connected, scope control is blocked' : 'not connected'}</dd>
      {#if sched?.enabled}
        <dt>Scheduler</dt>
        <dd>{sched.state}{sched.reason ? ` · ${sched.reason}` : ''}</dd>
      {/if}
      {#if weather}
        <dt>Next 12 h</dt>
        <dd class:bad-text={weather.warn}>
          cloud {Math.round(weather.mean_cloud_pct)}% · precip {weather.precip_mm} mm
          {#if weather.reason}· {weather.reason}. Bring the scope in.{/if}
        </dd>
      {/if}
      {#if mirror}
        <dt>Copy</dt>
        <dd>{mirror.state} · {mirror.files_copied} files</dd>
      {/if}
    </dl>
    {#if problems.length}
      <div class="problems">
        <div class="subhead">Last problems</div>
        {#each problems as p}<div>{p}</div>{/each}
      </div>
    {/if}
  </details>
{/if}

<style>
  .header {
    margin: 0.5rem 0 1rem;
  }
  .header h1 {
    margin: 0;
    font-size: 1.5rem;
  }
  h2 {
    margin: 0 0 0.6rem;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
  }
  .panel {
    margin-bottom: 1rem;
    padding: 1rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .status {
    display: flex;
    align-items: center;
    gap: 1rem;
  }
  .status-text {
    flex: 1;
    min-width: 0;
  }
  .dot {
    flex: none;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--fg-mute);
  }
  .status.good .dot {
    background: var(--good);
    box-shadow: 0 0 14px var(--good);
  }
  .status.warn .dot {
    background: var(--warn);
    box-shadow: 0 0 12px var(--warn);
  }
  .status.bad .dot {
    background: var(--bad);
    box-shadow: 0 0 12px var(--bad);
  }
  .word {
    font-family: var(--font-display);
    font-size: 1.6rem;
    line-height: 1.1;
  }
  .line {
    margin-top: 0.2rem;
    font-size: 0.9rem;
    color: var(--fg-mute);
  }
  .angles {
    display: flex;
    gap: 1rem;
    margin-top: 0.35rem;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    color: var(--fg-mute);
  }
  .bar {
    height: 4px;
    margin-top: 0.6rem;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: var(--accent);
  }
  .gallery {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }
  .frame {
    position: relative;
    margin: 0;
    min-width: 0;
    overflow: hidden;
    background: #000;
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    aspect-ratio: 16 / 9;
  }
  .frame.main {
    grid-column: 1 / -1;
    aspect-ratio: 16 / 9;
    max-height: 52vh;
    justify-self: center;
    width: 100%;
  }
  .frame img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .frame.main img {
    object-fit: contain;
  }
  figcaption {
    position: absolute;
    left: 0.5rem;
    bottom: 0.4rem;
    padding: 0.1rem 0.45rem;
    font-size: 0.7rem;
    letter-spacing: 0.04em;
    color: var(--fg);
    background: rgba(5, 8, 16, 0.65);
    border-radius: 999px;
  }
  .empty {
    display: grid;
    place-items: center;
    height: 100%;
    font-size: 0.8rem;
    color: var(--fg-mute);
  }
  .btn {
    min-height: 36px;
    padding: 0 1rem;
    font-size: 0.85rem;
  }
  .switches {
    display: flex;
    flex-direction: column;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .sw-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.7rem 0;
    border-bottom: 1px solid var(--border);
  }
  .sw-row:first-child {
    padding-top: 0;
  }
  .sw-title {
    font-weight: 500;
  }
  .sw-sub {
    margin-top: 0.15rem;
    font-size: 0.8rem;
    color: var(--fg-mute);
  }
  .switch {
    position: relative;
    flex: none;
    width: 46px;
    height: 26px;
    padding: 0;
    cursor: pointer;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 999px;
    transition: background 160ms ease, border-color 160ms ease;
  }
  .switch .knob {
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
    margin: 0.5rem 0 0;
    padding: 0;
    list-style: none;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .queue li {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    padding: 0.2rem 0.4rem 0.2rem 0.8rem;
    border-bottom: 1px solid var(--border);
  }
  .queue li:last-child {
    border-bottom: 0;
  }
  .queue .num {
    width: 1.1rem;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--fg-mute);
  }
  .queue .tname {
    flex: 1;
    min-width: 0;
    font-family: var(--font-mono);
    font-size: 0.9rem;
  }
  .queue .opt {
    font-size: 0.78rem;
    padding: 0.1rem 0.3rem;
  }
  .queue .opt.gain {
    width: 5.5rem;
  }
  .queue li.running .tname {
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
  .mosaic {
    display: block;
    width: 100%;
    margin-top: 0.8rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .details summary {
    cursor: pointer;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
  }
  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.3rem 1rem;
    margin: 0.8rem 0 0;
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }
  .kv dt {
    color: var(--fg-mute);
  }
  .kv dd {
    margin: 0;
    min-width: 0;
  }
  .bad-text {
    color: var(--bad);
  }
  .problems {
    margin-top: 0.8rem;
    padding: 0.5rem 0.7rem;
    font-size: 0.82rem;
    color: var(--fg-mute);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  .subhead {
    margin-bottom: 0.25rem;
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .muted {
    color: var(--fg-mute);
    margin: 0.8rem 0 0;
  }
  .small {
    font-size: 0.85em;
  }
  @media (min-width: 900px) {
    .gallery {
      grid-template-columns: 2fr 1fr;
      grid-template-rows: auto auto;
    }
    .frame.main {
      grid-column: 1;
      grid-row: 1 / span 2;
      aspect-ratio: auto;
      max-height: none;
      min-height: 320px;
    }
  }
</style>
