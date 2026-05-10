<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import Toasts from '$lib/Toasts.svelte';

  let { children } = $props();

  // Playground routes paint their own shell; skip the app chrome there so
  // the theme picker isn't competing with the real app's topbar.
  const bare = $derived($page.url.pathname.startsWith('/playground'));
</script>

{#if bare}
  {@render children?.()}
{:else}
  <Toasts />

  <header class="container topbar">
    <a href="/" class="logo">astrolab</a>
    <nav class="nav">
      <a href="/" class:active={$page.url.pathname === '/'}>Library</a>
      <a href="/tonight" class:active={$page.url.pathname.startsWith('/tonight')}>Tonight</a>
      <a href="/projects" class:active={$page.url.pathname.startsWith('/projects')}>Projects</a>
      <a href="/gallery" class:active={$page.url.pathname.startsWith('/gallery')}>Gallery</a>
      <a href="/settings" class:active={$page.url.pathname.startsWith('/settings')}>Settings</a>
    </nav>
  </header>

  <main class="container">
    {@render children?.()}
  </main>
{/if}

<style>
  .topbar {
    padding-bottom: 0.25rem;
    padding-top: 0.85rem;
    display: flex;
    align-items: baseline;
    gap: 1.5rem;
  }

  /* Brand wordmark: italic Fraunces with the cyan→magenta gradient that
     the playground locked in. One serif voice on every page. */
  .logo {
    font-family: var(--font-display);
    font-style: italic;
    font-weight: 500;
    font-size: 1.25rem;
    letter-spacing: -0.005em;
    background: linear-gradient(90deg, var(--accent), var(--bad));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }

  .nav {
    display: flex;
    gap: 1.25rem;
    flex: 1;
  }
  .nav a {
    color: var(--fg-mute);
    font-size: 0.9rem;
    position: relative;
    padding: 0.25rem 0;
    transition: color 160ms ease;
  }
  .nav a:hover {
    color: var(--fg);
  }
  .nav a.active {
    color: var(--fg);
  }
  /* Subtle active-tab underline pulled in the accent color so the
     current page is unambiguous without shouting. */
  .nav a.active::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: -2px;
    height: 1px;
    background: linear-gradient(90deg, var(--accent), var(--bad));
    opacity: 0.7;
  }
</style>
