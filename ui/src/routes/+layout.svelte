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
      <a href="/projects" class:active={$page.url.pathname.startsWith('/projects')}>Projects</a>
      <a href="/gallery" class:active={$page.url.pathname.startsWith('/gallery')}>Gallery</a>
      <a href="/tonight" class:active={$page.url.pathname.startsWith('/tonight')}>Tonight</a>
      <a href="/system" class:active={$page.url.pathname.startsWith('/system')}>System</a>
    </nav>
    <a
      href="/settings"
      class="settings-cog"
      class:active={$page.url.pathname.startsWith('/settings')}
      aria-label="Settings"
      title="Settings"
    >
      <svg
        viewBox="0 0 24 24"
        width="18"
        height="18"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.04 1.56V21a2 2 0 1 1-4 0v-.08a1.7 1.7 0 0 0-1.1-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06A2 2 0 1 1 4.13 16.93l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.04H3a2 2 0 1 1 0-4h.08a1.7 1.7 0 0 0 1.56-1.1 1.7 1.7 0 0 0-.34-1.87l-.06-.06A2 2 0 1 1 7.07 4.13l.06.06a1.7 1.7 0 0 0 1.87.34h.07A1.7 1.7 0 0 0 10.07 3V3a2 2 0 1 1 4 0v.08a1.7 1.7 0 0 0 1.04 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.07A1.7 1.7 0 0 0 21 10.07H21a2 2 0 1 1 0 4h-.08a1.7 1.7 0 0 0-1.56 1.04Z" />
      </svg>
    </a>
  </header>

  <main class="container">
    {@render children?.()}
  </main>

  <footer class="container footer">
    Powered by <a href="https://siril.org" target="_blank" rel="noopener">Siril</a>,
    <a href="https://www.graxpert.com" target="_blank" rel="noopener">GraXpert</a>, and
    <a href="https://www.starnetastro.com" target="_blank" rel="noopener">StarNet++</a>
  </footer>
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

  /* Settings cog: anchored to the right of the topbar so it reads as a
     persistent affordance rather than another nav tab. Same muted->fg
     hover treatment as the nav links so it still feels part of the
     toolbar family. */
  .settings-cog {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-mute);
    padding: 0.25rem;
    border-radius: var(--radius);
    transition: color 160ms ease, background-color 160ms ease;
  }
  .settings-cog:hover {
    color: var(--fg);
    background: var(--bg-elev-2);
  }
  .settings-cog.active {
    color: var(--accent);
  }

  .footer {
    padding-top: 1.5rem;
    padding-bottom: 1rem;
    font-size: 0.75rem;
    color: var(--fg-mute);
    opacity: 0.6;
  }
  .footer a {
    color: inherit;
  }
  .footer a:hover {
    color: var(--fg);
    opacity: 1;
  }
</style>
