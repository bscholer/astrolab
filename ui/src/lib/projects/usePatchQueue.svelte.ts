/**
 * Debounced PATCH coalescing for node parameter overrides.
 *
 * Collects partial override maps from multiple slider/input events and
 * flushes them as a single API call after a quiet period. Keeps the
 * cache-bust count low when the user drags a slider across many values.
 */

import { api, type Project } from '$lib/api';
import { toast } from '$lib/toast.svelte';

const PATCH_DEBOUNCE_MS = 350;

export function createPatchQueue(getProject: () => Project | null) {
  let patching = $state(false);
  let patchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingOverrides: Record<string, Record<string, unknown> | null> | null = null;

  function onNodeOverrideChange(
    nodeId: string,
    partialForNode: Record<string, unknown>
  ) {
    if (!pendingOverrides) pendingOverrides = {};
    pendingOverrides[nodeId] =
      Object.keys(partialForNode).length === 0 ? null : partialForNode;
    if (patchTimer) clearTimeout(patchTimer);
    patchTimer = setTimeout(flush, PATCH_DEBOUNCE_MS);
  }

  async function flush(): Promise<Project | null> {
    patchTimer = null;
    const project = getProject();
    if (!project || !pendingOverrides) return null;
    const overrides = pendingOverrides;
    pendingOverrides = null;
    patching = true;
    try {
      return await api.patchProject(project.id, { overrides });
    } catch (e) {
      toast.error(`Couldn't apply changes: ${(e as Error).message}`);
      return null;
    } finally {
      patching = false;
    }
  }

  function cancel() {
    if (patchTimer) {
      clearTimeout(patchTimer);
      patchTimer = null;
    }
    pendingOverrides = null;
  }

  return {
    get patching() { return patching; },
    onNodeOverrideChange,
    flush,
    cancel,
  };
}
