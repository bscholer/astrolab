<!--
  CompareController: compare wipe modal.

  The page owns compareA, compareB, comparePreviews, and compareLoading.
  This component renders the modal overlay when both slots are filled and
  compareOpen is true. It also exposes ensurePreviewForSeq so the page can
  call it before setting a slot.
-->
<script lang="ts">
  import { api, type Project } from '$lib/api';
  import CompareSlider from '$lib/CompareSlider.svelte';

  type Props = {
    project: Project | null;
    compareA: number | null;
    compareB: number | null;
    compareOpen: boolean;
    comparePreviews: Record<string, { hash: string; port: string } | null>;
    onClose: () => void;
  };

  let {
    project,
    compareA,
    compareB,
    compareOpen,
    comparePreviews,
    onClose,
  }: Props = $props();

  function compareSrcFor(seq: number | null): string | null {
    if (seq === null || !project) return null;
    const entry = project.history.find((h) => h.seq === seq);
    if (!entry) return null;
    const cached = comparePreviews[entry.job_id];
    if (!cached) return null;
    return api.previewUrl(cached.hash, cached.port);
  }

  function onModalKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  }
</script>

{#if compareOpen && project && compareA !== null && compareB !== null}
  {@const aSrc = compareSrcFor(compareA)}
  {@const bSrc = compareSrcFor(compareB)}
  {@const aEntry = project.history.find((h) => h.seq === compareA)}
  {@const bEntry = project.history.find((h) => h.seq === compareB)}
  <!-- Modal lives outside .project-root so the backdrop covers the full viewport. -->
  <div
    class="compare-backdrop"
    role="presentation"
    onclick={onClose}
    onkeydown={onModalKeydown}
  >
    <div
      class="compare-dialog"
      role="dialog"
      tabindex="-1"
      aria-modal="true"
      aria-label="Compare history versions"
      onclick={(e) => e.stopPropagation()}
      onkeydown={onModalKeydown}
    >
      <header class="compare-dialog-head">
        <span class="muted small">Compare</span>
        <span class="compare-titles">
          <span class="slot a">A · v{compareA + 1}</span>
          <span class="muted">vs</span>
          <span class="slot b">B · v{compareB + 1}</span>
        </span>
        <button type="button" class="ghost-btn" onclick={onClose} aria-label="Close compare">
          ✕
        </button>
      </header>
      {#if aSrc && bSrc}
        <CompareSlider
          {aSrc}
          {bSrc}
          aLabel={aEntry?.label ?? `v${compareA + 1}`}
          bLabel={bEntry?.label ?? `v${compareB + 1}`}
        />
      {:else}
        <p class="muted no-preview">
          Couldn't load one of the previews. The job may have failed or its
          cache may have been evicted.
        </p>
      {/if}
    </div>
  </div>
{/if}

<style>
  .compare-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.62);
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
    z-index: 100;
    display: grid;
    place-items: center;
    padding: 1.5rem;
  }
  .compare-dialog {
    background: var(--bg-elev-1, #14182b);
    border: 1px solid var(--border, #333);
    border-radius: var(--radius-card, 10px);
    box-shadow: 0 24px 48px rgba(0, 0, 0, 0.5);
    width: min(96vw, 1100px);
    max-height: 92vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .compare-dialog-head {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.6rem 0.9rem;
    border-bottom: 1px solid var(--border, #333);
  }
  .compare-titles {
    flex: 1;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.95rem;
  }
  .compare-titles .slot {
    font-family: var(--font-mono, monospace);
    font-size: 0.85rem;
  }
  .compare-titles .slot.a { color: var(--accent, #5eead4); }
  .compare-titles .slot.b { color: var(--bad, #ef4444); }
  .no-preview {
    padding: 1rem;
    color: var(--fg-mute, #888);
  }
  .muted { color: var(--fg-mute, #888); }
  .small { font-size: 0.85em; }
  .ghost-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border, #333);
    color: var(--fg, #ddd);
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .ghost-btn:hover {
    background: rgba(255, 255, 255, 0.04);
    border-color: var(--accent, #5eead4);
    color: var(--accent, #5eead4);
  }
</style>
