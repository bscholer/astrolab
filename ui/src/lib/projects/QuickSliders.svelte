<!--
  Quick finishing: a few big controls over the finishing nodes. Each one
  edits the same parameter the pipeline rows below show, through the same
  patch queue, so Quick and the full pipeline never disagree. A slider
  commits when released; each commit is one history entry.
-->
<script lang="ts">
  import type { Project } from '$lib/api';

  type Props = {
    project: Project;
    disabled?: boolean;
    /** The page is showing the crop editor in place of the picture. */
    cropping?: boolean;
    onCrop?: () => void;
    onNodeOverrideChange: (nodeId: string, snapshot: Record<string, unknown>) => void;
  };
  let { project, disabled = false, cropping = false, onCrop, onNodeOverrideChange }: Props = $props();

  // Template defaults when the template doesn't set the param itself.
  const FALLBACK: Record<string, Record<string, unknown>> = {
    stretch: { method: 'autostretch', target_bg: 0.25, auto_object: 0.4, saturation: 1 },
    auto_bp_shift: { enabled: true, clip_fraction: 0.01 },
    graxpert_bg: { enabled: false },
    graxpert_denoise: { enabled: false, strength: 0.5 },
    color_balance: { enabled: true, amount: 0.85 },
    channel_map: { enabled: false, oiii_strength: 1.4 },
    starnet_extract: { enabled: false },
    starnet_recombine: { enabled: true, stars: 1 },
    frame: { enabled: false, width: 0 },
  };

  function templateParams(nodeId: string): Record<string, unknown> | null {
    const node = project.template.nodes.find((n) => n.id === nodeId);
    if (!node) return null;
    return { ...(FALLBACK[nodeId] ?? {}), ...((node.params as Record<string, unknown>) ?? {}) };
  }

  function value(nodeId: string, param: string): unknown {
    const o = project.current_overrides?.[nodeId] as Record<string, unknown> | undefined;
    if (o && param in o) return o[param];
    return templateParams(nodeId)?.[param];
  }

  function commit(nodeId: string, values: Record<string, unknown>) {
    const defaults = templateParams(nodeId) ?? {};
    const snap: Record<string, unknown> = {
      ...((project.current_overrides?.[nodeId] as Record<string, unknown>) ?? {}),
    };
    for (const [k, v] of Object.entries(values)) {
      if (defaults[k] === v) delete snap[k];
      else snap[k] = v;
    }
    onNodeOverrideChange(nodeId, snap);
  }

  const has = (id: string) => project.template.nodes.some((n) => n.id === id);
  const method = $derived(has('stretch') ? String(value('stretch', 'method')) : '');
  const autostretch = $derived(method === 'autostretch' || method === 'auto');
  // 'auto' measures the image; brightness moves where the object lands.
  const brightParam = $derived(method === 'auto' ? 'auto_object' : 'target_bg');
  const brightRange = $derived(method === 'auto' ? [0.15, 0.7] : [0.03, 0.3]);

  // Local drag values, reset from the project whenever it changes.
  let brightness = $state(0);
  let darkSky = $state(0);
  let noise = $state(0);
  let green = $state(0);
  let colour = $state(1);
  let oiii = $state(1.4);
  let stars = $state(1);
  $effect(() => {
    brightness = Number(value('stretch', brightParam) ?? (brightParam === 'auto_object' ? 0.4 : 0.25));
    darkSky = Number(value('auto_bp_shift', 'clip_fraction') ?? 0.01) * 100;
    noise = value('graxpert_denoise', 'enabled') ? Number(value('graxpert_denoise', 'strength') ?? 0.5) : 0;
    green = value('color_balance', 'enabled') === false ? 0 : Number(value('color_balance', 'amount') ?? 0.85);
    colour = Number(value('stretch', 'saturation') ?? 1);
    oiii = Number(value('channel_map', 'oiii_strength') ?? 1.4);
    stars = value('starnet_extract', 'enabled') ? Number(value('starnet_recombine', 'stars') ?? 1) : 1;
  });
  const cropNode = $derived(has('frame') ? 'frame' : 'crop');
  const customCrop = $derived(Number(value(cropNode, 'width') ?? 0) > 0);

  // Stars at 1 skips StarNet entirely (it is the slow step); below 1 it
  // splits the stars off and puts them back smaller, at 0 not at all.
  function setStars(v: number) {
    if (v >= 1) {
      commit('starnet_extract', { enabled: false });
      commit('starnet_recombine', { enabled: true, stars: 1 });
    } else {
      commit('starnet_extract', { enabled: true });
      commit('starnet_recombine', { enabled: true, stars: v });
    }
  }
  const gradient = $derived(!!value('graxpert_bg', 'enabled'));
  const hooOn = $derived(!!value('channel_map', 'enabled'));

  // HOO and SCNR fight: green removal would eat the OIII. Switch them together.
  function setHoo(on: boolean) {
    commit('channel_map', { enabled: on });
    commit('color_balance', { enabled: !on });
  }
