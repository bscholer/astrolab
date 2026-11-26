<!--
  One editor control: icon, name, value and a reset button on one line,
  the slider (or switch) under it. Sliders call `oninput` while they move
  (the instant preview follows) and `onchange` on release.
-->
<script lang="ts">
  import Icon from './Icon.svelte';
  import type { ControlDef } from './controls';

  type Props = {
    def: ControlDef;
    value: number | boolean;
    dflt: number | boolean;
    oninput: (v: number | boolean) => void;
    onchange: (v: number | boolean) => void;
  };
  let { def, value, dflt, oninput, onchange }: Props = $props();

  const changed = $derived(
    typeof value === 'number' && typeof dflt === 'number'
      ? Math.round(value) !== Math.round(dflt)
      : value !== dflt
  );

  function fmt(v: number): string {
    if (def.kind !== 'slider') return '';
    const n = Math.round(v);
    if (def.unit) return `${n}${def.unit}`;
    return def.bipolar && n > 0 ? `+${n}` : `${n}`;
  }

  // Fill from the centre for bipolar sliders, from the left otherwise.
  const track = $derived.by(() => {
    if (def.kind !== 'slider' || typeof value !== 'number') return '';
    if (def.hue) {
      return 'linear-gradient(90deg, hsl(0 85% 60%), hsl(60 85% 60%), hsl(120 85% 55%), hsl(180 85% 55%), hsl(240 85% 65%), hsl(300 85% 60%), hsl(360 85% 60%))';
    }
    const pct = ((value - def.min) / (def.max - def.min)) * 100;
    const from = def.bipolar ? 50 : 0;
    const lo = Math.min(from, pct);
    const hi = Math.max(from, pct);
    return `linear-gradient(90deg, var(--ed-track) ${lo}%, var(--accent) ${lo}%, var(--accent) ${hi}%, var(--ed-track) ${hi}%)`;
  });
</script>

<div class="control" class:off={def.kind === 'toggle' && !value}>
  {#if def.kind === 'slider' && typeof value === 'number'}
    <div class="row">
      <span class="ic"><Icon name={def.icon} size={18} /></span>
      <span class="name" title={def.hint}>{def.label}</span>
      {#if !def.live}<span class="slow" title="Shows once the full render is done">render</span>{/if}
      <span class="val">{fmt(value)}</span>
      <button type="button" class="reset" disabled={!changed} aria-label="Reset {def.label}"
        title="Reset" onclick={() => onchange(dflt)}><Icon name="reset" size={14} /></button>
    </div>
    <input
      type="range"
      min={def.min}
      max={def.max}
      step="1"
      value={value}
      style="background: {track}"
      aria-label={def.label}
      oninput={(e) => oninput(Number((e.currentTarget as HTMLInputElement).value))}
      onchange={(e) => onchange(Number((e.currentTarget as HTMLInputElement).value))}
      ondblclick={() => onchange(dflt)}
    />
  {:else}
    <label class="row toggle">
      <span class="ic"><Icon name={def.icon} size={18} /></span>
      <span class="name" title={def.hint}>{def.label}</span>
      {#if !def.live}<span class="slow" title="Shows once the full render is done">render</span>{/if}
      <input
        type="checkbox"
        role="switch"
        class="switch"
        checked={!!value}
        onchange={(e) => onchange((e.currentTarget as HTMLInputElement).checked)}
      />
    </label>
  {/if}
</div>

<style>
  .control {
    padding: 0.4rem 0.2rem 0.5rem;
  }
  .row {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    min-height: 28px;
  }
  .ic {
    color: var(--accent);
    display: inline-flex;
    flex: none;
  }
  .off .ic {
    color: var(--fg-mute);
  }
  .name {
    font-size: 0.92rem;
    font-weight: 500;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .slow {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-mute);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0.05rem 0.4rem;
  }
  .val {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-size: 0.85rem;
    color: var(--fg-mute);
    min-width: 2.6rem;
    text-align: right;
  }
  .reset {
    appearance: none;
    width: 26px;
    height: 26px;
    min-height: 0;
    padding: 0;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }
  .reset:disabled {
    opacity: 0.25;
    cursor: default;
  }
  input[type='range'] {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 4px;
    margin: 0.6rem 0 0.15rem;
    border-radius: 999px;
    outline: none;
    cursor: pointer;
    touch-action: pan-y;
  }
  input[type='range']::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: #f4f7fb;
    border: 0;
    box-shadow: 0 0 0 5px rgba(244, 247, 251, 0.12), 0 2px 6px rgba(0, 0, 0, 0.5);
  }
  input[type='range']::-moz-range-thumb {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: #f4f7fb;
    border: 0;
    box-shadow: 0 0 0 5px rgba(244, 247, 251, 0.12), 0 2px 6px rgba(0, 0, 0, 0.5);
  }
  input[type='range']:focus-visible::-webkit-slider-thumb {
    box-shadow: 0 0 0 4px var(--accent-soft), 0 0 0 2px var(--accent);
  }
  .toggle {
    cursor: pointer;
  }
  .switch {
    appearance: none;
    width: 40px;
    height: 24px;
    border-radius: 999px;
    background: var(--ed-track);
    position: relative;
    cursor: pointer;
    flex: none;
    margin: 0;
    transition: background 160ms ease;
  }
  .switch::after {
    content: '';
    position: absolute;
    top: 3px;
    left: 3px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #f4f7fb;
    transition: transform 160ms ease;
  }
  .switch:checked {
    background: var(--accent);
  }
  .switch:checked::after {
    transform: translateX(16px);
  }
</style>
