<!--
  The crop editor on its own, for trying it and for tests/crop-editor.spec.ts.
  ?hash= picks the node output to frame (the framing API must know it).
-->
<script lang="ts">
  import { page } from '$app/stores';
  import CropEditor from '$lib/CropEditor.svelte';
  import { api } from '$lib/api';

  const hash = $derived($page.url.searchParams.get('hash') ?? 'h1');
  let params = $state<Record<string, unknown>>({});
  let stored = $state('');
</script>

<main class="wrap">
  <CropEditor
    previewUrl={api.previewUrlStretched(hash, 'image')}
    {hash}
    port="image"
    {params}
    onparams={(next) => {
      params = next ?? {};
      stored = JSON.stringify(params);
    }}
  />
  <pre data-testid="stored">{stored}</pre>
</main>

<style>
  .wrap { max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
</style>
