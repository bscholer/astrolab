<!--
  System-wide settings: cache budget + on-demand cleanup.

  Slider sets cache_max_bytes (binary GiB). The "Run cleanup" button
  triggers POST /api/storage/cleanup which evicts entries until the cache
  fits under the budget. Eviction order:
    1. Unreachable entries (deleted projects, half-runs) always go first.
    2. Bulk-tier entries (pre-stack sequence data — convert/calibrate/
       register/etc.) by oldest owning project first.
    3. Keep-tier entries (stack output and post-stack work) only when 1
       and 2 didn't free enough.
    Entries held by a running job are never evicted; see the in-use
    lockfile protocol in server/cache.py.
-->
<script lang="ts">
  import { api, type StorageSnapshot } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { formatBytes } from '$lib/format';

  const GIB = 1024 * 1024 * 1024;
  // Last-scan timestamp is per-browser metadata (just a UI nicety); leave
  // it in localStorage. The captures path itself is server-owned now.
  const LAST_SCAN_KEY = 'astrolab.last_scan_at';

  let storage = $state<StorageSnapshot | null>(null);
  let cacheMaxBytes = $state<number>(200 * GIB);
  // Active root = what the running process uses; pendingRoot = what the user
  // is editing. Saving pendingRoot persists it but only takes effect on
  // restart, hence the 'restart required' affordance.
  let activeCacheRoot = $state<string>('');
  let pendingCacheRoot = $state<string>('');
  // Persisted override (may be null when no override is set; we stash it so
  // we can tell the user 'saved, but server still on activeCacheRoot until
  // you restart').
  let persistedCacheRoot = $state<string | null>(null);
  let saving = $state(false);
  let savingRoot = $state(false);
  let cleaning = $state(false);
  // Captures path lives in the settings KV alongside cache_root: the scan
  // target is on the server, so storing it in localStorage was always
  // wrong (didn't survive a cache clear, didn't carry across devices).
  let captureRoot = $state('');
  let persistedCaptureRoot = $state<string | null>(null);
  let savingCapture = $state(false);
  let scanning = $state(false);

  // Site location for the Tonight planner. We keep these as strings while
  // the user is typing so the input doesn't fight an empty/-/decimal-in-
  // progress state; parseFloat happens at save time. Persisted values are
  // numbers (or null when never set).
  let siteLatInput = $state('');
  let siteLonInput = $state('');
  let siteElevInput = $state('');
  let persistedSiteLat = $state<number | null>(null);
  let persistedSiteLon = $state<number | null>(null);
  let persistedSiteElev = $state<number | null>(null);
  let savingSite = $state(false);
  let locating = $state(false);

  async function saveCaptureRoot() {
    const next = captureRoot.trim();
    savingCapture = true;
    try {
      const r = await api.patchSettings({ capture_root: next });
      persistedCaptureRoot = r.capture_root;
      captureRoot = r.capture_root ?? '';
      toast.success(next ? 'Capture path saved.' : 'Capture path cleared.');
    } catch (e) {
      toast.error(`Couldn't save: ${(e as Error).message}`);
    } finally {
      savingCapture = false;
    }
  }

  async function scanNow() {
    const root = captureRoot.trim();
    if (!root) {
      toast.error('Set a capture root first.');
      return;
    }
    // Persist before scanning so a refresh from another browser sees the
    // same target. Skip if it's already what's on the server.
    if (root !== persistedCaptureRoot) {
      try {
        await api.patchSettings({ capture_root: root });
        persistedCaptureRoot = root;
      } catch (e) {
        toast.error(`Couldn't save capture path: ${(e as Error).message}`);
        return;
      }
    }
    scanning = true;
    try {
      await api.scan(root);
      localStorage.setItem(LAST_SCAN_KEY, new Date().toISOString());
      toast.success('Scan started. Watch progress on the Library page.');
    } catch (e) {
      toast.error(`Scan failed: ${(e as Error).message}`);
    } finally {
      scanning = false;
    }
  }

  async function load() {
    try {
      const [snap, settings] = await Promise.all([
        api.getStorage(),
        api.getSettings(),
      ]);
      storage = snap;
      cacheMaxBytes = settings.cache_max_bytes;
      activeCacheRoot = settings.cache_root_active;
      persistedCacheRoot = settings.cache_root;
      pendingCacheRoot = settings.cache_root ?? settings.cache_root_active;
      persistedCaptureRoot = settings.capture_root;
      captureRoot = settings.capture_root ?? '';
      persistedSiteLat = settings.site_latitude;
      persistedSiteLon = settings.site_longitude;
      persistedSiteElev = settings.site_elevation_m;
      siteLatInput =
        settings.site_latitude !== null ? String(settings.site_latitude) : '';
      siteLonInput =
        settings.site_longitude !== null ? String(settings.site_longitude) : '';
      siteElevInput =
        settings.site_elevation_m !== null ? String(settings.site_elevation_m) : '';
    } catch (e) {
      toast.error(`Couldn't load settings: ${(e as Error).message}`);
    }
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      toast.error("This browser doesn't expose a geolocation API.");
      return;
    }
    locating = true;
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        // Round to four decimal places (~11m). Browsers report sub-meter
        // precision but the planner only resolves to degrees, so trimming
        // the noise keeps the inputs readable.
        siteLatInput = pos.coords.latitude.toFixed(4);
        siteLonInput = pos.coords.longitude.toFixed(4);
        // Browser elevation is null on most desktops (geolocation comes
        // from IP/wifi triangulation, no altitude). Only override if we
        // actually got a number.
        if (Number.isFinite(pos.coords.altitude)) {
          siteElevInput = String(Math.round(pos.coords.altitude as number));
        }
        toast.success('Location filled in. Click Save to persist.');
        locating = false;
      },
      (err) => {
        // PERMISSION_DENIED, POSITION_UNAVAILABLE, TIMEOUT — surface the
        // browser's own message rather than mapping codes; it's already
        // user-readable and we'd just rephrase.
        toast.error(`Couldn't get location: ${err.message}`);
        locating = false;
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
    );
  }

  async function saveSite() {
    // Local validation mirrors the server's bounds. Done client-side so
    // the user gets immediate inline feedback instead of a toast after a
    // round-trip.
    const lat = parseFloat(siteLatInput);
    const lon = parseFloat(siteLonInput);
    const elev = parseFloat(siteElevInput);
    if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
      toast.error('Latitude must be a number between -90 and 90.');
      return;
    }
    if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
      toast.error('Longitude must be a number between -180 and 180.');
      return;
    }
    if (!Number.isFinite(elev) || elev < -500 || elev > 9000) {
      toast.error('Elevation must be a number in meters between -500 and 9000.');
      return;
    }
    savingSite = true;
    try {
      const r = await api.patchSettings({
        site_latitude: lat,
        site_longitude: lon,
        site_elevation_m: elev,
      });
      persistedSiteLat = r.site_latitude;
      persistedSiteLon = r.site_longitude;
      persistedSiteElev = r.site_elevation_m;
      toast.success('Site location saved.');
    } catch (e) {
      toast.error(`Couldn't save: ${(e as Error).message}`);
    } finally {
      savingSite = false;
    }
  }

  $effect(() => {
    load();
  });

  const captureDirty = $derived(
    captureRoot.trim() !== (persistedCaptureRoot ?? '')
  );

  const siteDirty = $derived(
    siteLatInput.trim() !== (persistedSiteLat !== null ? String(persistedSiteLat) : '') ||
    siteLonInput.trim() !== (persistedSiteLon !== null ? String(persistedSiteLon) : '') ||
    siteElevInput.trim() !== (persistedSiteElev !== null ? String(persistedSiteElev) : '')
  );

  // Slider operates on GiB to give us friendly round-number values; we
  // convert back to bytes on save. Max = the partition's total size
  // (per /api/storage cache_disk.total_bytes). On a fresh install the
  // backend defaults the budget to half the partition, so the slider
  // lands mid-range automatically.
  const sliderMaxGiB = $derived.by(() => {
    const diskTotal = (storage?.cache_disk?.total_bytes ?? 0) / GIB;
    return diskTotal > 0 ? Math.max(2, Math.floor(diskTotal)) : 200;
  });
  // Display in whole GiB; bytes truncated to GiB rounded down so the
  // input doesn't show 1136.0625... when the value came back as e.g.
  // half-of-2440-GB. setSliderGiB writes back exact GiB-aligned bytes
  // so a save round-trip is stable.
  const cacheMaxGiB = $derived(Math.round(cacheMaxBytes / GIB));

  function setSliderGiB(g: number) {
    cacheMaxBytes = Math.round(g) * GIB;
  }

  // Cache-root edit state: dirty when the editor shows something other
  // than what's persisted, plus the live process disagrees with the
  // persisted value (i.e. user already saved but hasn't restarted).
  const cacheRootDirty = $derived(
    pendingCacheRoot.trim() !== (persistedCacheRoot ?? activeCacheRoot)
  );
  const restartRequired = $derived(
    persistedCacheRoot !== null &&
    persistedCacheRoot !== '' &&
    persistedCacheRoot !== activeCacheRoot
  );

  async function saveCacheRoot() {
    const next = pendingCacheRoot.trim();
    savingRoot = true;
    try {
      // Empty input clears the override (server falls back to default).
      const r = await api.patchSettings({ cache_root: next });
      persistedCacheRoot = r.cache_root;
      pendingCacheRoot = r.cache_root ?? r.cache_root_active;
      activeCacheRoot = r.cache_root_active;
      if (r.cache_root && r.cache_root !== r.cache_root_active) {
        toast.success('Saved. Restart astrolab-api to use the new cache root.');
      } else {
        toast.success('Saved.');
      }
    } catch (e) {
      toast.error(`Couldn't save: ${(e as Error).message}`);
    } finally {
      savingRoot = false;
    }
  }

  async function save() {
    saving = true;
    try {
      const r = await api.patchSettings({ cache_max_bytes: cacheMaxBytes });
      cacheMaxBytes = r.cache_max_bytes;
      toast.success(`Budget set to ${formatBytes(cacheMaxBytes)}`);
    } catch (e) {
      toast.error(`Couldn't save: ${(e as Error).message}`);
    } finally {
      saving = false;
    }
  }

  async function runCleanup() {
    cleaning = true;
    try {
      const r = await api.storageCleanup();
      const msg = r.evicted_count === 0
        ? 'Cache already fits under the budget — nothing to evict.'
        : `Evicted ${r.evicted_count} entries, freed ${formatBytes(r.bytes_freed)}.`;
      if (r.over_budget) {
        toast.error(
          `${msg} Still over budget — consider raising the cap or deleting projects.`
        );
      } else {
        toast.success(msg);
      }
      await load();
    } catch (e) {
      toast.error(`Cleanup failed: ${(e as Error).message}`);
    } finally {
      cleaning = false;
    }
  }
