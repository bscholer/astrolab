<!--
  Gallery — every successful render across every project, newest
  first. Each card links to the parent project; the cover renders
  with a small star marker so you can spot which one represents the
  project on the Projects list.

  Design alignment: friendly target name in serif, catalog code muted,
  template line in small caps, mono dates. Same vocabulary as the
  Projects rows; the gallery is just the photos-first cut of the
  same data.
-->
<script lang="ts">
  import { goto } from '$app/navigation';
  import { api, type GalleryEntry } from '$lib/api';
  import { toast } from '$lib/toast.svelte';
  import { shortAgo, templateDisplayName } from '$lib/format';

  let entries = $state<GalleryEntry[] | null>(null);

  async function load() {
    try {
      entries = await api.listGallery();
    } catch (e) {
      toast.error(`Couldn't load gallery: ${(e as Error).message}`);
    }
  }

  $effect(() => {
    load();
  });

  function shortDate(iso: string): string {
    return iso.slice(0, 10);
  }

  // Compare selection. `${project_id}:${seq}` keys, max two. Tracks
  // insertion order so the earlier pick lands on the A side of the
  // wipe (matches reading order — left → right, A → B).
  let picks = $state<string[]>([]);

  function entryKey(e: GalleryEntry): string {
    return `${e.project_id}:${e.seq}`;
  }

  function togglePick(e: GalleryEntry) {
    const key = entryKey(e);
    const idx = picks.indexOf(key);
    if (idx >= 0) {
      picks = picks.filter((k) => k !== key);
      return;
    }
    if (picks.length >= 2) {
      // Drop the older pick to make room for the new one. Two-slot
      // queue — feels like the most predictable behavior since the
      // UI only renders two slots anyway.
      picks = [picks[1], key];
    } else {
      picks = [...picks, key];
    }
  }

  function openCompare() {
    if (picks.length !== 2) return;
    goto(`/compare?a=${picks[0]}&b=${picks[1]}`);
  }
</script>

<div class="header">
  <h1>Gallery</h1>
  <span class="muted small">
    {entries === null ? '' : `${entries.length} render${entries.length === 1 ? '' : 's'}`}
  </span>
</div>

