/**
 * Debounced PATCH coalescing for node parameter overrides.
 *
 * Collects partial override maps from multiple slider/input events and
 * flushes them as a single API call after a quiet period. Keeps the
 * cache-bust count low when the user drags a slider across many values.
 *
 * Callers pass the full intended snapshot of overrides for a node (the
 * map of fields they want overridden, with default-matching fields
 * omitted). The queue diffs that against the project's current
 * overrides for the node and emits explicit `null` entries for any
 * fields that disappeared since last commit. The server's merge
 * semantics treat `null` as "reset this param to template default" and
 * a missing key as "leave alone", so without the explicit-null step
 * the reset button (and any "slider returned to default" event) would
 * silently no-op.
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
    snapshotForNode: Record<string, unknown>
  ) {
    const project = getProject();
    const serverPrev = (project?.current_overrides?.[nodeId] as
      | Record<string, unknown>
      | undefined) ?? {};
    // Folding any already-queued partial gives the effective post-flush
    // state, which is what the user just diffed against on screen.
    const queuedPartial = pendingOverrides?.[nodeId] ?? null;
    const effectivePrev: Record<string, unknown> = { ...serverPrev };
    if (queuedPartial) {
      for (const [k, v] of Object.entries(queuedPartial)) {
        if (v === null) delete effectivePrev[k];
        else effectivePrev[k] = v;
      }
    }

    const diff: Record<string, unknown> = {};
    for (const k of Object.keys(effectivePrev)) {
      if (!(k in snapshotForNode)) diff[k] = null;
    }
    for (const [k, v] of Object.entries(snapshotForNode)) {
      if (effectivePrev[k] !== v) diff[k] = v;
    }
    if (Object.keys(diff).length === 0) return;

    if (!pendingOverrides) pendingOverrides = {};
    const merged = { ...(queuedPartial ?? {}), ...diff };
    pendingOverrides[nodeId] = merged;
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
