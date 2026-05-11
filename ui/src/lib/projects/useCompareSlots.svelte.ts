/**
 * Two-slot compare state: arming/unarming A and B, fetching previews
 * on-demand (memoized by job_id), and the open/close flag for the modal.
 */

import { api, type Project } from '$lib/api';

export function createCompareSlots(getProject: () => Project | null) {
  let compareA = $state<number | null>(null);
  let compareB = $state<number | null>(null);
  let compareOpen = $state(false);
  // Memoized by job_id.
  let comparePreviews = $state<Record<string, { hash: string; port: string } | null>>({});
  let compareLoading = $state(false);

  async function ensurePreviewForSeq(seq: number): Promise<{ hash: string; port: string } | null> {
    const project = getProject();
    if (!project) return null;
    const entry = project.history.find((h) => h.seq === seq);
    if (!entry) return null;
    const cached = comparePreviews[entry.job_id];
    if (cached !== undefined) return cached;
    try {
      const job = await api.getJob(entry.job_id);
      if (!job.outputs) {
        comparePreviews = { ...comparePreviews, [entry.job_id]: null };
        return null;
      }
      // Prefer 'image' port to match what gallery cards surface.
      const port = 'image' in job.outputs ? 'image' : Object.keys(job.outputs)[0];
      const ref = job.outputs[port];
      const out = ref ? { hash: ref.node_hash, port } : null;
      comparePreviews = { ...comparePreviews, [entry.job_id]: out };
      return out;
    } catch {
      comparePreviews = { ...comparePreviews, [entry.job_id]: null };
      return null;
    }
  }

  /**
   * State machine for compare slot clicks:
   *   neither armed -> arm A
   *   A armed, same seq -> unarm A
   *   A armed, different seq -> arm B + open modal
   *   both armed, A's seq -> unarm A (modal stays if B remains)
   *   both armed, B's seq -> unarm B + close modal
   *   both armed, third seq -> replace B
   */
  async function toggleCompareSlot(seq: number) {
    if (compareLoading) return;
    if (compareA === null) {
      compareLoading = true;
      try {
        await ensurePreviewForSeq(seq);
        compareA = seq;
      } finally {
        compareLoading = false;
      }
      return;
    }
    if (compareA === seq) {
      compareA = null;
      compareOpen = false;
      return;
    }
    if (compareB === seq) {
      compareB = null;
      compareOpen = false;
      return;
    }
    compareLoading = true;
    try {
      await ensurePreviewForSeq(seq);
      compareB = seq;
      compareOpen = true;
    } finally {
      compareLoading = false;
    }
  }

  function clear() {
    compareA = null;
    compareB = null;
    compareOpen = false;
  }

  return {
    get compareA() { return compareA; },
    get compareB() { return compareB; },
    get compareOpen() { return compareOpen; },
    set compareOpen(v: boolean) { compareOpen = v; },
    get comparePreviews() { return comparePreviews; },
    get compareLoading() { return compareLoading; },
    toggleCompareSlot,
    clear,
  };
}