{#if entries === null}
  <p class="muted">Loading…</p>
{:else if entries.length === 0}
  <p class="muted">
    No saved renders yet. Run a project from the
    <a href="/" class="link">Library</a> and your finished versions
    will land here.
  </p>
{:else}
  <ul class="grid">
    {#each entries as e, i (`${e.project_id}-${e.seq}`)}
      {@const key = `${e.project_id}:${e.seq}`}
      {@const pickIdx = picks.indexOf(key)}
      <li class="card" class:picked={pickIdx >= 0} style="--stagger: {i}">
        <a class="card-link" href="/projects/{e.project_id}">
          <div class="card-thumb">
            <img
              class="card-img"
              src={api.previewUrl(e.preview_hash, e.preview_port)}
              alt="{e.project_name} v{e.seq + 1}"
              loading="lazy"
              decoding="async"
            />
            <span class="version-chip">v{e.seq + 1}</span>
            {#if e.is_cover}
              <span class="cover-marker" title="Project cover">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" aria-hidden="true">
                  <path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z" />
                </svg>
              </span>
            {/if}
            <!-- Compare toggle. Lives on the thumb so it's reachable
                 without leaving the card; the link wraps the whole
                 card so we stop the click from also navigating. -->
            <button
              type="button"
              class="pick-btn"
              class:active={pickIdx >= 0}
              onclick={(ev) => { ev.preventDefault(); togglePick(e); }}
              aria-pressed={pickIdx >= 0}
              aria-label={pickIdx >= 0 ? `Picked as ${pickIdx === 0 ? 'A' : 'B'}` : 'Pick for compare'}
              title={pickIdx >= 0 ? `Picked as ${pickIdx === 0 ? 'A' : 'B'}` : 'Pick for compare'}
            >
              {#if pickIdx >= 0}
                {pickIdx === 0 ? 'A' : 'B'}
              {:else}
                <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <rect x="3" y="3" width="8" height="18" rx="1.5" />
                  <rect x="13" y="3" width="8" height="18" rx="1.5" />
                </svg>
              {/if}
            </button>
          </div>
          <div class="card-body">
            <div class="card-name">
              {#if e.target_common_name && e.target_common_name !== e.project_name}
                {e.target_common_name}
                <span class="card-cat muted">{e.project_name}</span>
              {:else}
                {e.project_name}
              {/if}
            </div>
            <div class="card-template">{templateDisplayName(e.template_id)}</div>
            <div class="card-foot muted small">
              <span class="num" title={e.created_at}>{shortDate(e.created_at)}</span>
              <span class="dot" aria-hidden="true">·</span>
              <span class="num">{shortAgo(e.created_at)}</span>
            </div>
          </div>
        </a>
      </li>
    {/each}
  </ul>

  <!-- Floating selection tray. Appears once the user picks something;
       turns into a primary CTA at two picks. -->
  {#if picks.length > 0}
    <div class="tray" role="region" aria-label="Compare selection">
      <span class="tray-label muted small">
        {#if picks.length === 1}
          1 picked — choose another to compare
        {:else}
          2 picked — ready to compare
        {/if}
      </span>
      <button type="button" class="tray-btn" onclick={() => (picks = [])}>
        Clear
      </button>
      <button
        type="button"
        class="tray-btn primary"
        disabled={picks.length !== 2}
        onclick={openCompare}
      >
        Open compare
      </button>
    </div>
  {/if}
{/if}

<style>
  .header {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    margin: 0.5rem 0 1.25rem;
  }
  .header h1 {
    margin: 0;
    flex: 1;
    font-size: 1.5rem;
  }
  .muted { color: var(--fg-mute); }
  .small { font-size: 0.85em; }
  .link {
    color: var(--accent);
    text-decoration: underline;
    text-decoration-color: var(--accent-soft);
    text-underline-offset: 2px;
  }
  .link:hover { text-decoration-color: var(--accent); }

  .grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    /* Auto-fill keeps tiles around 280px on desktop, two-up on tablet,
       one-up on phones. The gallery wants generous space per card —
       this isn't a dense list. */
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 1rem;
  }

  .card {
    animation: rise-in 360ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
    animation-delay: calc(var(--stagger, 0) * 35ms + 60ms);
  }
  .card-link {
    display: flex;
    flex-direction: column;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    overflow: hidden;
    color: inherit;
    text-decoration: none;
    box-shadow: var(--shadow);
    transition: border-color 160ms ease, transform 160ms ease;
  }
  .card-link:hover {
    border-color: var(--border-strong);
    transform: translateY(-2px);
  }
  .card-link:hover .card-img {
    transform: scale(1.03);
    filter: saturate(1.1) brightness(1.05);
  }

  .card-thumb {
    position: relative;
    aspect-ratio: 4 / 3;
    background: var(--bg-elev-2);
    overflow: hidden;
  }
  .card-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: transform 280ms cubic-bezier(0.2, 0.8, 0.2, 1), filter 280ms ease;
  }

  /* Version chip, mirrors the projects-list pattern. */
  .version-chip {
    position: absolute;
    bottom: 6px;
    right: 8px;
    padding: 0.1rem 0.5rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.92);
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    border-radius: 999px;
  }
  /* Cover marker — small accent star in the top-left to match the
     'Set as cover' button on the project detail page. */
  .cover-marker {
    position: absolute;
    top: 6px;
    left: 8px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 999px;
    background: linear-gradient(135deg, var(--accent), var(--good));
    color: var(--accent-ink);
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 12px var(--accent-soft);
  }

  .card-body {
    padding: 0.75rem 0.95rem 0.95rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .card-name {
    /* Same serif treatment as project rows / library cards. */
    font-family: var(--font-display);
    font-weight: 500;
    font-size: 1.1rem;
    letter-spacing: -0.01em;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .card-cat {
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 0.78rem;
    font-variant-numeric: tabular-nums;
    position: relative;
    top: -0.1em;
  }
  .card-template {
    font-size: 0.82rem;
    color: var(--fg);
    opacity: 0.85;
    font-variant: small-caps;
    letter-spacing: 0.02em;
  }
  .card-foot {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: baseline;
    font-size: 0.75rem;
  }
  .card-foot .dot { opacity: 0.5; }
  .num {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* Highlight a card that's been picked for compare. The accent
     border doubles as a visual cue alongside the A/B chip. */
  .card.picked .card-link {
    border-color: var(--accent);
    box-shadow:
      0 0 0 1px var(--accent-soft),
      0 16px 36px rgba(0, 0, 0, 0.5),
      0 0 24px var(--accent-soft);
  }

  /* Pick button — sits in the top-right of the thumb. Compact, reads
     as a chip, becomes a labeled A/B once picked. */
  .pick-btn {
    position: absolute;
    top: 6px;
    right: 8px;
    appearance: none;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid var(--border);
    color: rgba(255, 255, 255, 0.92);
    width: 26px;
    height: 22px;
    padding: 0 0.4rem;
    border-radius: 999px;
    font: inherit;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    transition: color 160ms ease, border-color 160ms ease, background 160ms ease;
  }
  .pick-btn:hover {
    color: var(--accent);
    border-color: var(--accent);
  }
  .pick-btn.active {
    background: linear-gradient(135deg, var(--accent), var(--bad));
    color: var(--accent-ink);
    border-color: transparent;
    width: auto;
    min-width: 26px;
  }

  /* Floating compare tray — bottom-right, slides up via the rise-in
     keyframe in app.css. */
  .tray {
    position: fixed;
    right: max(1rem, env(safe-area-inset-right));
    bottom: max(1rem, env(safe-area-inset-bottom));
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.6rem 0.8rem;
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    box-shadow: var(--shadow);
    z-index: 50;
    animation: rise-in 240ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
  }
  .tray-label {
    padding: 0 0.3rem;
  }
  .tray-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 0.3rem 0.85rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
    transition: border-color 160ms ease, color 160ms ease, transform 160ms ease;
  }
  .tray-btn:hover:not(:disabled) {
    border-color: var(--accent);
    color: var(--accent);
  }
  .tray-btn.primary {
    background: linear-gradient(135deg, var(--accent), var(--good));
    color: var(--accent-ink);
    border-color: transparent;
    font-weight: 600;
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 16px var(--accent-soft);
  }
  .tray-btn.primary:hover:not(:disabled) {
    transform: translateY(-1px);
    filter: brightness(1.08);
  }
  .tray-btn:disabled { opacity: 0.55; cursor: not-allowed; }
</style>
