<!--
  Auto-generated parameter form for a single node, driven by the JSON
  Schema returned by GET /api/templates/{id}/schema.

  Each schema field can carry UI metadata via Pydantic's
  `Field(json_schema_extra={...})`:
    - ui_hidden: true                    -> never shown (pipeline plumbing).
    - ui_section: "advanced"             -> collapsed behind a disclosure.
    - ui_when: {paramName: value}        -> only shown when current value of
                                            paramName equals `value` (scalar)
                                            or is in `value` (list).
    - (default)                          -> always shown in basic.

  Control rendering is heuristic on the field shape:
    - enum         -> <select>
    - integer/number with min+max -> <input type=range> + numeric display
    - integer/number without bounds -> <input type=number>
    - boolean      -> toggle (<input type=checkbox>)
    - string       -> <input type=text>

  Range/number controls follow a "live display, commit on release" model:
  dragging the slider updates the displayed numeric value continuously but
  doesn't fire `onchange` to the parent until the user releases. This keeps
  the slider feeling responsive without queuing dozens of jobs while a
  drag is in progress.
-->
<script lang="ts">
  import type { CostClass, JSONSchemaField } from '$lib/api';

  interface Props {
    nodeId: string;
    schemaProps: Record<string, JSONSchemaField>;
    defaults: Record<string, unknown>;
    overrides: Record<string, unknown>;
    cost: CostClass;
    onchange: (next: Record<string, unknown>) => void;
    /** Field names the form should NOT render. Used for fields the parent
     * surfaces directly (e.g. `enabled`, which has its own toggle on the
     * card header so it doesn't clutter the body). */
    hideFields?: string[];
  }

  const {
    nodeId,
    schemaProps,
    defaults,
    overrides,
    cost,
    onchange,
    hideFields = []
  }: Props = $props();

  // Effective value: override wins, else default. Used for both reading the
  // visible state of each control and for evaluating ui_when dependencies.
  function effective(name: string): unknown {
    return name in overrides ? overrides[name] : defaults[name];
  }

  // ui_when: a field is visible only when every dependency it lists matches
  // the current effective value of the depended-on param. Scalar dep value
  // = exact match; list = "any of these"; missing dep = treat as no match
  // (defensively hide the dependent field). Returning true here means
  // "render this field"; false means "skip it".
  function isVisible(field: JSONSchemaField): boolean {
    if (!field.ui_when) return true;
    for (const [dep, expected] of Object.entries(field.ui_when)) {
      const actual = effective(dep);
      if (Array.isArray(expected)) {
        if (!expected.includes(actual)) return false;
      } else if (actual !== expected) {
        return false;
      }
    }
    return true;
  }

  // Partition properties by UI section, filtering out anything that's hidden
  // or whose ui_when says it shouldn't show right now. Anything without a
  // section tag defaults to "basic" so freshly-added params surface by
  // default rather than silently disappearing.
  const partitioned = $derived.by(() => {
    const basic: [string, JSONSchemaField][] = [];
    const advanced: [string, JSONSchemaField][] = [];
    const hide = new Set(hideFields);
    for (const [name, field] of Object.entries(schemaProps)) {
      if (hide.has(name)) continue;
      if (field.ui_hidden === true) continue;
      if (!isVisible(field)) continue;
      if (field.ui_section === 'advanced') advanced.push([name, field]);
      else basic.push([name, field]);
    }
    return { basic, advanced };
  });

  function emit(name: string, value: unknown) {
    // Drop the override if the new value matches the default — keeps the
    // hash stable and prevents the override map from accumulating noise.
    const next: Record<string, unknown> = { ...overrides };
    if (value === defaults[name]) {
      delete next[name];
    } else {
      next[name] = value;
    }
    onchange(next);
  }

  function fieldType(field: JSONSchemaField): 'enum' | 'range' | 'number' | 'boolean' | 'string' {
    if (field.enum) return 'enum';
    const types = Array.isArray(field.type) ? field.type : field.type ? [field.type] : [];
    const hasNum = types.includes('number') || types.includes('integer');
    const hasMin = field.minimum !== undefined || field.exclusiveMinimum !== undefined;
    const hasMax = field.maximum !== undefined || field.exclusiveMaximum !== undefined;
    if (hasNum && hasMin && hasMax) return 'range';
    if (hasNum) return 'number';
    if (types.includes('boolean')) return 'boolean';
    return 'string';
  }

  function rangeStep(field: JSONSchemaField): number {
    const types = Array.isArray(field.type) ? field.type : field.type ? [field.type] : [];
    if (types.includes('integer')) return 1;
    if (field.hash_precision !== undefined) {
      return Math.pow(10, -field.hash_precision);
    }
    const lo = (field.minimum ?? field.exclusiveMinimum ?? 0) as number;
    const hi = (field.maximum ?? field.exclusiveMaximum ?? 1) as number;
    return Math.min((hi - lo) / 100, 0.1);
  }

  function isOverridden(name: string): boolean {
    return name in overrides;
  }

  function resetField(name: string) {
    const next: Record<string, unknown> = { ...overrides };
    delete next[name];
    onchange(next);
  }

  // Live values for range/number controls: while the user drags a slider,
  // the displayed number updates per `oninput` but we don't push the change
  // up to the parent (no PATCH gets queued). On release / blur we fire
  // `onchange` once, which goes through the debounce on the parent. Keeps
  // sliders feeling responsive without spawning a job per pixel of drag.
  let liveValues = $state<Record<string, number>>({});

  function liveOrEffective(name: string): unknown {
    return name in liveValues ? liveValues[name] : effective(name);
  }

  function setLive(name: string, value: number) {
    liveValues = { ...liveValues, [name]: value };
  }

  function commitLive(name: string, value: number) {
    // Clear local draft first so the next render reads from the (possibly
    // re-derived) effective value rather than stale draft state.
    if (name in liveValues) {
      const next = { ...liveValues };
      delete next[name];
      liveValues = next;
    }
    emit(name, value);
  }

  // Count of overridden params split by section so the badges + disclosure
  // accurately reflect what the user has touched.
  const overriddenInBasic = $derived(
    partitioned.basic.filter(([n]) => isOverridden(n)).length
  );
  const overriddenInAdvanced = $derived(
    partitioned.advanced.filter(([n]) => isOverridden(n)).length
  );
