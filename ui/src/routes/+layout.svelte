<script lang="ts">
  import '../app.css';
  import Toasts from '$lib/Toasts.svelte';

  let { children } = $props();
</script>

<Toasts />

<header class="container topbar">
  <div class="row">
    <a href="/" class="logo">astrolab</a>
    <nav class="row nav">
      <a href="/">Library</a>
      <a href="/projects">Projects</a>
      <a href="/settings">Settings</a>
      <a href="/jobs" class="dim">Jobs</a>
    </nav>
    <span class="muted phase-pill">Phase 3</span>
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
    text-decoration: none;
    color: inherit;
  }
  .nav {
    gap: 1rem;
    margin-left: 1.5rem;
    flex: 1;
  }
  .nav a {
    color: var(--muted, #888);
    text-decoration: none;
    font-size: 0.9rem;
  }
  .nav a:hover {
    color: var(--fg, #ddd);
  }
  .nav a.dim {
    opacity: 0.55;
  }

  .phase-pill {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border);
  }
</style>
