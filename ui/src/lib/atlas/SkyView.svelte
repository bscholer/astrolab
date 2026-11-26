<!--
  The atlas map: survey imagery drawn by SkyRenderer (WebGL) with a 2D
  overlay for the grid, horizon, constellations, catalog objects and the
  scope's field of view. Drag to pan, wheel or pinch to zoom.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import type { TonightEntry } from '$lib/api';
  import { displayName, typeGroup } from '$lib/tonight';
  import { SkyRenderer } from './renderer';
  import {
    altAzVec,
    cross,
    dot,
    horizon,
    normalize,
    offset,
    radec,
    toAltAz,
    toRaDec,
    View,
    type Horizon,
    type Vec3,
  } from './sky';
  import {
    importance,
    inBox,
    planMosaic,
    TELE_FOV_DEG,
    type Constellations,
    type Grid,
    type Layers,
    type SkyBox,
  } from './atlas';

  type Frame = 'horizon' | 'equatorial';

  let {
    entries,
    selected = null,
    site = null,
    timeMs,
    frame = 'horizon',
    layers,
    minAlt = 20,
    autoNames = new Set<string>(),
    constellations = null,
    covered = [],
    planned = [],
    mosaicGrid = null,
    skyline = null,
    onselect,
    onviewchange,
  }: {
    entries: TonightEntry[];
    selected?: string | null;
    site?: { lat: number; lon: number } | null;
    timeMs: number;
    frame?: Frame;
    layers: Layers;
    minAlt?: number;
    autoNames?: Set<string>;
    constellations?: Constellations | null;
    /** What blocks the sky from the scope: altitude per degree of azimuth, null unknown. */
    skyline?: (number | null)[] | null;
    /** Where captured sessions landed; hours set how strongly each is filled. */
    covered?: (SkyBox & { hours: number })[];
    /** Mosaic panels waiting on the auto list. */
    planned?: (SkyBox & { label: string })[];
    /** A mosaic being laid out: drawn around the centre of the view. */
    mosaicGrid?: Grid | null;
    onselect: (name: string | null) => void;
    onviewchange?: (v: { altDeg: number | null; azDeg: number | null; ra: number; dec: number; fov: number }) => void;
  } = $props();

  const MIN_FOV = 0.25;
  const MAX_FOV = 140;
  const COLORS: Record<string, string> = {
    nebulae: '#f472b6',
    galaxies: '#fbbf24',
    clusters: '#5eead4',
    other: '#a5b4cf',
  };

  let host: HTMLDivElement;
  let glCanvas: HTMLCanvasElement;
  let overlay: HTMLCanvasElement;
  let renderer: SkyRenderer | null = null;
  let glError = $state<string | null>(null);
  let width = $state(0);
  let height = $state(0);
  let hover = $state<{ x: number; y: number; e: TonightEntry } | null>(null);

  // Centre of the view in the current frame: azimuth/altitude, or RA/Dec.
  let lon = 180;
  let lat = 40;
  let fov = 90;
  let activeFrame: Frame = 'horizon';
  let dirty = true;
  let raf = 0;
  let hits: { x: number; y: number; r: number; e: TonightEntry }[] = [];
  let anim: { from: Vec3; to: Vec3; fov0: number; fov1: number; t0: number; ms: number } | null = null;

  const frameUsed = $derived<Frame>(site ? frame : 'equatorial');

  function horizonNow(): Horizon | null {
    return site ? horizon(timeMs, site.lat, site.lon) : null;
  }

  function centerVec(h: Horizon | null): Vec3 {
    return activeFrame === 'horizon' && h ? altAzVec(h, lat, lon) : radec(lon, lat);
  }

  function setCenter(v: Vec3, h: Horizon | null) {
    if (activeFrame === 'horizon' && h) {
      const p = toAltAz(h, v);
      lon = p.az;
      lat = Math.max(-89.5, Math.min(89.5, p.alt));
    } else {
      const p = toRaDec(v);
      lon = p.ra;
      lat = Math.max(-89.5, Math.min(89.5, p.dec));
    }
  }

  function makeView(h: Horizon | null, fovDeg = fov): View {
    const up: Vec3 = activeFrame === 'horizon' && h ? h.zenith : [0, 0, 1];
    return new View(centerVec(h), up, fovDeg, width, height);
  }

  /** Rotate v by the rotation that takes a to b. */
  function rotateBy(v: Vec3, a: Vec3, b: Vec3): Vec3 {
    const axis = cross(a, b);
    const s = Math.hypot(axis[0], axis[1], axis[2]);
    const c = dot(a, b);
    if (s < 1e-12) return v;
    const k = [axis[0] / s, axis[1] / s, axis[2] / s] as Vec3;
    const kv = cross(k, v);
    const kd = dot(k, v);
    return normalize([
      v[0] * c + kv[0] * s + k[0] * kd * (1 - c),
      v[1] * c + kv[1] * s + k[1] * kd * (1 - c),
      v[2] * c + kv[2] * s + k[2] * kd * (1 - c),
    ]);
  }

  /** Zoom to `newFov` keeping the sky under (x, y) where it is. */
  function zoomAt(newFov: number, x: number, y: number) {
    newFov = Math.max(MIN_FOV, Math.min(MAX_FOV, newFov));
    const h = horizonNow();
    const before = makeView(h).unproject(x, y);
    fov = newFov;
    const after = makeView(h).unproject(x, y);
    setCenter(rotateBy(centerVec(h), after, before), h);
    anim = null;
    invalidate();
  }

  function pan(dx: number, dy: number) {
    const h = horizonNow();
    const view = makeView(h);
    const before = view.unproject(width / 2 - dx, height / 2 - dy);
    const c = centerVec(h);
    setCenter(rotateBy(c, c, before), h);
    anim = null;
    invalidate();
  }

  /** Where the centre must be for a position to show `liftPx` above the middle. */
  function centerFor(raDeg: number, decDeg: number, fovDeg: number, liftPx: number): Vec3 {
    const target = radec(raDeg, decDeg);
    if (!liftPx) return target;
    const h = horizonNow();
    const up: Vec3 = activeFrame === 'horizon' && h ? h.zenith : [0, 0, 1];
    return new View(target, up, fovDeg, width, height).unproject(width / 2, height / 2 + liftPx);
  }

  /** Point the view at a position, animating there. `liftPx` raises it
   *  above the middle, clear of a panel covering the bottom. */
  export function flyTo(raDeg: number, decDeg: number, fovDeg?: number, liftPx = 0) {
    const h = horizonNow();
    const fov1 = Math.max(MIN_FOV, Math.min(MAX_FOV, fovDeg ?? fov));
    anim = {
      from: centerVec(h),
      to: centerFor(raDeg, decDeg, fov1, liftPx),
      fov0: fov,
      fov1,
      t0: performance.now(),
      ms: 800,
    };
    invalidate();
  }

  /** Look at a point on the horizon frame directly, without animation. */
  export function lookAt(altDeg: number, azDeg: number, fovDeg?: number) {
    const h = horizonNow();
    if (!h) return;
    setCenter(altAzVec(h, altDeg, azDeg), h);
    if (fovDeg) fov = fovDeg;
    anim = null;
    invalidate();
  }

  /** Look at a position on the sky directly, without animation. */
  export function lookAtSky(raDeg: number, decDeg: number, fovDeg?: number, liftPx = 0) {
    if (fovDeg) fov = Math.max(MIN_FOV, Math.min(MAX_FOV, fovDeg));
    setCenter(centerFor(raDeg, decDeg, fov, liftPx), horizonNow());
    anim = null;
    invalidate();
  }

  /** RA and Dec at the centre of the view. */
  export function center(): { ra: number; dec: number } {
    return toRaDec(centerVec(horizonNow()));
  }

  export function zoomBy(factor: number) {
    zoomAt(fov * factor, width / 2, height / 2);
  }

  function slerp(a: Vec3, b: Vec3, t: number): Vec3 {
    const c = Math.max(-1, Math.min(1, dot(a, b)));
    const th = Math.acos(c);
    if (th < 1e-9) return b;
    const s = Math.sin(th);
    const wa = Math.sin((1 - t) * th) / s;
    const wb = Math.sin(t * th) / s;
    return normalize([a[0] * wa + b[0] * wb, a[1] * wa + b[1] * wb, a[2] * wa + b[2] * wb]);
  }

  function invalidate() {
    dirty = true;
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function tick(now: number) {
    raf = 0;
    if (anim) {
      const t = Math.min(1, (now - anim.t0) / anim.ms);
      const e = t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
      const h = horizonNow();
      setCenter(slerp(anim.from, anim.to, e), h);
      fov = Math.exp(Math.log(anim.fov0) + (Math.log(anim.fov1) - Math.log(anim.fov0)) * e);
      if (t >= 1) anim = null;
      dirty = true;
    }
    if (dirty) {
      dirty = false;
      draw();
    }
    if (anim) raf = requestAnimationFrame(tick);
  }

  // Redraw whenever an input changes.
  $effect(() => {
    void entries;
    void selected;
    void timeMs;
    void layers.grid;
    void layers.constellations;
    void layers.labels;
    void layers.frame;
    void layers.ground;
    void minAlt;
    void autoNames;
    void constellations;
    void covered;
    void planned;
    void mosaicGrid;
    void layers.captured;
    invalidate();
  });

  // The renderer may not exist yet; onMount hands it the skyline then.
  $effect(() => {
    const line = skyline;
    renderer?.setSkyline(line);
  });

  // Keep looking at the same part of the sky when the frame changes.
  $effect(() => {
    const next = frameUsed;
    if (next === activeFrame) return;
    const h = horizonNow();
    const c = centerVec(h);
    activeFrame = next;
    setCenter(c, h);
    invalidate();
  });

  function gridStep(f: number): number {
    if (f > 80) return 30;
    if (f > 40) return 15;
    if (f > 15) return 10;
    if (f > 6) return 5;
    if (f > 2.5) return 2;
    if (f > 1) return 1;
    return 0.5;
  }

  /** Stroke a curve through sky points, breaking where it leaves the view. */
  function strokeCurve(ctx: CanvasRenderingContext2D, view: View, pts: Vec3[]) {
    let pen = false;
    let last: { x: number; y: number } | null = null;
    ctx.beginPath();
    for (const v of pts) {
      const p = view.project(v);
      if (!p || p.z < -0.1) {
        pen = false;
        last = null;
        continue;
      }
      // A long jump means the curve wrapped behind the viewer.
      if (pen && last && Math.hypot(p.x - last.x, p.y - last.y) > Math.max(width, height)) pen = false;
      if (pen) ctx.lineTo(p.x, p.y);
      else ctx.moveTo(p.x, p.y);
      pen = true;
      last = p;
    }
    ctx.stroke();
  }

  function drawGrid(ctx: CanvasRenderingContext2D, view: View, h: Horizon | null) {
    const step = gridStep(fov);
    const sample = Math.max(0.25, Math.min(2, fov / 60));
    const toVec = (a: number, b: number): Vec3 =>
      activeFrame === 'horizon' && h ? altAzVec(h, b, a) : radec(a, b);
    ctx.strokeStyle = 'rgba(148, 197, 255, 0.16)';
    ctx.lineWidth = 1;
    for (let a = 0; a < 360; a += step) {
      const pts: Vec3[] = [];
      for (let b = -90 + step; b <= 90 - step + 1e-9; b += sample) pts.push(toVec(a, b));
      strokeCurve(ctx, view, pts);
    }
    for (let b = -90 + step; b < 90 - 1e-9; b += step) {
      const pts: Vec3[] = [];
      for (let a = 0; a <= 360 + 1e-9; a += sample) pts.push(toVec(a, b));
      strokeCurve(ctx, view, pts);
    }
  }

  function drawHorizon(ctx: CanvasRenderingContext2D, view: View, h: Horizon) {
    const ring = (alt: number) => {
      const pts: Vec3[] = [];
      for (let az = 0; az <= 360; az += 1) pts.push(altAzVec(h, alt, az));
      return pts;
    };
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(94, 234, 212, 0.65)';
    strokeCurve(ctx, view, ring(0));
    ctx.setLineDash([4, 5]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(251, 191, 36, 0.45)';
    strokeCurve(ctx, view, ring(minAlt));
    ctx.setLineDash([]);
    if (skyline && layers.ground) drawSkyline(ctx, view, h, skyline);

    ctx.font = '600 13px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const points: [string, number][] = [
      ['N', 0], ['NE', 45], ['E', 90], ['SE', 135], ['S', 180], ['SW', 225], ['W', 270], ['NW', 315],
    ];
    for (const [label, az] of points) {
      const p = view.project(altAzVec(h, 1.5, az));
      if (!p || p.z < 0 || p.x < -20 || p.x > width + 20 || p.y < -20 || p.y > height + 20) continue;
      ctx.fillStyle = label.length === 1 ? 'rgba(94, 234, 212, 0.95)' : 'rgba(94, 234, 212, 0.6)';
      ctx.fillText(label, p.x, Math.min(p.y - 8, height - 12));
    }
  }

  /** The skyline from the scope's panorama, broken where the scan did not reach. */
  function drawSkyline(ctx: CanvasRenderingContext2D, view: View, h: Horizon, alt: (number | null)[]) {
    const step = 360 / alt.length;
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = 'rgba(253, 224, 71, 0.75)';
    let run: Vec3[] = [];
    const flush = () => {
      if (run.length > 1) strokeCurve(ctx, view, run);
      run = [];
    };
    for (let i = 0; i <= alt.length; i++) {
      const a = alt[i % alt.length];
      if (a == null) {
        flush();
        continue;
      }
      run.push(altAzVec(h, Math.max(0, a), i * step));
    }
    flush();
  }

  function drawConstellations(ctx: CanvasRenderingContext2D, view: View, c: Constellations) {
    ctx.strokeStyle = 'rgba(125, 211, 252, 0.28)';
    ctx.lineWidth = 1;
    for (const segs of Object.values(c.lines)) {
      for (const seg of segs) {
        const pts: Vec3[] = [];
        for (let i = 0; i < seg.length; i++) {
          const a = radec(seg[i][0], seg[i][1]);
          if (i > 0) {
            // Subdivide long segments so they curve with the projection.
            const b = radec(seg[i - 1][0], seg[i - 1][1]);
            for (let t = 0.25; t < 1; t += 0.25) pts.push(slerp(b, a, t));
          }
          pts.push(a);
        }
        strokeCurve(ctx, view, pts);
      }
    }
    if (fov < 12) return;
    ctx.font = '500 11px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = 'rgba(125, 211, 252, 0.5)';
    for (const n of Object.values(c.names)) {
      const p = view.project(radec(n.ra, n.dec));
      if (!p || p.z < 0.2) continue;
      ctx.fillText(n.name.toUpperCase(), p.x, p.y);
    }
  }

  /** Outline points of an object's ellipse on the sky. */
  function ellipse(e: TonightEntry, center: Vec3): Vec3[] {
    const a = (e.size_arcmin ?? 0) / 120;
    const b = (e.minor_arcmin ?? e.size_arcmin ?? 0) / 120;
    const pa = e.pa_deg ?? 0;
    const pts: Vec3[] = [];
    for (let i = 0; i <= 32; i++) {
      const t = (i / 32) * 2 * Math.PI;
      // Major axis along position angle `pa`; radius of the ellipse at angle t.
      const x = a * Math.cos(t);
      const y = b * Math.sin(t);
      pts.push(offset(center, pa + (Math.atan2(y, x) * 180) / Math.PI, Math.hypot(x, y)));
    }
    return pts;
  }

  /** Outline of a north-up box on the sky, as points along its edges. */
  function boxPath(center: Vec3, w: number, hgt: number): Vec3[] {
    const corner = (sx: number, sy: number): Vec3 => {
      const east = (sx * w) / 2;
      const north = (sy * hgt) / 2;
      return offset(center, (Math.atan2(east, north) * 180) / Math.PI, Math.hypot(east, north));
    };
    const pts: Vec3[] = [];
    const path: [number, number][] = [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]];
    for (let i = 1; i < path.length; i++) {
      const [x0, y0] = path[i - 1];
      const [x1, y1] = path[i];
      for (let t = 0; t < 1; t += 0.125) pts.push(corner(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t));
    }
    pts.push(pts[0]);
    return pts;
  }

  function fillBox(ctx: CanvasRenderingContext2D, view: View, pts: Vec3[]) {
    ctx.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const p = view.project(pts[i]);
      if (!p || p.z < 0) return;
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    }
    ctx.fill();
  }

  function onScreen(view: View, v: Vec3, marginDeg: number): boolean {
    const p = view.project(v);
    const m = marginDeg * view.pxPerDeg;
    return !!p && p.z > 0 && p.x > -m && p.x < width + m && p.y > -m && p.y < height + m;
  }

  function hoursAt(ra: number, dec: number): number {
    let h = 0;
    for (const b of covered) if (inBox(b, ra, dec)) h += b.hours;
    return h;
  }

  function drawCovered(ctx: CanvasRenderingContext2D, view: View) {
    // Quieter while a mosaic is laid out, so the grid reads first.
    const k = mosaicGrid ? 0.55 : 1;
    for (const b of covered) {
      const c = radec(b.ra, b.dec);
      if (!onScreen(view, c, Math.max(b.w, b.h))) continue;
      const pts = boxPath(c, b.w, b.h);
      ctx.fillStyle = `rgba(167, 139, 250, ${k * Math.min(0.06 + b.hours * 0.04, 0.24)})`;
      fillBox(ctx, view, pts);
      ctx.strokeStyle = `rgba(167, 139, 250, ${k * 0.45})`;
      ctx.lineWidth = 1;
      strokeCurve(ctx, view, pts);
    }
  }

  /** A panel number in a dark round badge, with the hours already shot there. */
  function badge(ctx: CanvasRenderingContext2D, view: View, center: Vec3, text: string, hours: number, color: string) {
    const p = view.project(center);
    if (!p) return;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 13, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(2, 3, 10, 0.85)';
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = color;
    ctx.stroke();
    ctx.font = '700 12px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = color;
    ctx.fillText(text, p.x, p.y + 0.5);
    if (hours >= 0.05) {
      const label = hours >= 10 ? `${Math.round(hours)} h shot` : `${hours.toFixed(1)} h shot`;
      ctx.font = '600 11px Inter, system-ui, sans-serif';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(2, 3, 10, 0.85)';
      ctx.strokeText(label, p.x, p.y + 24);
      ctx.fillStyle = 'rgba(196, 181, 253, 0.95)';
      ctx.fillText(label, p.x, p.y + 24);
    }
  }

  /** Stroke a box with a dark halo under the line, so it reads over bright sky. */
  function strokeBox(ctx: CanvasRenderingContext2D, view: View, pts: Vec3[], color: string, w: number) {
    ctx.lineWidth = w + 2.5;
    ctx.strokeStyle = 'rgba(2, 3, 10, 0.7)';
    strokeCurve(ctx, view, pts);
    ctx.lineWidth = w;
    ctx.strokeStyle = color;
    strokeCurve(ctx, view, pts);
  }

  function drawPlanned(ctx: CanvasRenderingContext2D, view: View) {
    for (const b of planned) {
      const c = radec(b.ra, b.dec);
      if (!onScreen(view, c, Math.max(b.w, b.h))) continue;
      ctx.setLineDash([6, 4]);
      strokeBox(ctx, view, boxPath(c, b.w, b.h), 'rgba(251, 191, 36, 0.85)', 1.25);
      ctx.setLineDash([]);
      if (view.pxPerDeg * b.h > 50) badge(ctx, view, c, b.label, hoursAt(b.ra, b.dec), 'rgba(251, 191, 36, 0.95)');
    }
  }

  let dimCanvas: HTMLCanvasElement | null = null;

  /** Darken everything outside the panels. */
  function dimOutside(ctx: CanvasRenderingContext2D, view: View, boxes: Vec3[][]) {
    dimCanvas ??= document.createElement('canvas');
    dimCanvas.width = overlay.width;
    dimCanvas.height = overlay.height;
    const d = dimCanvas.getContext('2d');
    if (!d) return;
    const dpr = overlay.width / width;
    d.setTransform(dpr, 0, 0, dpr, 0, 0);
    d.fillStyle = 'rgba(2, 3, 10, 0.5)';
    d.fillRect(0, 0, width, height);
    d.globalCompositeOperation = 'destination-out';
    d.fillStyle = '#000';
    for (const pts of boxes) fillBox(d, view, pts);
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(dimCanvas, 0, 0);
    ctx.restore();
  }

  function drawMosaic(ctx: CanvasRenderingContext2D, view: View, center: Vec3, grid: Grid) {
    const rd = toRaDec(center);
    const panels = planMosaic(rd.ra, rd.dec, grid);
    const paths = panels.map((b) => boxPath(radec(b.ra, b.dec), b.w, b.h));
    dimOutside(ctx, view, paths);
    // A faint fill, so where panels overlap shows a little brighter.
    ctx.fillStyle = 'rgba(94, 234, 212, 0.05)';
    for (const pts of paths) fillBox(ctx, view, pts);
    for (const pts of paths) strokeBox(ctx, view, pts, 'rgba(94, 234, 212, 0.95)', 1.75);
    if (view.pxPerDeg * TELE_FOV_DEG[1] > 36) {
      panels.forEach((b, i) => {
        badge(ctx, view, radec(b.ra, b.dec), String(i + 1), hoursAt(b.ra, b.dec), 'rgba(94, 234, 212, 0.95)');
      });
    }
  }

  function drawFrameBox(ctx: CanvasRenderingContext2D, view: View, center: Vec3) {
    const [w, hgt] = TELE_FOV_DEG;
    ctx.strokeStyle = 'rgba(94, 234, 212, 0.95)';
    ctx.lineWidth = 1.5;
    strokeCurve(ctx, view, boxPath(center, w, hgt));
    // Top-left corner: east of north, so a negative east offset.
    const label = view.project(
      offset(center, (Math.atan2(-w / 2, hgt / 2) * 180) / Math.PI, Math.hypot(w / 2, hgt / 2)),
    );
    if (label && view.pxPerDeg * hgt > 40) {
      ctx.font = '500 11px Inter, system-ui, sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillStyle = 'rgba(94, 234, 212, 0.9)';
      ctx.fillText('DWARF 3 tele', label.x + 2, label.y - 3);
    }
  }

  function importanceFloor(f: number): number {
    if (f > 90) return 0.32;
    if (f > 45) return 0.22;
    if (f > 20) return 0.12;
    if (f > 8) return 0.04;
    return 0;
  }

  function drawObjects(ctx: CanvasRenderingContext2D, view: View, h: Horizon | null) {
    const floor = importanceFloor(fov);
    const shown: { x: number; y: number; r: number; e: TonightEntry; imp: number; color: string; up: boolean }[] = [];
    for (const e of entries) {
      const imp = importance(e);
      if (imp < floor && e.name !== selected && !autoNames.has(e.name)) continue;
      const v = radec(e.ra_deg, e.dec_deg);
      const p = view.project(v);
      if (!p || p.z < 0 || p.x < -60 || p.x > width + 60 || p.y < -60 || p.y > height + 60) continue;
      const up = h ? dot(v, h.zenith) > 0 : true;
      const color = COLORS[typeGroup(e.object_type) ?? 'other'];
      const rPx = ((e.size_arcmin ?? 0) / 120) * view.pxPerDeg;
      ctx.globalAlpha = up ? 1 : 0.45;
      ctx.strokeStyle = color;
      ctx.lineWidth = e.name === selected ? 2 : 1.25;
      if (rPx > 7) {
        strokeCurve(ctx, view, ellipse(e, v));
      } else {
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (autoNames.has(e.name)) {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 2, 0, Math.PI * 2);
        ctx.fill();
      }
      shown.push({ x: p.x, y: p.y, r: Math.max(rPx, 4), e, imp, color, up });
    }
    ctx.globalAlpha = 1;
    hits = shown;

    if (!layers.labels) return;
    // Most important first; skip labels that would overlap one already placed.
    shown.sort((a, b) => (b.e.name === selected ? 1 : 0) - (a.e.name === selected ? 1 : 0) || b.imp - a.imp);
    const placed: [number, number, number, number][] = [];
    const maxLabels = width < 600 ? 28 : 70;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (const s of shown) {
      if (placed.length >= maxLabels) break;
      const main = displayName(s.e);
      const extra = s.e.common_name && (fov < 40 || s.imp > 0.5) ? s.e.common_name : null;
      const text = extra && extra !== main ? `${main} · ${extra}` : main;
      ctx.font = `${s.e.name === selected ? 600 : 500} 11.5px Inter, system-ui, sans-serif`;
      const w = ctx.measureText(text).width;
      const x = s.x + Math.min(s.r, 40) * 0.72 + 5;
      const y = s.y - Math.min(s.r, 40) * 0.72;
      const box: [number, number, number, number] = [x - 2, y - 8, x + w + 2, y + 8];
      if (placed.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])) continue;
      placed.push(box);
      ctx.globalAlpha = s.up ? 1 : 0.55;
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(2, 3, 10, 0.85)';
      ctx.strokeText(text, x, y);
      ctx.fillStyle = s.e.name === selected ? '#ffffff' : s.color;
      ctx.fillText(text, x, y);
    }
    ctx.globalAlpha = 1;
  }

  function draw() {
    if (!width || !height) return;
    const h = horizonNow();
    const view = makeView(h);
    if (renderer) {
      renderer.showGround = layers.ground;
      renderer.minAltDeg = minAlt;
      renderer.draw(view, h?.zenith ?? null, h?.north ?? null, h?.east ?? null);
    }
    const ctx = overlay.getContext('2d');
    if (!ctx) return;
    const dpr = overlay.width / width;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (layers.grid) drawGrid(ctx, view, h);
    if (layers.constellations && constellations) drawConstellations(ctx, view, constellations);
    if (layers.captured) drawCovered(ctx, view);
    if (h) drawHorizon(ctx, view, h);
    drawPlanned(ctx, view);
    drawObjects(ctx, view, h);
    const c = centerVec(h);
    const sel = selected ? entries.find((e) => e.name === selected) : null;
    if (mosaicGrid) drawMosaic(ctx, view, c, mosaicGrid);
    else if (layers.frame && sel) drawFrameBox(ctx, view, radec(sel.ra_deg, sel.dec_deg));

    const rd = toRaDec(c);
    const aa = h ? toAltAz(h, c) : null;
    onviewchange?.({ altDeg: aa?.alt ?? null, azDeg: aa?.az ?? null, ra: rd.ra, dec: rd.dec, fov });
  }

  function resize() {
    const r = host.getBoundingClientRect();
    width = Math.round(r.width);
    height = Math.round(r.height);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    for (const c of [glCanvas, overlay]) {
      c.width = Math.round(width * dpr);
      c.height = Math.round(height * dpr);
    }
    invalidate();
  }

  // ----- input ---------------------------------------------------------

  const pointers = new Map<number, { x: number; y: number }>();
  let downAt: { x: number; y: number; t: number } | null = null;
  let moved = 0;
  let pinch: { dist: number; fov: number } | null = null;

  function local(ev: PointerEvent | WheelEvent | MouseEvent) {
    const r = host.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  }

  function onPointerDown(ev: PointerEvent) {
    host.setPointerCapture(ev.pointerId);
    const p = local(ev);
    pointers.set(ev.pointerId, p);
    if (pointers.size === 1) {
      downAt = { ...p, t: performance.now() };
      moved = 0;
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), fov };
      downAt = null;
    }
    hover = null;
  }

  function onPointerMove(ev: PointerEvent) {
    const p = local(ev);
    const prev = pointers.get(ev.pointerId);
    if (!prev) {
      if (ev.pointerType === 'mouse') updateHover(p);
      return;
    }
    pointers.set(ev.pointerId, p);
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d > 0) zoomAt((pinch.fov * pinch.dist) / d, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (pointers.size === 1) {
      moved += Math.hypot(p.x - prev.x, p.y - prev.y);
      pan(p.x - prev.x, p.y - prev.y);
    }
  }

  function onPointerUp(ev: PointerEvent) {
    const p = local(ev);
    pointers.delete(ev.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 1) {
      // Carry on panning with the finger that stayed down.
      const [rest] = [...pointers.values()];
      downAt = { ...rest, t: performance.now() };
      moved = 99;
      return;
    }
    if (downAt && moved < 8 && performance.now() - downAt.t < 600) tap(p);
    downAt = null;
  }

  function nearest(p: { x: number; y: number }, slop: number): TonightEntry | null {
    let best: TonightEntry | null = null;
    let bestD = Infinity;
    for (const h of hits) {
      const d = Math.hypot(h.x - p.x, h.y - p.y);
      // Inside a large outline counts, but a closer small marker wins.
      const score = d <= h.r ? d / Math.max(h.r, 1) * slop : d;
      if ((d <= slop || d <= h.r) && score < bestD) {
        best = h.e;
        bestD = score;
      }
    }
    return best;
  }

  function tap(p: { x: number; y: number }) {
    const hit = nearest(p, 22);
    if (hit) onselect(hit.name);
  }

  function updateHover(p: { x: number; y: number }) {
    const hit = nearest(p, 14);
    hover = hit ? { ...p, e: hit } : null;
  }

  function onWheel(ev: WheelEvent) {
    ev.preventDefault();
    const p = local(ev);
    const delta = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
    zoomAt(fov * Math.exp(delta * 0.0015), p.x, p.y);
  }

  function onDblClick(ev: MouseEvent) {
    const p = local(ev);
    zoomAt(fov / 2, p.x, p.y);
  }

  function onKey(ev: KeyboardEvent) {
    const step = Math.max(width, height) * 0.08;
    const keys: Record<string, () => void> = {
      ArrowLeft: () => pan(step, 0),
      ArrowRight: () => pan(-step, 0),
      ArrowUp: () => pan(0, step),
      ArrowDown: () => pan(0, -step),
      '+': () => zoomBy(1 / 1.4),
      '=': () => zoomBy(1 / 1.4),
      '-': () => zoomBy(1.4),
    };
    const fn = keys[ev.key];
    if (fn) {
      ev.preventDefault();
      fn();
    }
  }

  onMount(() => {
    activeFrame = frameUsed;
    try {
      renderer = new SkyRenderer(glCanvas);
      renderer.onChange = invalidate;
      renderer.setSkyline(skyline);
    } catch (e) {
      glError = (e as Error).message;
    }
    const ro = new ResizeObserver(resize);
    ro.observe(host);
    resize();
    host.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      ro.disconnect();
      host.removeEventListener('wheel', onWheel);
      cancelAnimationFrame(raf);
      renderer?.destroy();
    };
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
<div
  class="sky"
  bind:this={host}
  tabindex="0"
  role="application"
  aria-label="Sky atlas. Drag to pan, scroll or pinch to zoom, arrow keys to move."
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={onPointerUp}
  onpointercancel={onPointerUp}
  onpointerleave={() => (hover = null)}
  ondblclick={onDblClick}
  onkeydown={onKey}
>
  <canvas bind:this={glCanvas} class="layer"></canvas>
  <canvas bind:this={overlay} class="layer"></canvas>
  {#if glError}
    <p class="gl-error">Can't draw the sky: {glError}</p>
  {/if}
  {#if hover}
    <div class="tip" style:left="{hover.x + 14}px" style:top="{hover.y + 12}px">
      <strong>{displayName(hover.e)}</strong>
      {#if hover.e.common_name}<span>{hover.e.common_name}</span>{/if}
      <span class="muted">{hover.e.object_type ?? ''}</span>
    </div>
  {/if}
</div>

<style>
  .sky {
    position: relative;
    width: 100%;
    height: 100%;
    overflow: hidden;
    touch-action: none;
    cursor: grab;
    background: #02030a;
    outline: none;
    user-select: none;
    -webkit-user-select: none;
  }
  .sky:active {
    cursor: grabbing;
  }
  .layer {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    display: block;
  }
  .gl-error {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    color: var(--fg-mute);
  }
  .tip {
    position: absolute;
    pointer-events: none;
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 0.35rem 0.55rem;
    font-size: 0.8rem;
    background: rgba(10, 15, 28, 0.92);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    white-space: nowrap;
  }
</style>
