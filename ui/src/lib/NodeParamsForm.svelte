<!--
  Auto-generated parameter form for a single node, driven by the JSON
  Schema returned by GET /api/templates/{id}/schema.

  We render one control per top-level property. Type heuristics:
    - enum         -> <select>
    - integer/number with min+max -> <input type=range> + numeric display
    - integer/number without bounds -> <input type=number>
    - boolean      -> toggle (<input type=checkbox>)
    - string       -> <input type=text>

  The component does NOT submit on its own; it emits 'change' events with
  the new partial overrides for this node, so the parent can debounce and
  PATCH the rendering.

  Props:
    nodeId            stable id used in override dispatch
    schemaProps       object map: paramName -> JSONSchemaField
    defaults          object map of template-merged defaults
    overrides         current live override values (from rendering history)
    cost              cost class for this node's downstream closure (badge)
    onchange(partial) called whenever any control fires; partial is the new
                      *full* override dict for this node (not just the diff
                      that just changed)
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
  }

  const {
    nodeId,
    schemaProps,
    defaults,
    overrides,
    cost,
    onchange
  }: Props = $props();

  // Effective value for a param: override wins, then default. We bind UI
  // controls to local copies so typing into a number field doesn't snap on
  // every keystroke; the parent debounces the resulting change events.
  function effective(name: string): unknown {
    return name in overrides ? overrides[name] : defaults[name];
  }

  function emit(name: string, value: unknown) {
    // Only mark as an override if it differs from the default. Equal values
    // get dropped so we don't accumulate dead override entries (and the
    // hash stays clean).
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
    // Float fields: derive from hash_precision (Pydantic Field json_schema_extra).
    // Integer fields step by 1. Anything else: take the smaller of (1/100 of
    // range, 0.1) so wide [0..1000] ranges still feel responsive.
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
</script>

<div class="form">
  <header class="form-head">
    <span class="cost-pill cost-{cost}" title="Editing this node re-runs {cost} downstream work">
      {cost}
    </span>
    <span class="muted small">{nodeId}</span>
  </header>

  {#if Object.keys(schemaProps).length === 0}
    <p class="muted small">No editable parameters on this node.</p>
  {:else}
    {#each Object.entries(schemaProps) as [name, field] (name)}
      {@const ft = fieldType(field)}
      {@const val = effective(name)}
      {@const overridden = isOverridden(name)}
      <div class="param" class:overridden>
        <div class="param-head">
          <label for="{nodeId}-{name}">{name}</label>
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
          <div class="range-wrap">
            <input
              id="{nodeId}-{name}"
              type="range"
              min={field.minimum ?? field.exclusiveMinimum}
              max={field.maximum ?? field.exclusiveMaximum}
              step={rangeStep(field)}
              value={val as number}
              oninput={(e) => emit(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
            />
            <input
              type="number"
              class="range-num"
              min={field.minimum ?? field.exclusiveMinimum}
              max={field.maximum ?? field.exclusiveMaximum}
              step={rangeStep(field)}
              value={val as number}
              oninput={(e) => emit(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
            />
          </div>
        {:else if ft === 'number'}
          <input
            id="{nodeId}-{name}"
            type="number"
            value={val as number}
            oninput={(e) => emit(name, parseFloat((e.currentTarget as HTMLInputElement).value))}
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

        {#if field.description}
          <p class="param-help muted small">{field.description}</p>
        {/if}
      </div>
    {/each}
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
    /* A subtle accent on the left edge so users can see what's been touched
       at a glance, without re-coloring every value. */
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

  .param-help {
    font-size: 0.72rem;
    line-height: 1.3;
    margin: 0.1rem 0 0;
  }
</style>
