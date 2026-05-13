<!--
  Checkbox: a square, custom-styled checkbox used throughout the app.

  Renders a 16x16 box (border-radius: 3px) with an SVG checkmark on
  checked state. Accepts the same attributes as a native checkbox so
  callers can bind:checked, set disabled, and attach onchange handlers.
  Scoped styles guarantee it looks the same regardless of where it
  lands in the component tree.
-->
<script lang="ts">
  type Props = {
    checked?: boolean;
    disabled?: boolean;
    onchange?: (checked: boolean) => void;
    title?: string;
    id?: string;
  };

  let {
    checked = false,
    disabled = false,
    onchange,
    title,
    id,
  }: Props = $props();
</script>

<input
  type="checkbox"
  class="checkbox"
  {checked}
  {disabled}
  {title}
  {id}
  onchange={(e) => onchange?.((e.currentTarget as HTMLInputElement).checked)}
/>

<style>
  .checkbox {
    appearance: none;
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    min-width: 16px;
    min-height: 16px;
    margin: 0;
    padding: 0;
    border: 1.5px solid rgba(255, 255, 255, 0.22);
    border-radius: 3px;
    background: var(--bg);
    cursor: pointer;
    display: inline-grid;
    place-content: center;
    transition: background-color 140ms ease, border-color 140ms ease;
    flex-shrink: 0;
  }

  .checkbox::before {
    content: '';
    width: 10px;
    height: 10px;
    transform: scale(0);
    background-color: var(--accent-ink);
    clip-path: polygon(14% 44%, 0 60%, 40% 100%, 100% 20%, 80% 6%, 38% 70%);
    transition: transform 140ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }

  .checkbox:checked {
    background: var(--accent);
    border-color: var(--accent);
  }

  .checkbox:checked::before {
    transform: scale(1);
  }

  .checkbox:hover:not(:disabled) {
    border-color: var(--accent);
  }

  .checkbox:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .checkbox:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
</style>
