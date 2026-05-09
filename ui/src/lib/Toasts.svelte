<script lang="ts">
  import { fly, fade } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { toast } from '$lib/toast.svelte';
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toast.items as t (t.id)}
    <div
      class="toast toast-{t.kind}"
      in:fly={{ x: 20, duration: 180, easing: cubicOut }}
      out:fade={{ duration: 220 }}
    >
      <span class="msg">{t.message}</span>
      <button
        type="button"
        class="dismiss"
        aria-label="dismiss"
        onclick={() => toast.dismiss(t.id)}
      >×</button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    top: 1rem;
    right: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-width: min(420px, 90vw);
    z-index: 100;
    pointer-events: none;
  }
  .toast {
    pointer-events: auto;
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.6rem 0.75rem;
    border-radius: 8px;
    border: 1px solid var(--border, #444);
    background: var(--bg-elev, #1a1d24);
    color: var(--fg, #ddd);
    font-size: 0.9rem;
    line-height: 1.4;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
  }
  .toast-error {
    border-color: var(--bad, #f88);
    background: linear-gradient(180deg, rgba(255,122,138,0.08), var(--bg-elev, #1a1d24));
  }
  .toast-success {
    border-color: var(--good, #5ed3a8);
  }
  .toast-info {
    border-color: var(--accent, #7aa2ff);
  }
  .msg {
    flex: 1;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .dismiss {
    appearance: none;
    background: transparent;
    color: inherit;
    border: none;
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    opacity: 0.6;
    padding: 0 0.2rem;
  }
  .dismiss:hover { opacity: 1; }
</style>
