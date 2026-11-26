<!--
  Home: Projects once there is something to finish, the Library before that
  (a fresh install starts by pointing it at captures and scanning). Older
  links to the Library's tabs (/?view=storage) still land on them.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { api } from '$lib/api';

  onMount(async () => {
    const view = $page.url.searchParams.get('view');
    if (view) {
      goto(`/library?view=${encodeURIComponent(view)}`, { replaceState: true });
      return;
    }
    let hasProjects = false;
    try {
      hasProjects = (await api.listProjects()).length > 0;
    } catch {
      // Can't tell; the Library works either way.
    }
    goto(hasProjects ? '/projects' : '/library', { replaceState: true });
  });
</script>
