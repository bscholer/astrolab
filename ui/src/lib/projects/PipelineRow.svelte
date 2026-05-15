<!--
  PipelineRow: one accordion node row (head card + expandable body).

  Head: 16:9 thumbnail (skeleton while loading), title overlay with
  status pill + modified badge + toggle switch + chevron.
  Body (expanded): CropEditor for crop nodes, NodeParamsForm for
  everything else, and the output actions for the final node.
-->
<script lang="ts">
  import { fade, slide } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { api, type Project, type TemplateNodeSchema, type JobQuality, type NodeWarning } from '$lib/api';
  import { isTogglable, nodeDisplayName } from '$lib/graph';
  import NodeParamsForm from '$lib/NodeParamsForm.svelte';
  import CropEditor from '$lib/CropEditor.svelte';

  type NodeStatus = 'pending' | 'running' | 'cached' | 'skipped' | 'completed' | 'failed';

  type Props = {
    nschema: TemplateNodeSchema;
    project: Project;
    status: NodeStatus;
    progress: { fraction: number; message: string } | undefined;
    hash: string | undefined;
    port: string;
    kind: string;
    /** Structured warnings the node surfaced via ctx.warn during its run
     * (or replayed from the cache on a cache-hit). Empty when the node
     * ran cleanly. */
    warnings: NodeWarning[];
    previewLoaded: boolean;
    isOutput: boolean;
    isExpanded: boolean;
    /** The hash for the node that feeds this node's image input (for CropEditor). */
    upstreamHash: string | undefined;
    upstreamPort: string | undefined;
    durationMs: number | undefined;
    isCover: boolean;
    coverBusy: boolean;
    onToggle: () => void;
    onPreviewLoad: () => void;
    onPreviewError: () => void;
    onNodeOverrideChange: (nodeId: string, partial: Record<string, unknown>) => void;
    onToggleEnabled: (
      nodeId: string,
      schemaProps: Record<string, { default?: unknown }>,
      fullDefaults: Record<string, unknown>,
      currentOverrides: Record<string, unknown>
    ) => void;
    onToggleCover: () => void;
    finalOutputPort: string | undefined;
    finalOutputRef: { node_hash: string; path: string } | undefined;
    jobQuality?: JobQuality | null;
  };

  let {
    nschema,
    project,
    status,
    progress,
    hash,
    port,
    kind,
    warnings,
    previewLoaded,
    isOutput,
    isExpanded,
    upstreamHash,
    upstreamPort,
    durationMs,
    isCover,
    coverBusy,
    onToggle,
    onPreviewLoad,
    onPreviewError,
    onNodeOverrideChange,
    onToggleEnabled,
    onToggleCover,
    finalOutputPort,
    finalOutputRef,
    jobQuality,
  }: Props = $props();

  let qualityOpen = $state(false);

  function fmtF(n: number | null | undefined): string {
    if (n === null || n === undefined || !Number.isFinite(n)) return '—';
    if (n === 0) return '0';
    // Decimal with trimmed trailing zeros. Scientific notation reads poorly;
    // pipeline values live in [0, 1] (normalized) so 6 decimals is enough to
    // not bottom out at 0.0000 for very small sigmas while keeping the
    // number scannable.
    return n.toFixed(6).replace(/\.?0+$/, '');
  }
  function fmtPct(n: number | null | undefined): string {
    if (n === null || n === undefined || !Number.isFinite(n)) return '—';
    return (n * 100).toFixed(2) + '%';
  }
  function fmtFwhm(n: number | null): string {
    if (n === null) return '—';
    return n.toFixed(2) + ' px';
  }

  const nid = $derived(nschema.node_id);
  const overrides = $derived(
    (project.current_overrides[nid] as Record<string, unknown>) ?? {}
  );
  const schemaProps = $derived(nschema.schema.properties ?? {});
  const fullDefaults = $derived(
    { ...nschema.defaults, ...nschema.template_params } as Record<string, unknown>
  );
  const togglable = $derived(isTogglable(schemaProps));
  const enabled = $derived(togglable ? effectiveEnabled(fullDefaults, overrides) : true);
  const modifiedCount = $derived(Object.keys(overrides).length);

  function effectiveEnabled(
    defaults: Record<string, unknown>,
    ov: Record<string, unknown>
  ): boolean {
    const v = 'enabled' in ov ? ov.enabled : defaults.enabled;
    return v === undefined ? true : Boolean(v);
  }

  function formatStepDuration(ms: number | undefined): string {
    if (ms === undefined) return '';
    if (ms < 1000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
    if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
    const m = Math.floor(ms / 60_000);
    const s = Math.round((ms % 60_000) / 1000);
    return `${m}m ${s}s`;
  }

  const upstreamPreviewUrl = $derived.by(() => {
    if (!upstreamHash) return null;
    // Use force_stretch so the crop editor always shows an autostretched
    // preview regardless of whether the upstream node (e.g. graxpert) marks
    // its output as display-ready linear data.
    return api.previewUrlStretched(upstreamHash, upstreamPort ?? 'image');
  });
</script>

<li
  class="node-row node-{status}"
  class:expanded={isExpanded}
  class:output={isOutput}
  class:disabled={togglable && !enabled}
  class:preview-hidden={nschema.preview_hidden}
  transition:fade={{ duration: 160, easing: cubicOut }}
>
  <div class="node-head">
    <!-- Invisible full-cover click target for expand. Sits at z:0 so
         visual content paints over it; visuals have pointer-events:none. -->
    <button
      type="button"
      class="head-expand"
      aria-expanded={isExpanded}
      aria-label={isExpanded
        ? `Collapse ${nodeDisplayName(kind, nid)}`
        : `Expand ${nodeDisplayName(kind, nid)}`}
      onclick={onToggle}
    ></button>

    {#if !nschema.preview_hidden}
      <div class="head-thumb">
        {#if (status === 'completed' || status === 'cached') && hash}
          {#if !previewLoaded}
            <div class="flow-skeleton" aria-hidden="true"></div>
          {/if}
          <img
            class="head-img"
            class:loaded={previewLoaded}
            src={api.previewUrl(hash, port)}
            alt=""
            loading="lazy"
            onload={onPreviewLoad}
            onerror={onPreviewError}
          />
          {#if !nschema.preview_display_ready && nschema.outputs?.[port] === 'image/fits'}
            <span class="preview-stretch-badge" title="Preview is auto-stretched for visibility — actual output may look different">&#8776;</span>
          {/if}
        {:else if status === 'running' && progress}
          <span class="head-pct">{Math.round((progress.fraction ?? 0) * 100)}%</span>
        {:else}
          <span class="head-status muted">{status}</span>
        {/if}
        {#if status === 'running' && progress}
          <div class="head-progress" style:width="{(progress.fraction ?? 0) * 100}%"></div>
        {/if}
      </div>
    {/if}

    <div class="head-overlay">
      <span class="head-name">{nodeDisplayName(kind, nid)}</span>
      <span class="status status-mini status-{togglable && !enabled ? 'off' : status}">
        {togglable && !enabled
          ? 'off'
          : status}{#if status === 'running' && progress && nschema.preview_hidden}<span class="dur"><span class="dur-sep" aria-hidden="true"></span>{Math.round((progress.fraction ?? 0) * 100)}%</span>{:else if (status === 'completed' || status === 'failed') && durationMs && enabled}<span class="dur"><span class="dur-sep" aria-hidden="true"></span>{formatStepDuration(durationMs)}</span>{/if}
      </span>
      {#if warnings && warnings.length > 0}
        <!-- Warning chip: triangle icon with a hover/focus popover
             listing every warning's kind + message. `tabindex=0` so
             keyboard users can :focus it for the same reveal, and
             `role=button` so screen readers announce it as
             interactive. -->
        <span
          class="warning-chip"
          tabindex="0"
          role="button"
          aria-label="{warnings.length} warning{warnings.length === 1 ? '' : 's'}: {warnings
            .map((w) => w.message)
            .join('; ')}"
        >
          <svg
            class="warning-icon"
            viewBox="0 0 24 24"
            width="14"
            height="14"
            aria-hidden="true"
          >
            <!-- Filled warning triangle with an exclamation
                 inside. Stroke + fill use currentColor so the
                 chip's amber palette drives the icon too. -->
            <path
              d="M12 3 L22 20 L2 20 Z"
              fill="currentColor"
              stroke="currentColor"
              stroke-width="1"
              stroke-linejoin="round"
            />
            <path
              d="M12 9 L12 14"
              stroke="#1a1300"
              stroke-width="2"
              stroke-linecap="round"
            />
            <circle cx="12" cy="17" r="1.1" fill="#1a1300" />
          </svg>
          {#if warnings.length > 1}<span class="warning-count">{warnings.length}</span>{/if}
          <span class="warning-pop" role="tooltip">
            <span class="warning-pop-head">
              {warnings.length} warning{warnings.length === 1 ? '' : 's'}
            </span>
            <ul class="warning-pop-list">
              {#each warnings as w, i (i)}
                <li class="warning-pop-item">
                  <span class="warning-pop-kind">{w.kind}</span>
                  <span class="warning-pop-msg">{w.message}</span>
                </li>
              {/each}
            </ul>
          </span>
        </span>
      {/if}
      {#if isOutput}
        <span class="output-tag">final</span>
      {/if}
      <span class="head-spacer"></span>
      {#if modifiedCount > 0}
        <span
          class="badge-modified"
          title="{modifiedCount} param{modifiedCount === 1 ? '' : 's'} modified"
        >●{modifiedCount}</span>
      {/if}
      {#if togglable}
        <button
          type="button"
          class="node-toggle"
          class:on={enabled}
          role="switch"
          aria-checked={enabled}
          aria-label={enabled
            ? `Disable ${nodeDisplayName(kind, nid)}`
            : `Enable ${nodeDisplayName(kind, nid)}`}
          title={enabled ? 'On - click to skip this step' : 'Off - click to run this step'}
          onclick={() => onToggleEnabled(nid, schemaProps, fullDefaults, overrides)}
        >
          <span class="node-toggle-knob"></span>
        </button>
      {/if}
      <svg
        class="chevron"
        class:rotated={isExpanded}
        viewBox="0 0 24 24"
        width="14"
        height="14"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </div>

    {#if nschema.preview_hidden && status === 'running' && progress}
      <div
        class="head-progress head-progress--bottom"
        style:width="{(progress.fraction ?? 0) * 100}%"
      ></div>
    {/if}
  </div>

  {#if isExpanded}
    <div
      class="node-body"
      class:body-output={isOutput}
      transition:slide={{ duration: 220, easing: cubicOut }}
    >
      {#if status !== 'pending'}
        <div class="step-status-bar">
          <span class="status status-mini status-{togglable && !enabled ? 'off' : status}">
            {togglable && !enabled ? 'off' : status}{#if (status === 'completed' || status === 'failed') && durationMs && enabled}<span class="dur"><span class="dur-sep" aria-hidden="true"></span>{formatStepDuration(durationMs)}</span>{/if}
          </span>
          {#if status === 'running' && progress?.message}
            <span class="step-status-msg muted small">{progress.message}</span>
          {/if}
          {#if status === 'failed' && !isOutput}
            <span class="step-status-msg muted small">see error below</span>
          {/if}
        </div>
      {/if}

      {#if isOutput && finalOutputRef && finalOutputPort}
        <div class="output-actions">
          <button
            type="button"
            class="cover-btn"
            class:active={isCover}
            disabled={coverBusy}
            onclick={onToggleCover}
            title={isCover
              ? 'This version is the project cover. Click to clear.'
              : 'Pin this version as the project cover'}
          >
            <svg viewBox="0 0 24 24" width="14" height="14" fill={isCover ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 17.27 18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z" />
            </svg>
            {isCover ? 'Cover' : 'Set as cover'}
          </button>
          <a
            class="cover-btn"
            href={api.outputUrl(finalOutputRef.node_hash, finalOutputPort)}
            target="_blank"
            rel="noopener"
            title="Open full-size in a new tab"
          >
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
              <polyline points="15 3 21 3 21 9" />
              <line x1="10" y1="14" x2="21" y2="3" />
            </svg>
            Open full
          </a>
        </div>
        {#if jobQuality}
          <button
            type="button"
            class="quality-toggle"
            onclick={() => (qualityOpen = !qualityOpen)}
            aria-expanded={qualityOpen}
          >
            <span class="quality-toggle-arrow" class:rotated={qualityOpen}>&#9656;</span>
            Quality
          </button>
          <div class="quality-drawer" class:open={qualityOpen} aria-hidden={!qualityOpen}>
            <div class="quality-grid">
              <span class="qg-section">Background</span>
              <span class="qg-label">BG sigma</span>
              <span class="qg-val">{fmtF(jobQuality.background.sigma)}</span>

              <span class="qg-section-cont"></span>
              <span class="qg-label">BG level</span>
              <span class="qg-val">{fmtF(jobQuality.background.estimated_level)}</span>

              <span class="qg-section">Stars</span>
              <span class="qg-label" class:qg-unavail={jobQuality.sharpness.fwhm_px === null}
                    title={jobQuality.sharpness.fwhm_px === null ? 'Siril findstar unavailable' : ''}>FWHM</span>
              <span class="qg-val" class:qg-unavail={jobQuality.sharpness.fwhm_px === null}
                    >{fmtFwhm(jobQuality.sharpness.fwhm_px)}</span>

              <span class="qg-section-cont"></span>
              <span class="qg-label" class:qg-unavail={jobQuality.sharpness.roundness === null}
                    title={jobQuality.sharpness.roundness === null ? 'Siril findstar unavailable' : ''}>Roundness</span>
              <span class="qg-val" class:qg-unavail={jobQuality.sharpness.roundness === null}
                    >{fmtF(jobQuality.sharpness.roundness)}</span>

              <span class="qg-section-cont"></span>
              <span class="qg-label">Star count</span>
              <span class="qg-val">{jobQuality.sharpness.star_count ?? '—'}</span>

              <span class="qg-section">Sharpness</span>
              <span class="qg-label">Lap. var</span>
              <span class="qg-val">{fmtF(jobQuality.sharpness.laplacian_variance)}</span>

              {#if jobQuality.color_balance.r_g_ratio !== undefined || jobQuality.color_balance.b_g_ratio !== undefined}
                <span class="qg-section">Color</span>
                <span class="qg-label">R/G</span>
                <span class="qg-val">{fmtF(jobQuality.color_balance.r_g_ratio)}</span>

                <span class="qg-section-cont"></span>
                <span class="qg-label">B/G</span>
                <span class="qg-val">{fmtF(jobQuality.color_balance.b_g_ratio)}</span>
              {/if}

              {#if jobQuality.channels.length > 0}
                <span class="qg-section">Integrity</span>
                <span class="qg-label">Sat. high</span>
                <span class="qg-val">{fmtPct(jobQuality.channels[0].clipped_high_pct)}</span>

                <span class="qg-section-cont"></span>
                <span class="qg-label">Sat. low</span>
                <span class="qg-val">{fmtPct(jobQuality.channels[0].clipped_low_pct)}</span>
              {/if}

              <span class="qg-section">Warnings</span>
              <span class="qg-warnings" style="grid-column: 2 / -1;">
                {jobQuality.siril_warnings.length > 0 ? jobQuality.siril_warnings.join(' · ') : 'none'}
              </span>
            </div>
          </div>
        {/if}
      {:else}
        {#if kind === 'crop'}
          {@const eff = (k: string) => (k in overrides ? overrides[k] : fullDefaults[k])}
          <div class="stage-params crop-host">
            <CropEditor
              previewUrl={upstreamPreviewUrl}
              enabled={Boolean(eff('enabled'))}
              x={Number(eff('x') ?? 0)}
              y={Number(eff('y') ?? 0)}
              width={Number(eff('width') ?? 1)}
              height={Number(eff('height') ?? 1)}
              onchange={(next) => {
                const partial: Record<string, unknown> = {};
                for (const [k, v] of Object.entries(next)) {
                  if (fullDefaults[k] !== v) partial[k] = v;
                }
                onNodeOverrideChange(nid, partial);
              }}
              onreset={() => onNodeOverrideChange(nid, {})}
            />
          </div>
        {:else if Object.keys(schemaProps).length > 0}
          <div class="stage-params">
            <NodeParamsForm
              nodeId={nid}
              {schemaProps}
              defaults={fullDefaults}
              {overrides}
              hideFields={togglable ? ['enabled'] : []}
              onchange={(next) => onNodeOverrideChange(nid, next)}
            />
          </div>
        {:else}
          <p class="muted small no-params">No editable parameters.</p>
        {/if}
      {/if}
    </div>
  {/if}
</li>

<style>
  /* ---------- Node row card ---------- */

  .node-row {
    background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    transition: border-color 160ms ease, transform 160ms ease;
  }
  .node-row:hover { border-color: rgba(94, 234, 212, 0.45); }
  .node-row:hover:not(.expanded) { transform: translateY(-1px); }
  .node-row.expanded { border-color: rgba(94, 234, 212, 0.45); }
  /* Failed nodes get the red/pink border to signal an error state;
     all other states stay on the teal track. */
  .node-row.node-failed { border-color: rgba(236, 72, 153, 0.55); }
  .node-row.node-failed:hover { border-color: rgba(236, 72, 153, 0.75); }
  .node-row.output { box-shadow: 0 0 0 1px var(--accent-soft); }
  .node-row.output.expanded {
    box-shadow: 0 0 0 1px var(--accent-soft), 0 0 24px rgba(94, 234, 212, 0.06);
  }

  /* ---------- Head ---------- */

  .node-head {
    background: transparent;
    color: inherit;
    width: 100%;
    text-align: left;
    border-radius: 0;
    position: relative;
    display: block;
  }
  .head-expand {
    position: absolute;
    inset: 0;
    appearance: none;
    background: transparent;
    border: 0;
    padding: 0;
    margin: 0;
    cursor: pointer;
    z-index: 0;
  }
  .head-expand:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .head-thumb {
    position: relative;
    aspect-ratio: 16 / 9;
    background: var(--bg-elev-2);
    overflow: hidden;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
    transition: opacity 220ms ease, filter 220ms ease;
  }
  .head-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    opacity: 0;
    transition: opacity 280ms ease;
  }
  .head-img.loaded { opacity: 1; }
  .head-pct {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--accent);
    font-size: 1.15rem;
  }
  .head-status {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  @keyframes bar-shimmer {
    0%   { transform: translateX(-100%); }
    100% { transform: translateX(400%); }
  }

  .head-progress {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 2px;
    background: var(--accent);
    transition: width 200ms ease;
    z-index: 2;
    overflow: hidden;
  }
  .head-progress::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.35) 50%, transparent 100%);
    animation: bar-shimmer 2.2s linear infinite;
  }
  .preview-stretch-badge {
    position: absolute;
    bottom: 3px;
    right: 3px;
    background: rgba(0, 0, 0, 0.55);
    color: rgba(255, 255, 255, 0.7);
    font-size: 9px;
    line-height: 1;
    padding: 2px 3px;
    border-radius: 3px;
    user-select: none;
    pointer-events: auto;
    z-index: 2;
  }
  .head-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    padding: 0.45rem 0.6rem 0.85rem;
    background: linear-gradient(to bottom, rgba(0, 0, 0, 0.85) 30%, rgba(0, 0, 0, 0));
    color: #fff;
    display: flex;
    align-items: center;
    gap: 0.45rem;
    pointer-events: none;
    z-index: 1;
  }
  .head-name {
    font-weight: 600;
    font-size: 0.95rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.6);
    min-width: 0;
    flex-shrink: 1;
  }
  .head-spacer { flex: 1 1 0; min-width: 0.25rem; }
  .head-overlay > .status,
  .head-overlay > .badge-modified,
  .head-overlay > .output-tag,
  .head-overlay > .warning-chip,
  .head-overlay > .chevron { flex-shrink: 0; }
  .output-tag {
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--accent);
    background: var(--accent-soft);
    border: 1px solid var(--accent);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
  }

  /* Warning chip: signals a non-fatal partial-success path during the
     node's run (e.g. calibrate used an out-of-tolerance dark or
     dropped uncalibratable frames). Visually distinct enough from the
     status pill that the eye picks it up without making the row feel
     broken. */
  .warning-chip {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 600;
    color: #f5b300;
    background: rgba(245, 179, 0, 0.12);
    border: 1px solid rgba(245, 179, 0, 0.65);
    padding: 0.05rem 0.45rem;
    border-radius: 999px;
    cursor: help;
    outline: none;
  }
  .warning-chip:focus-visible {
    box-shadow: 0 0 0 2px rgba(245, 179, 0, 0.4);
  }
  .warning-icon {
    display: inline-block;
    width: 0.95rem;
    height: 0.95rem;
    /* color: currentColor flows from .warning-chip's amber into the
       SVG's fill/stroke so the icon matches the chip without
       hard-coding twice. */
  }
  .warning-count {
    font-variant-numeric: tabular-nums;
  }

  /* Hover/focus popover anchored to the chip. Hidden by default;
     animated in on .warning-chip:hover / :focus / :focus-within. The
     z-index has to clear the head-overlay's stacking context;
     pointer-events:none on the closed state keeps it from blocking
     clicks on the row underneath. */
  .warning-pop {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    z-index: 50;
    min-width: 18rem;
    max-width: 26rem;
    padding: 0.55rem 0.7rem;
    background: rgba(20, 16, 4, 0.96);
    color: #f5e9c5;
    border: 1px solid rgba(245, 179, 0, 0.55);
    border-radius: 6px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
    font-family: var(--font-sans, system-ui);
    font-weight: 400;
    font-size: 0.78rem;
    line-height: 1.4;
    opacity: 0;
    transform: translateY(-2px);
    pointer-events: none;
    transition: opacity 120ms ease, transform 120ms ease;
    white-space: normal;
  }
  .warning-chip:hover .warning-pop,
  .warning-chip:focus .warning-pop,
  .warning-chip:focus-within .warning-pop {
    opacity: 1;
    transform: translateY(0);
    pointer-events: auto;
  }
  .warning-pop-head {
    display: block;
    margin-bottom: 0.3rem;
    font-weight: 600;
    color: #f5b300;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .warning-pop-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .warning-pop-item {
    display: flex;
    gap: 0.45rem;
    align-items: baseline;
  }
  .warning-pop-kind {
    flex-shrink: 0;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #1a1300;
    background: #f5b300;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
  }
  .warning-pop-msg {
    flex: 1 1 auto;
    word-break: break-word;
  }
  .chevron {
    color: var(--fg-mute);
    transition: transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
    flex-shrink: 0;
  }
  .chevron.rotated { transform: rotate(180deg); }
  .node-row:not(.expanded) .chevron { color: rgba(255, 255, 255, 0.7); }

  /* ---------- Hidden-preview variant ----------
     Used for pre-stack sequence ops (convert, calibrate, resample,
     offset, bg_extract) where a per-frame preview is technically
     renderable but not informative. The thumb is dropped; the overlay
     becomes a normal in-flow header strip; a thin progress bar at the
     bottom of the head still surfaces live progress. The card body
     (params + progress %) is unchanged. */
  .node-row.preview-hidden .node-head {
    min-height: 2.6rem;
  }
  .node-row.preview-hidden .head-overlay {
    position: static;
    background: none;
    color: inherit;
    padding: 0.6rem 0.75rem;
  }
  .node-row.preview-hidden .head-name {
    text-shadow: none;
    color: var(--fg);
  }
  .node-row.preview-hidden .chevron { color: var(--fg-mute); }
  /* Bottom-anchored progress strip mirroring the thumb's, but living
     directly under the compact head so the user still sees the bar
     advance while a sequence node is running. */
  .head-progress--bottom {
    position: absolute;
    left: 0;
    bottom: 0;
    height: 2px;
    background: var(--accent);
    transition: width 200ms ease;
    z-index: 2;
    overflow: hidden;
  }
  .head-progress--bottom::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(90deg, transparent 0%, rgba(255, 255, 255, 0.35) 50%, transparent 100%);
    animation: bar-shimmer 2.2s linear infinite;
  }

  /* ---------- Body ---------- */

  .node-body {
    padding: 0.7rem 0.75rem 0.85rem;
    border-top: 1px solid var(--hairline);
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .stage-params { min-width: 0; }

  .step-status-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding-bottom: 0.35rem;
    border-bottom: 1px solid var(--hairline);
  }
  .step-status-msg {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ---------- Skeleton shimmer ---------- */

  @keyframes flow-skeleton-shimmer {
    0%   { background-position: -150% 0, 0 0; }
    100% { background-position: 250% 0, 0 0; }
  }
  .flow-skeleton {
    position: absolute;
    inset: 0;
    background:
      linear-gradient(90deg, transparent 30%, var(--accent-soft) 50%, transparent 70%),
      linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-elev-2) 100%);
    background-size: 200% 100%, 100% 100%;
    background-repeat: no-repeat;
    animation: flow-skeleton-shimmer 1.6s linear infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .flow-skeleton { animation: none; }
    .chevron { transition: none; }
    .head-progress::after,
    .head-progress--bottom::after { animation: none; }
  }

  /* ---------- Status pills ---------- */

  .status {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.6rem;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    line-height: 1;
  }
  .dur {
    display: inline-flex;
    align-items: center;
    gap: 0.3em;
  }
  /* CSS-rendered dot separator: a small circle that sits precisely
     at the optical midpoint of the surrounding text, unaffected by
     the font's baseline positioning of the Unicode middot character. */
  .dur-sep {
    display: inline-block;
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0.6;
    flex-shrink: 0;
  }
  .status-mini {
    font-size: 0.6rem;
    padding: 0.05rem 0.4rem;
  }
  .status-pending { background: rgba(255, 255, 255, 0.06); color: var(--fg-mute); }
  .status-queued { background: rgba(255, 255, 255, 0.10); color: var(--fg); }
  .status-running { background: var(--accent-soft); color: var(--accent); }
  .status-cached { background: rgba(255, 255, 255, 0.10); color: var(--fg-mute); }
  /* Lazy-skipped (no downstream consumer needed its outputs). Same muted
     palette as cached; the label "skipped" carries the distinction so the
     preview pane knows there's nothing to load. */
  .status-skipped { background: rgba(255, 255, 255, 0.10); color: var(--fg-mute); }
  .status-completed { background: color-mix(in oklab, var(--good) 18%, transparent); color: var(--good); }
  .status-failed { background: color-mix(in oklab, var(--bad) 18%, transparent); color: var(--bad); }
  .status-off {
    background: rgba(255, 255, 255, 0.04);
    color: var(--fg-mute);
    border: 1px solid var(--hairline);
  }

  /* ---------- Enable/disable toggle ---------- */

  .node-toggle {
    appearance: none;
    background: rgba(255, 255, 255, 0.10);
    border: 1px solid var(--hairline);
    width: 28px;
    height: 16px;
    border-radius: 999px;
    padding: 0;
    cursor: pointer;
    position: relative;
    flex-shrink: 0;
    transition: background-color 160ms ease, border-color 160ms ease;
    pointer-events: auto;
  }
  .node-toggle:hover { border-color: var(--border-strong); }
  .node-toggle.on { background: var(--accent); border-color: var(--accent); }
  .node-toggle-knob {
    position: absolute;
    top: 1px;
    left: 1px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--bg);
    transition: transform 160ms cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .node-toggle.on .node-toggle-knob {
    transform: translateX(12px);
    background: white;
  }
  .node-toggle:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* Dim disabled card thumb; keep header controls reachable. */
  .node-row.disabled .head-thumb {
    opacity: 0.35;
    filter: grayscale(0.6);
  }
  .node-row.disabled .head-thumb .flow-skeleton,
  .node-row.disabled .head-thumb .head-progress { display: none; }
  .node-row.disabled .head-thumb::after {
    content: 'off';
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--fg-mute);
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .node-row.disabled .badge-modified { opacity: 0.7; }

  /* Compact mod counter: just the dot-count in accent color, no border or
     background so the step name keeps its width. Tooltip carries the full
     "N params modified" text. */
  .badge-modified {
    color: var(--accent);
    font-family: var(--font-mono);
    font-size: 0.65rem;
    opacity: 0.85;
    padding: 0 0.1rem;
    flex-shrink: 0;
    cursor: default;
  }
  .no-params {
    margin: 0.2rem 0;
    color: var(--fg-mute, #888);
    font-size: 0.85em;
  }
  .muted { color: var(--fg-mute, #888); }
  .small { font-size: 0.85em; }

  /* ---------- Output actions ---------- */

  .output-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }
  .cover-btn {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-mute);
    padding: 0.25rem 0.7rem;
    border-radius: 999px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    transition: color 160ms ease, border-color 160ms ease, background 160ms ease;
    text-decoration: none;
  }
  .cover-btn:hover:not(:disabled) {
    color: var(--accent);
    border-color: var(--accent);
  }
  .cover-btn.active {
    color: var(--accent-ink);
    background: linear-gradient(135deg, var(--accent), var(--good));
    border-color: transparent;
    box-shadow: 0 0 0 1px rgba(94, 234, 212, 0.3), 0 0 14px var(--accent-soft);
  }
  .cover-btn:disabled { opacity: 0.55; cursor: progress; }

  /* ---------- Quality drawer ---------- */

  .quality-toggle {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--fg-mute);
    font-size: 0.72rem;
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border-radius: 0;
    line-height: 1;
    align-self: flex-start;
    margin-top: 0.15rem;
  }
  .quality-toggle:hover {
    color: var(--fg);
  }
  .quality-toggle-arrow {
    font-size: 0.6rem;
    transition: transform 150ms ease;
    display: inline-block;
  }
  .quality-toggle-arrow.rotated {
    transform: rotate(90deg);
  }
  .quality-drawer {
    overflow: hidden;
    max-height: 0;
    transition: max-height 150ms ease;
  }
  .quality-drawer.open {
    max-height: 40rem;
  }
  .quality-grid {
    display: grid;
    /* Three columns: section heading, label, value. Earlier 5-col layout
       overflowed the drawer's overflow:hidden box when the label column
       had to fit "Roundness", leaving the right-side values invisible. */
    grid-template-columns: 5.5rem auto 1fr;
    column-gap: 0.6rem;
    row-gap: 0.2rem;
    align-items: baseline;
    padding: 0.5rem 0 0.2rem;
    font-size: 0.75rem;
  }
  .qg-section, .qg-section-cont {
    font-size: 0.62rem;
    font-family: var(--font-mono);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-mute);
    white-space: nowrap;
    font-weight: 600;
    padding-top: 0.1rem;
  }
  .qg-label {
    color: var(--fg-mute);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .qg-val {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    font-size: 0.75rem;
    color: var(--fg);
    white-space: nowrap;
  }
  .qg-unavail {
    opacity: 0.45;
    font-style: italic;
    cursor: help;
  }
  .qg-warnings {
    font-size: 0.72rem;
    color: var(--fg-mute);
    word-break: break-word;
    white-space: normal;
    line-height: 1.35;
    padding: 0.1rem 0;
  }
</style>