</script>

<div class="form">
  <header class="form-head">
    <span class="cost-pill cost-{cost}" title="Editing this node re-runs {cost} downstream work">
      {cost}
    </span>
    <span class="muted small">{nodeId}</span>
  </header>

  {#snippet paramControl(name: string, field: JSONSchemaField)}
    {@const ft = fieldType(field)}
    {@const val = effective(name)}
    {@const overridden = isOverridden(name)}
    <div class="param" class:overridden>
      <div class="param-head">
        <label for="{nodeId}-{name}">{name}</label>
        {#if field.description}
          <button
            type="button"
            class="info-icon"
            data-tip={field.description}
            aria-label={field.description}
            tabindex="0"
          >?</button>
        {/if}
        {#if overridden}
          <button
            type="button"
            class="reset-btn"
            title="Reset to template default ({JSON.stringify(defaults[name])})"
            onclick={() => resetField(name)}
          >reset</button>
        {/if}
      </div>

      {#if ft === 'enum'}
        <select
          id="{nodeId}-{name}"
          value={val}
          onchange={(e) => emit(name, (e.currentTarget as HTMLSelectElement).value)}
        >
          {#each field.enum ?? [] as opt}
            <option value={opt}>{String(opt)}</option>
          {/each}
        </select>
      {:else if ft === 'range'}
        {@const live = liveOrEffective(name) as number}
        <div class="range-wrap">
          <input
            id="{nodeId}-{name}"
            type="range"
            min={field.minimum ?? field.exclusiveMinimum}
            max={field.maximum ?? field.exclusiveMaximum}
            step={rangeStep(field)}
            value={live}
            oninput={(e) => setLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
            onchange={(e) => commitLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
          />
          <input
            type="number"
            class="range-num"
            min={field.minimum ?? field.exclusiveMinimum}
            max={field.maximum ?? field.exclusiveMaximum}
            step={rangeStep(field)}
            value={live}
            oninput={(e) => setLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
            onchange={(e) => commitLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
          />
        </div>
      {:else if ft === 'number'}
        {@const live = liveOrEffective(name) as number}
        <input
          id="{nodeId}-{name}"
          type="number"
          value={live}
          oninput={(e) => setLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
          onchange={(e) => commitLive(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
        />
      {:else if ft === 'boolean'}
        <label class="toggle">
          <input
            id="{nodeId}-{name}"
            type="checkbox"
            checked={Boolean(val)}
            onchange={(e) => emit(name, (e.currentTarget as HTMLInputElement).checked)}
          />
          <span>{val ? 'on' : 'off'}</span>
        </label>
      {:else}
        <input
          id="{nodeId}-{name}"
          type="text"
          value={val as string ?? ''}
          oninput={(e) => emit(name, (e.currentTarget as HTMLInputElement).value)}
        />
      {/if}

    </div>
  {/snippet}

  {#if partitioned.basic.length === 0 && partitioned.advanced.length === 0}
    <p class="muted small">No editable parameters on this node.</p>
  {:else}
    {#each partitioned.basic as [name, field] (name)}
      {@render paramControl(name, field)}
    {/each}

    {#if partitioned.advanced.length > 0}
      <details class="advanced" open={overriddenInAdvanced > 0}>
        <summary>
          <span class="adv-caret" aria-hidden="true">▸</span>
          <span>Advanced</span>
          <span class="muted small">{partitioned.advanced.length}</span>
          {#if overriddenInAdvanced > 0}
            <span class="adv-badge">{overriddenInAdvanced} modified</span>
          {/if}
        </summary>
        <div class="advanced-body">
          {#each partitioned.advanced as [name, field] (name)}
            {@render paramControl(name, field)}
          {/each}
        </div>
      </details>
    {/if}
  {/if}
</div>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .form-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.25rem;
  }
  .small {
    font-size: 0.8rem;
  }
  .muted {
    color: var(--fg-mute, #888);
  }

  .cost-pill {
    display: inline-block;
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
  }
  .cost-cheap {
    background: rgba(94, 211, 168, 0.18);
    color: var(--good, #5ed3a8);
    border: 1px solid var(--good, #5ed3a8);
  }
  .cost-medium {
    background: rgba(240, 179, 94, 0.18);
    color: var(--warn, #f0b35e);
    border: 1px solid var(--warn, #f0b35e);
  }
  .cost-expensive {
    background: rgba(255, 122, 138, 0.18);
    color: var(--bad, #ff7a8a);
    border: 1px solid var(--bad, #ff7a8a);
  }

  .param {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    padding: 0.25rem 0;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
  }
  .param:first-of-type {
    border-top: none;
  }
  .param.overridden {
    box-shadow: inset 3px 0 0 var(--accent, #7aa2ff);
    padding-left: 0.5rem;
  }
  .param-head {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
  }
  .param label {
    font-family: ui-monospace, monospace;
    font-size: 0.8rem;
    flex: 1;
  }
  .reset-btn {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--accent, #7aa2ff);
    font-size: 0.7rem;
    cursor: pointer;
    padding: 0;
  }
  .reset-btn:hover {
    text-decoration: underline;
  }

  /* Help affordance: a tiny ? next to the param name. The full description
     surfaces as a tooltip on hover/focus instead of taking permanent
     vertical space; param descriptions are often a paragraph and pushed
     the form to ridiculous heights. */
  .info-icon {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg-mute, #888);
    width: 14px;
    height: 14px;
    border-radius: 50%;
    font-size: 0.65rem;
    line-height: 1;
    padding: 0;
    cursor: help;
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .info-icon:hover,
  .info-icon:focus-visible {
    color: var(--fg, #ddd);
    border-color: var(--accent, #7aa2ff);
    outline: none;
  }
  .info-icon[data-tip]::after {
    content: attr(data-tip);
    position: absolute;
    bottom: calc(100% + 6px);
    left: 0;
    white-space: pre-line;
    max-width: min(320px, 60vw);
    width: max-content;
    text-align: left;
    background: var(--bg-elev, #14171d);
    color: var(--fg, #ddd);
    border: 1px solid var(--border, #333);
    padding: 0.4rem 0.6rem;
    border-radius: 6px;
    font-size: 0.75rem;
    font-weight: 400;
    line-height: 1.4;
    pointer-events: none;
    opacity: 0;
    transform: translateY(2px);
    transition: opacity 120ms ease, transform 120ms ease;
    z-index: 5;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  }
  .info-icon:hover[data-tip]::after,
  .info-icon:focus-visible[data-tip]::after {
    opacity: 1;
    transform: translateY(0);
  }

  .param input[type='text'],
  .param input[type='number'],
  .param select {
    width: 100%;
    padding: 0.25rem 0.4rem;
    background: var(--bg, #0a0c10);
    color: var(--fg, #ddd);
    border: 1px solid var(--border, #333);
    border-radius: 4px;
    font: inherit;
    font-size: 0.85rem;
  }

  .range-wrap {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .range-wrap input[type='range'] {
    flex: 1;
  }
  .range-num {
    width: 5rem;
  }

  .toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .advanced {
    margin-top: 0.4rem;
    border-top: 1px dashed rgba(255, 255, 255, 0.08);
    padding-top: 0.4rem;
  }
  .advanced > summary {
    list-style: none;
    cursor: pointer;
    display: flex;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--fg-mute, #888);
    user-select: none;
    padding: 0.15rem 0;
  }
  .advanced > summary::-webkit-details-marker {
    display: none;
  }
  .adv-caret {
    display: inline-block;
    transition: transform 120ms ease;
    font-size: 0.85em;
  }
  .advanced[open] .adv-caret {
    transform: rotate(90deg);
  }
  .adv-badge {
    background: rgba(122, 162, 255, 0.18);
    color: var(--accent, #7aa2ff);
    border: 1px solid var(--accent, #7aa2ff);
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
    font-size: 0.6rem;
    margin-left: auto;
  }
  .advanced-body {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-top: 0.25rem;
  }
</style>
