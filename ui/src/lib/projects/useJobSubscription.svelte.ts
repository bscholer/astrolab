/**
 * WebSocket subscription lifecycle for a single active job.
 *
 * Owns: attach/detach, WS open/close, event replay, terminal-event refresh.
 * Does NOT own pipeline state — caller passes applyEvent and an onJobDone
 * callback so the state objects live at the page level.
 */

import { api, type JobEvent, type JobSummary } from '$lib/api';
import { toast } from '$lib/toast.svelte';

export function createJobSubscription(
  applyEvent: (ev: JobEvent, onJobTerminal?: () => void) => void
) {
  let activeJob = $state<JobSummary | null>(null);
  let ws: WebSocket | null = null;
  let subscribedJobId: string | null = null;

  function refreshActiveJob() {
    if (!activeJob?.id) return;
    api.getJob(activeJob.id)
      .then((j) => (activeJob = j))
      .catch(() => undefined);
  }

  async function attachToJob(jobId: string) {
    if (subscribedJobId === jobId) return;
    // Keep previous job visible while the new fetch resolves — no flash.
    detachFromJob({ clearActiveJob: false });
    subscribedJobId = jobId;
    try {
      const fresh = await api.getJob(jobId);
      if (subscribedJobId !== jobId) return;
      activeJob = fresh;
      const history = await api.getJobEvents(jobId);
      if (subscribedJobId !== jobId) return;
      for (const ev of history) applyEvent(ev, refreshActiveJob);
      if (fresh.status === 'queued' || fresh.status === 'running') {
        ws = api.subscribeJobEvents(jobId, (ev) => applyEvent(ev, refreshActiveJob));
      }
    } catch (e) {
      toast.error(`Couldn't load job ${jobId}: ${(e as Error).message}`);
    }
  }

  function detachFromJob({ clearActiveJob = true }: { clearActiveJob?: boolean } = {}) {
    ws?.close();
    ws = null;
    subscribedJobId = null;
    if (clearActiveJob) activeJob = null;
  }

  return {
    get activeJob() { return activeJob; },
    set activeJob(v: JobSummary | null) { activeJob = v; },
    attachToJob,
    detachFromJob,
  };
}
