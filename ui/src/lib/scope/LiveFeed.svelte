<!--
  One of the scope's cameras, live. The server turns the scope's RTSP video into MJPEG, which
  an <img> plays as it arrives. When that cannot start (the scope is not prepared, or has no
  video, like the fake scope), it shows stills instead and tries live again a minute later.
  While the tab is hidden it lets go of the stream.
-->
<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  let { kind }: { kind: 'wide' | 'tele' } = $props();

  const label = $derived(kind === 'wide' ? 'Wide' : 'Tele');
  const STILL_EVERY_MS = { wide: 8000, tele: 30000 };
  const LIVE_RETRY_MS = 60000;

  let mode = $state<'live' | 'still'>('live');
  let visible = $state(true);
  let liveKey = $state(Date.now());
  let stillTick = $state(Date.now());
  let stillFailed = $state(false);
  let timers: ReturnType<typeof setTimeout>[] = [];

  function later(fn: () => void, ms: number) {
    timers.push(setTimeout(fn, ms));
  }

  function liveFailed() {
    mode = 'still';
    stillTick = Date.now();
    later(() => {
      mode = 'live';
      liveKey = Date.now();
    }, LIVE_RETRY_MS);
  }

  function nextStill() {
    later(() => {
      if (mode !== 'still') return;
      if (document.hidden) return nextStill();
      stillTick = Date.now();
    }, STILL_EVERY_MS[kind]);
  }

  function stillError() {
    stillFailed = true;
    later(() => {
      stillFailed = false;
      stillTick = Date.now();
    }, 30000);
  }

  function onVisibility() {
    visible = !document.hidden;
    if (visible && mode === 'live') liveKey = Date.now(); // a fresh connection, not a stale one
  }

  onMount(() => {
    visible = !document.hidden;
    document.addEventListener('visibilitychange', onVisibility);
  });

  onDestroy(() => {
    document.removeEventListener('visibilitychange', onVisibility);
    timers.forEach(clearTimeout);
  });
</script>

<figure class="frame">
  {#if mode === 'live'}
    {#if visible}
      {#key liveKey}
        <img src="/api/scope/live/{kind}.mjpg?k={liveKey}" alt="Live {kind} view" onerror={liveFailed} />
      {/key}
    {/if}
  {:else if stillFailed}
    <div class="empty">No {kind} frame</div>
  {:else}
    <img
      src="/api/scope/frame?camera={kind}&t={stillTick}"
      alt="Latest {kind} frame"
      onload={nextStill}
      onerror={stillError}
    />
  {/if}
  <figcaption>
    {label} · {mode === 'live' ? 'live' : 'stills'}
  </figcaption>
</figure>