</script>

{#if autostretch}
  <section class="quick" aria-label="Quick finishing">
    <h2 class="section-h">Quick</h2>
    <div class="grid">
      <label>
        <span class="name">Brightness <span class="val">{brightness.toFixed(2)}</span></span>
        <input type="range" min={brightRange[0]} max={brightRange[1]} step="0.01" bind:value={brightness} {disabled}
          onchange={() => commit('stretch', { [brightParam]: Number(brightness.toFixed(2)) })} />
        <span class="hint">{method === 'auto'
          ? 'How bright the galaxy or nebula comes out; the sky follows.'
          : 'Where the sky background lands after the stretch.'}</span>
      </label>
      {#if has('auto_bp_shift') && value('auto_bp_shift', 'enabled') !== false}
        <label>
          <span class="name">Dark sky <span class="val">{darkSky.toFixed(1)}%</span></span>
          <input type="range" min="0.1" max="8" step="0.1" bind:value={darkSky} {disabled}
            onchange={() => commit('auto_bp_shift', { clip_fraction: Number((darkSky / 100).toFixed(4)) })} />
          <span class="hint">Share of pixels pushed to black before the stretch.</span>
        </label>
      {/if}
      {#if has('graxpert_denoise')}
        <label>
          <span class="name">Noise reduction <span class="val">{noise === 0 ? 'off' : noise.toFixed(2)}</span></span>
          <input type="range" min="0" max="1" step="0.05" bind:value={noise} {disabled}
            onchange={() =>
              commit('graxpert_denoise', noise === 0 ? { enabled: false } : { enabled: true, strength: Number(noise.toFixed(2)) })} />
          <span class="hint">GraXpert denoise; 0.4–0.6 is the usual range.</span>
        </label>
      {/if}
      <label>
        <span class="name">Colour strength <span class="val">{colour.toFixed(2)}</span></span>
        <input type="range" min="0" max="2" step="0.05" bind:value={colour} {disabled}
          onchange={() => commit('stretch', { saturation: Number(colour.toFixed(2)) })} />
        <span class="hint">1 leaves colour as it is; higher is more vivid.</span>
      </label>
      {#if has('channel_map') && hooOn}
        <label>
          <span class="name">Red–blue balance <span class="val">{oiii.toFixed(2)}</span></span>
          <input type="range" min="0" max="2" step="0.05" bind:value={oiii} {disabled}
            onchange={() => commit('channel_map', { oiii_strength: Number(oiii.toFixed(2)) })} />
          <span class="hint">How much of the nebula shows OIII blue; higher is bluer.</span>
        </label>
      {/if}
      {#if has('color_balance') && !hooOn}
        <label>
          <span class="name">Green removal <span class="val">{green === 0 ? 'off' : green.toFixed(2)}</span></span>
          <input type="range" min="0" max="1" step="0.05" bind:value={green} {disabled}
            onchange={() =>
              commit('color_balance', green === 0 ? { enabled: false } : { enabled: true, amount: Number(green.toFixed(2)) })} />
          <span class="hint">Less keeps the teal of OIII on duo-band data.</span>
        </label>
      {/if}
      {#if has('channel_map')}
        <label class="toggle">
          <input type="checkbox" checked={hooOn} {disabled}
            onchange={(e) => setHoo((e.currentTarget as HTMLInputElement).checked)} />
          <span class="name">HOO colour</span>
          <span class="hint">For duo-band nebulae: red H-alpha, teal OIII.</span>
        </label>
      {/if}
      {#if has('starnet_extract') && has('starnet_recombine')}
        <label>
          <span class="name">Stars <span class="val">{stars >= 1 ? 'as shot' : stars === 0 ? 'none' : stars.toFixed(2)}</span></span>
          <input type="range" min="0" max="1" step="0.05" bind:value={stars} {disabled}
            onchange={() => setStars(Number(stars.toFixed(2)))} />
          <span class="hint">Lower shrinks stars, faint ones first; 0 removes them. Adds StarNet to the render (slower).</span>
        </label>
      {/if}
      {#if has('graxpert_bg')}
        <label class="toggle">
          <input type="checkbox" checked={gradient} {disabled}
            onchange={(e) => commit('graxpert_bg', { enabled: (e.currentTarget as HTMLInputElement).checked })} />
          <span class="name">Remove gradients</span>
          <span class="hint">GraXpert background extraction. Turn off if a nebula fills the frame.</span>
        </label>
      {/if}
      {#if (has('frame') || has('crop')) && onCrop}
        <div class="crop-row">
          <span class="name">Crop <span class="val">{customCrop ? 'custom' : cropNode === 'frame' ? 'full frame' : 'auto'}</span></span>
          <button type="button" class="crop-btn" class:on={cropping} {disabled} onclick={onCrop}>
            {cropping ? 'Done cropping' : 'Adjust crop'}
          </button>
        </div>
      {/if}
    </div>
    <p class="export">
      Export the current version:
      <a href="/api/projects/{project.id}/export" download>full-size PNG</a> ·
      <a href="/api/projects/{project.id}/export?kind=stacked" download>linear stack (FITS)</a>
    </p>
  </section>
{/if}

<style>
  .quick {
    margin: 1.25rem 0;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 0.9rem 1.4rem;
    border: 1px solid var(--border);
    border-radius: var(--radius-card, 12px);
    background: var(--bg-elev);
    padding: 0.9rem 1rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .name {
    font-weight: 600;
    font-size: 0.9rem;
    display: flex;
    justify-content: space-between;
  }
  .val {
    font-weight: 500;
    color: var(--fg-mute);
    font-family: var(--font-mono);
    font-size: 0.8rem;
  }
  .hint {
    color: var(--fg-mute);
    font-size: 0.78rem;
  }
  input[type='range'] {
    width: 100%;
    accent-color: var(--accent);
  }
  .export {
    margin: 0.5rem 0 0;
    font-size: 0.82rem;
    color: var(--fg-mute);
  }
  .export a {
    color: var(--accent);
  }
  .crop-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }
  .crop-row .name {
    flex: 1;
  }
  .crop-btn {
    appearance: none;
    font: inherit;
    font-size: 0.82rem;
    padding: 0.25rem 0.75rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg);
    cursor: pointer;
  }
  .crop-btn:hover:not(:disabled),
  .crop-btn.on {
    border-color: var(--accent);
    color: var(--accent);
  }
  .toggle {
    display: grid;
    grid-template-columns: auto 1fr;
    column-gap: 0.5rem;
    align-items: center;
  }
  .toggle .hint {
    grid-column: 2;
  }
  .toggle input {
    accent-color: var(--accent);
  }
  @media (max-width: 700px) {
    .grid {
      grid-template-columns: 1fr;
      gap: 0.55rem;
      padding: 0.7rem 0.8rem;
    }
    /* The hints are a desktop luxury; each control's name says enough here. */
    .hint {
      display: none;
    }
    .quick {
      margin: 0.75rem 0;
    }
  }
</style>
