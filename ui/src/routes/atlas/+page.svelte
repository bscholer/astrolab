<!--
  The atlas is now the Tonight page's map view. Old links and bookmarks
  land there, with their ?target= kept.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';

  onMount(() => {
    const params = new URLSearchParams(window.location.search);
    params.set('view', 'map');
    goto(`/tonight?${params}`, { replaceState: true });
  });
</script>
