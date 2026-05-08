<script lang="ts">
  import '../app.css';

  let { children } = $props();
</script>

<header class="container topbar">
  <div class="row">
    <span class="logo">astrolab</span>
    <span class="muted phase-pill">Phase 1 · Library</span>
  </div>
</header>

<main class="container">
  {@render children?.()}
</main>

<style>
  .topbar {
    padding-bottom: 0;
  }

  .logo {
    font-weight: 600;
    letter-spacing: 0.02em;
    font-size: 1.1rem;
  }

  .phase-pill {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border);
  }
</style>
