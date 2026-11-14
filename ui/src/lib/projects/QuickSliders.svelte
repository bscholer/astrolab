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
    onNodeOverrideChange: (nodeId: string, snapshot: Record<string, unknown>) => void;
  };
  let { project, disabled = false, onNodeOverrideChange }: Props = $props();

  // Template defaults when the template doesn't set the param itself.
  const FALLBACK: Record<string, Record<string, unknown>> = {
    stretch: { method: 'autostretch', target_bg: 0.25 },
    auto_bp_shift: { enabled: true, clip_fraction: 0.01 },
    graxpert_bg: { enabled: false },
    graxpert_denoise: { enabled: false, strength: 0.5 },
    color_balance: { enabled: true, amount: 0.85 },
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
  const autostretch = $derived(has('stretch') && value('stretch', 'method') === 'autostretch');

  // Local drag values, reset from the project whenever it changes.
  let brightness = $state(0);
  let darkSky = $state(0);
  let noise = $state(0);
  let green = $state(0);
  $effect(() => {
    brightness = Number(value('stretch', 'target_bg') ?? 0.25);
    darkSky = Number(value('auto_bp_shift', 'clip_fraction') ?? 0.01) * 100;
    noise = value('graxpert_denoise', 'enabled') ? Number(value('graxpert_denoise', 'strength') ?? 0.5) : 0;
    green = value('color_balance', 'enabled') === false ? 0 : Number(value('color_balance', 'amount') ?? 0.85);
  });
  const gradient = $derived(!!value('graxpert_bg', 'enabled'));
</script>

{#if autostretch}
  <section class="quick" aria-label="Quick finishing">
    <h2 class="section-h">Quick</h2>
    <div class="grid">
      <label>
        <span class="name">Brightness <span class="val">{brightness.toFixed(2)}</span></span>
        <input type="range" min="0.03" max="0.3" step="0.01" bind:value={brightness} {disabled}
          onchange={() => commit('stretch', { target_bg: Number(brightness.toFixed(2)) })} />
        <span class="hint">Where the sky background lands after the stretch.</span>
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
      {#if has('color_balance')}
        <label>
          <span class="name">Green removal <span class="val">{green === 0 ? 'off' : green.toFixed(2)}</span></span>
          <input type="range" min="0" max="1" step="0.05" bind:value={green} {disabled}
            onchange={() =>
              commit('color_balance', green === 0 ? { enabled: false } : { enabled: true, amount: Number(green.toFixed(2)) })} />
          <span class="hint">Less keeps the teal of OIII on duo-band data.</span>
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
    </div>
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
</style>