</script>

<div class="header">
  <h1>Settings</h1>
</div>

<section class="panel">
  <h2>Capture</h2>
  <p class="muted small">
    Where the scope drops new frames. Refresh on the Library page
    re-scans this path.
  </p>
  <div class="capture-row">
    <input
      type="text"
      class="capture-input"
      placeholder="/captures or /home/you/Pictures/Siril"
      bind:value={captureRoot}
      autocomplete="off"
      spellcheck="false"
    />
    <button
      type="button"
      class="btn"
      onclick={saveCaptureRoot}
      disabled={savingCapture || !captureDirty}
      title={captureDirty ? 'Save the capture path to the server' : 'No changes to save'}
    >
      {savingCapture ? 'Saving…' : 'Save'}
    </button>
    <button
      type="button"
      class="btn warn"
      onclick={scanNow}
      disabled={scanning || !captureRoot.trim()}
    >
      {scanning ? 'Scanning…' : 'Scan now'}
    </button>
  </div>
  {#if captureDirty}
    <p class="muted small note">
      Unsaved. Click Save (or Scan now — that saves first) to persist.
    </p>
  {/if}
</section>

<section class="panel">
  <h2>Site location</h2>
  <p class="muted small">
    Where you observe from. The Tonight planner uses these to compute
    altitude and transit times. Latitude is positive north, longitude
    positive east. Elevation is in meters above sea level.
  </p>
  <div class="site-grid">
    <label class="field">
      <span class="field-label">Latitude</span>
      <input
        type="number"
        class="num-input"
        step="0.0001"
        min={-90}
        max={90}
        placeholder="40.7128"
        bind:value={siteLatInput}
        autocomplete="off"
        spellcheck="false"
        aria-label="Site latitude in decimal degrees"
      />
      <span class="field-unit">deg</span>
    </label>
    <label class="field">
      <span class="field-label">Longitude</span>
      <input
        type="number"
        class="num-input"
        step="0.0001"
        min={-180}
        max={180}
        placeholder="-74.006"
        bind:value={siteLonInput}
        autocomplete="off"
        spellcheck="false"
        aria-label="Site longitude in decimal degrees"
      />
      <span class="field-unit">deg</span>
    </label>
    <label class="field">
      <span class="field-label">Elevation</span>
      <input
        type="number"
        class="num-input"
        step="1"
        min={-500}
        max={9000}
        placeholder="10"
        bind:value={siteElevInput}
        autocomplete="off"
        spellcheck="false"
        aria-label="Site elevation in meters"
      />
      <span class="field-unit">m</span>
    </label>
  </div>
  <div class="site-actions">
    <button
      type="button"
      class="btn"
      onclick={useMyLocation}
      disabled={locating}
      title="Fill latitude and longitude from your browser's geolocation"
    >
      {locating ? 'Locating…' : 'Use my location'}
    </button>
    <button
      type="button"
      class="btn primary"
      onclick={saveSite}
      disabled={savingSite || !siteDirty}
      title={siteDirty ? 'Save site location' : 'No changes to save'}
    >
      {savingSite ? 'Saving…' : 'Save location'}
    </button>
  </div>
</section>

{#if storage === null}
  <p class="muted">Loading…</p>
{:else}
  <section class="panel">
    <h2>Storage</h2>
    <div class="stats">
      <div class="stat">
        <span class="stat-label">cache total</span>
        <span class="stat-val">{formatBytes(storage.total_bytes)}</span>
        <span class="stat-sub muted">{storage.entry_count} entries</span>
      </div>
      <div class="stat">
        <span class="stat-label">unreachable</span>
        <span class="stat-val">{formatBytes(storage.unreachable_bytes)}</span>
        <span class="stat-sub muted">{storage.unreachable_count} dead entries</span>
      </div>
      <div class="stat">
        <span class="stat-label">partition</span>
        <span class="stat-val">{formatBytes(storage.cache_disk.total_bytes)}</span>
        <span class="stat-sub muted">
          {formatBytes(storage.cache_disk.free_bytes)} free
        </span>
      </div>
    </div>
    <div class="cache-root-row">
      <label class="cache-root-label" for="cache-root-input">cache location</label>
      <input
        id="cache-root-input"
        type="text"
        class="cache-root-input"
        bind:value={pendingCacheRoot}
        placeholder={activeCacheRoot}
        autocomplete="off"
        spellcheck="false"
      />
      <button
        type="button"
        class="btn primary"
        onclick={saveCacheRoot}
        disabled={savingRoot || !cacheRootDirty}
      >
        {savingRoot ? 'Saving…' : 'Save location'}
      </button>
    </div>
    {#if restartRequired}
      <p class="warn small">
        Saved <code>{persistedCacheRoot}</code>. Server is still using
        <code>{activeCacheRoot}</code> — restart <code>astrolab-api</code> to
        switch over. Existing cache files at the old location stay put.
      </p>
    {/if}
  </section>

  <section class="panel">
    <h2>Cache budget</h2>
    <p class="muted small">
      Eviction sweeps trim the cache to this size. Orphans go first, then
      pre-stack bulk data (oldest project first), and only as a last
      resort the stack output and post-stack work. Anything a running
      job is reading or writing is held back via the in-use lockfile.
    </p>
    <div class="slider-row">
      <input
        type="range"
        min={1}
        max={sliderMaxGiB}
        step={1}
        value={cacheMaxGiB}
        oninput={(e) => setSliderGiB(parseFloat((e.currentTarget as HTMLInputElement).value))}
      />
      <input
        type="number"
        class="num"
        min={1}
        max={sliderMaxGiB}
        step={1}
        value={cacheMaxGiB}
        oninput={(e) => setSliderGiB(parseFloat((e.currentTarget as HTMLInputElement).value))}
      />
      <span class="unit">GiB</span>
    </div>
    <div class="actions">
      <button type="button" class="btn primary" onclick={save} disabled={saving}>
        {saving ? 'Saving…' : 'Save budget'}
      </button>
      <button type="button" class="btn" onclick={runCleanup} disabled={cleaning}>
        {cleaning ? 'Cleaning…' : 'Run cleanup now'}
      </button>
    </div>
    {#if storage.total_bytes > cacheMaxBytes}
      <p class="warn">
        Current usage ({formatBytes(storage.total_bytes)}) exceeds this
        budget ({formatBytes(cacheMaxBytes)}). Click <strong>Run
        cleanup now</strong> to evict.
      </p>
    {/if}
  </section>
{/if}

<style>
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin: 0.5rem 0 1.25rem;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }

  .small {
    font-size: 0.85em;
  }
  .muted {
    color: var(--fg-mute);
  }

  .panel {
    margin-bottom: 1.5rem;
    padding: 1.05rem 1.2rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--shadow);
  }
  .panel h2 {
    margin: 0 0 0.5rem;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
    font-weight: 600;
  }

  .stats {
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    margin-top: 0.5rem;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 8rem;
  }
  .stat-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .stat-val {
    font-family: var(--font-mono);
    font-size: 1.5rem;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }
  .stat-sub {
    font-size: 0.75rem;
  }
  .cache-root-row {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    margin-top: 1rem;
    flex-wrap: wrap;
  }
  .cache-root-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .cache-root-input {
    flex: 1 1 280px;
    min-width: 0;
    padding: 0.4rem 0.7rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font: inherit;
    font-family: var(--font-mono);
    font-size: 0.85rem;
  }

  .capture-row {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    margin-top: 0.5rem;
    flex-wrap: wrap;
  }

  .site-grid {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    margin-top: 0.6rem;
    align-items: flex-end;
  }
  .site-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.85rem;
    flex-wrap: wrap;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    min-width: 7.5rem;
  }
  .field-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--fg-mute);
  }
  .field-unit {
    font-size: 0.7rem;
    color: var(--fg-mute);
    font-family: var(--font-mono);
  }
  .num-input {
    padding: 0.4rem 0.7rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font: inherit;
    font-family: var(--font-mono);
    font-size: 0.9rem;
    width: 100%;
  }
  .capture-input {
    flex: 1 1 280px;
    min-width: 0;
    padding: 0.5rem 0.85rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font: inherit;
    font-family: var(--font-mono);
    font-size: 0.9rem;
  }

  .slider-row {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    margin: 0.6rem 0;
  }
  .slider-row input[type='range'] {
    flex: 1;
  }
  .slider-row .num {
    width: 5rem;
    padding: 0.3rem 0.5rem;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font: inherit;
    font-family: var(--font-mono);
  }
  .slider-row .unit {
    color: var(--fg-mute);
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.6rem;
  }
  .btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 0.4rem 0.95rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.85rem;
    cursor: pointer;
    transition: border-color 160ms ease, color 160ms ease, transform 160ms ease;
  }
  .btn:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn.primary {
    background: linear-gradient(135deg, var(--accent), var(--good));
    color: var(--accent-ink);
    border-color: transparent;
    font-weight: 600;
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 18px var(--accent-soft);
  }
  .btn.primary:hover:not(:disabled) {
    color: var(--accent-ink);
    transform: translateY(-1px);
    filter: brightness(1.08);
  }
  /* Amber call-to-action for buttons that kick off real, side-effecting
     work (e.g. Scan now), as opposed to .primary which is for saving
     config. Dark ink keeps the label legible against the bright fill. */
  .btn.warn {
    background: linear-gradient(135deg, var(--warn), #f59e0b);
    color: #2a1d05;
    border-color: transparent;
    font-weight: 600;
    box-shadow: 0 0 0 1px rgba(251, 191, 36, 0.3), 0 0 18px rgba(251, 191, 36, 0.18);
  }
  .btn.warn:hover:not(:disabled) {
    color: #2a1d05;
    transform: translateY(-1px);
    filter: brightness(1.08);
  }

  p.warn {
    margin-top: 0.5rem;
    font-size: 0.85rem;
    color: var(--warn);
  }
</style>
