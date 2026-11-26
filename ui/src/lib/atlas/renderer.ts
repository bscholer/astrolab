// Draws the DSS colour survey (a HiPS tree served by /api/sky/hips) onto a
// WebGL canvas in the View's stereographic projection. The all-sky image at
// order 3 is always drawn first; sharper tiles are drawn over it once they
// arrive, coarse to fine, so a view is never blank while zooming.

import { children, tileCount, tilePath, tilePoint, type Vec3 } from './healpix';
import type { View } from './sky';

const BASE_ORDER = 3;
const MAX_ORDER = 9;
const TILE_PX = 512;
const ALLSKY_COLS = 27;
const GRID = 6;
const MAX_TEXTURES = 220;
const MAX_INFLIGHT = 6;
const TILE_URL = (path: string) => `/api/sky/hips/${path}`;

const TILE_VS = `
attribute vec3 a_pos;
attribute vec2 a_uv;
uniform mat3 u_rot;
uniform vec2 u_scale;
varying vec2 v_uv;
void main() {
  vec3 v = u_rot * a_pos;
  vec2 p = v.xy / max(1.0 + v.z, 1e-3);
  gl_Position = vec4(p * u_scale, 0.0, 1.0);
  v_uv = a_uv;
}`;

const TILE_FS = `
precision mediump float;
uniform sampler2D u_tex;
uniform float u_gain;
uniform float u_black;
varying vec2 v_uv;
void main() {
  // A black point hides the plate-to-plate background steps of the survey.
  vec3 c = texture2D(u_tex, v_uv).rgb;
  gl_FragColor = vec4(clamp((c - u_black) * u_gain, 0.0, 1.0), 1.0);
}`;

// Full-screen pass: darken what is below the horizon, darken (a little less)
// what the scope's panorama found blocking the sky above it, and tint the band
// below the altitude limit, by inverting the projection per pixel. The skyline
// is a 360 x 1 texture: altitude 0..90 per degree of azimuth from north.
const GROUND_VS = `
attribute vec2 a_clip;
varying vec2 v_clip;
void main() { v_clip = a_clip; gl_Position = vec4(a_clip, 0.0, 1.0); }`;

const GROUND_FS = `
precision highp float;
uniform mat3 u_inv;
uniform vec2 u_scale;
uniform vec3 u_zenith;
uniform vec3 u_north;
uniform vec3 u_east;
uniform sampler2D u_skyline;
uniform float u_has_skyline;
uniform float u_min_sin;
uniform float u_on;
varying vec2 v_clip;
void main() {
  vec2 p = v_clip / u_scale;
  float r2 = dot(p, p);
  vec3 cam = vec3(2.0 * p, 1.0 - r2) / (1.0 + r2);
  vec3 d = u_inv * cam;
  float s = dot(d, u_zenith);
  if (u_on < 0.5) discard;
  float block = 0.0;
  if (u_has_skyline > 0.5) {
    float az = atan(dot(d, u_east), dot(d, u_north));
    float u = fract(az / 6.2831853 + 1.0);
    block = sin(radians(texture2D(u_skyline, vec2(u, 0.5)).r * 90.0));
  }
  if (s < 0.0) {
    float depth = clamp(-s * 4.0, 0.0, 1.0);
    gl_FragColor = vec4(mix(vec3(0.035, 0.05, 0.045), vec3(0.01, 0.015, 0.015), depth), 0.86);
  } else if (s < block) {
    gl_FragColor = vec4(0.05, 0.07, 0.06, 0.74);
  } else if (s < u_min_sin) {
    gl_FragColor = vec4(0.98, 0.75, 0.14, 0.045);
  } else {
    discard;
  }
}`;

interface Tile {
  key: string;
  order: number;
  npix: number;
  /** 3x3 grid of sample points for culling. */
  samples: Vec3[];
}

interface Loaded {
  tex: WebGLTexture;
  vbo: WebGLBuffer;
  used: number;
}

function compile(gl: WebGLRenderingContext, vs: string, fs: string): WebGLProgram {
  const prog = gl.createProgram()!;
  for (const [type, src] of [
    [gl.VERTEX_SHADER, vs],
    [gl.FRAGMENT_SHADER, fs],
  ] as const) {
    const sh = gl.createShader(type)!;
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh) ?? 'shader');
    gl.attachShader(prog, sh);
  }
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? 'link');
  return prog;
}

/** Triangle-list mesh of a tile: position (3) and texture coordinate (2). */
function tileMesh(order: number, npix: number, uv: (fx: number, fy: number) => [number, number]): Float32Array {
  const pts: Vec3[][] = [];
  for (let i = 0; i <= GRID; i++) {
    const row: Vec3[] = [];
    for (let j = 0; j <= GRID; j++) row.push(tilePoint(order, npix, i / GRID, j / GRID));
    pts.push(row);
  }
  const out = new Float32Array(GRID * GRID * 6 * 5);
  let o = 0;
  const put = (i: number, j: number) => {
    const p = pts[i][j];
    const [u, v] = uv(i / GRID, j / GRID);
    out[o++] = p[0];
    out[o++] = p[1];
    out[o++] = p[2];
    out[o++] = u;
    out[o++] = v;
  };
  for (let i = 0; i < GRID; i++) {
    for (let j = 0; j < GRID; j++) {
      put(i, j);
      put(i + 1, j);
      put(i, j + 1);
      put(i + 1, j);
      put(i + 1, j + 1);
      put(i, j + 1);
    }
  }
  return out;
}

// HiPS JPEG tiles store the HEALPix x index down the rows and y across the
// columns (see server/sky_thumbs.render), so u follows fy and v follows fx.
const tileUV = (fx: number, fy: number): [number, number] => [fy, fx];

export class SkyRenderer {
  private gl: WebGLRenderingContext;
  private tileProg: WebGLProgram;
  private groundProg: WebGLProgram;
  private quad: WebGLBuffer;
  private allsky: WebGLTexture | null = null;
  private allskyVbo = new Map<number, WebGLBuffer>();
  private tiles = new Map<string, Tile>();
  private loaded = new Map<string, Loaded>();
  private inflight = new Set<string>();
  private failed = new Set<string>();
  private frame = 0;
  /** Called when a tile arrives, so the owner can draw again. */
  onChange: () => void = () => {};
  showGround = true;
  minAltDeg = 20;
  private skylineTex: WebGLTexture | null = null;
  gain = 1.2;
  black = 0.045;

  constructor(private canvas: HTMLCanvasElement) {
    const gl = canvas.getContext('webgl', { antialias: true, premultipliedAlpha: false });
    if (!gl) throw new Error('WebGL is not available in this browser');
    this.gl = gl;
    this.tileProg = compile(gl, TILE_VS, TILE_FS);
    this.groundProg = compile(gl, GROUND_VS, GROUND_FS);
    this.quad = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    this.loadImage(TILE_URL(`Norder${BASE_ORDER}/Allsky.jpg`)).then(
      (img) => {
        this.allsky = this.texture(img, false);
        this.onChange();
      },
      () => {},
    );
  }

  private loadImage(url: string): Promise<HTMLImageElement> {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error(url));
      img.src = url;
    });
  }

  private texture(img: HTMLImageElement, mip: boolean): WebGLTexture {
    const gl = this.gl;
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    if (mip) {
      gl.generateMipmap(gl.TEXTURE_2D);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    } else {
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    }
    return tex;
  }

  private buffer(data: Float32Array): WebGLBuffer {
    const gl = this.gl;
    const b = gl.createBuffer()!;
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return b;
  }

  private tile(order: number, npix: number): Tile {
    const key = `${order}/${npix}`;
    let t = this.tiles.get(key);
    if (!t) {
      const samples: Vec3[] = [];
      for (const fx of [0, 0.5, 1]) for (const fy of [0, 0.5, 1]) samples.push(tilePoint(order, npix, fx, fy));
      t = { key, order, npix, samples };
      this.tiles.set(key, t);
    }
    return t;
  }

  private visible(t: Tile, view: View): boolean {
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    let front = false;
    for (const s of t.samples) {
      const z = s[0] * view.fwd[0] + s[1] * view.fwd[1] + s[2] * view.fwd[2];
      // Points far behind the viewer project to nonsense; such a tile is out of view.
      if (z < -0.6) return false;
      if (z > 0) front = true;
      const d = 1 + z;
      const x = view.cx + ((s[0] * view.right[0] + s[1] * view.right[1] + s[2] * view.right[2]) / d) * view.k;
      const y = view.cy - ((s[0] * view.up[0] + s[1] * view.up[1] + s[2] * view.up[2]) / d) * view.k;
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
    if (!front) return false;
    // Tile edges bow outwards between samples; leave a margin for that.
    const mx = (maxX - minX) * 0.15;
    const my = (maxY - minY) * 0.15;
    return maxX + mx >= 0 && minX - mx <= view.width && maxY + my >= 0 && minY - my <= view.height;
  }

  /** Tile order whose pixels are about the size of a screen pixel. */
  private targetOrder(view: View, dpr: number): number {
    // A tile pixel at order o spans about 1.0233 / 2^(o + 9) radians.
    const pxPerRad = (view.pxPerDeg * dpr * 180) / Math.PI;
    for (let o = BASE_ORDER; o <= MAX_ORDER; o++) {
      if ((1.0233 / 2 ** (o + 9)) * pxPerRad <= 1.3) return o;
    }
    return MAX_ORDER;
  }

  private request(t: Tile, view: View, queue: { t: Tile; dist: number }[]) {
    if (this.loaded.has(t.key) || this.inflight.has(t.key) || this.failed.has(t.key)) return;
    const c = t.samples[4];
    queue.push({ t, dist: -(c[0] * view.fwd[0] + c[1] * view.fwd[1] + c[2] * view.fwd[2]) });
  }

  private startLoads(queue: { t: Tile; dist: number }[]) {
    queue.sort((a, b) => a.dist - b.dist);
    for (const { t } of queue) {
      if (this.inflight.size >= MAX_INFLIGHT) break;
      this.inflight.add(t.key);
      this.loadImage(TILE_URL(tilePath(t.order, t.npix))).then(
        (img) => {
          this.inflight.delete(t.key);
          this.loaded.set(t.key, {
            tex: this.texture(img, true),
            vbo: this.buffer(tileMesh(t.order, t.npix, tileUV)),
            used: this.frame,
          });
          this.evict();
          this.onChange();
        },
        () => {
          this.inflight.delete(t.key);
          this.failed.add(t.key);
          this.onChange();
        },
      );
    }
  }

  private evict() {
    if (this.loaded.size <= MAX_TEXTURES) return;
    const byAge = [...this.loaded.entries()].sort((a, b) => a[1].used - b[1].used);
    for (const [key, l] of byAge.slice(0, this.loaded.size - MAX_TEXTURES)) {
      if (l.used === this.frame) break;
      this.gl.deleteTexture(l.tex);
      this.gl.deleteBuffer(l.vbo);
      this.loaded.delete(key);
    }
  }

  private allskyBuffer(npix: number): WebGLBuffer {
    let b = this.allskyVbo.get(npix);
    if (!b) {
      // The all-sky image packs every order-3 tile at 64 px, 27 to a row.
      const cell = 64;
      const w = ALLSKY_COLS * cell;
      const rows = Math.ceil(tileCount(BASE_ORDER) / ALLSKY_COLS);
      const h = rows * cell;
      const col = npix % ALLSKY_COLS;
      const row = Math.floor(npix / ALLSKY_COLS);
      const inset = 0.5;
      const uv = (fx: number, fy: number): [number, number] => [
        (col * cell + inset + fy * (cell - 2 * inset)) / w,
        (row * cell + inset + fx * (cell - 2 * inset)) / h,
      ];
      b = this.buffer(tileMesh(BASE_ORDER, npix, uv));
      this.allskyVbo.set(npix, b);
    }
    return b;
  }

  private uniforms(view: View) {
    const gl = this.gl;
    // Column-major mat3 whose rows are right, up, fwd.
    const rot = new Float32Array([
      view.right[0], view.up[0], view.fwd[0],
      view.right[1], view.up[1], view.fwd[1],
      view.right[2], view.up[2], view.fwd[2],
    ]);
    const sx = (2 * view.k) / view.width;
    const sy = (2 * view.k) / view.height;
    return { gl, rot, sx, sy };
  }

  private drawMesh(vbo: WebGLBuffer, tex: WebGLTexture) {
    const gl = this.gl;
    const pos = gl.getAttribLocation(this.tileProg, 'a_pos');
    const uv = gl.getAttribLocation(this.tileProg, 'a_uv');
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.enableVertexAttribArray(pos);
    gl.vertexAttribPointer(pos, 3, gl.FLOAT, false, 20, 0);
    gl.enableVertexAttribArray(uv);
    gl.vertexAttribPointer(uv, 2, gl.FLOAT, false, 20, 12);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.drawArrays(gl.TRIANGLES, 0, GRID * GRID * 6);
  }

  /**
   * What blocks the sky, from the scope's panorama: altitude per degree of
   * azimuth (north 0, east 90), null where unknown. null clears it.
   */
  setSkyline(alt: (number | null)[] | null) {
    const gl = this.gl;
    if (this.skylineTex) gl.deleteTexture(this.skylineTex);
    this.skylineTex = null;
    if (!alt || !alt.length) return;
    const n = alt.length;
    const data = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      const a = Math.max(0, Math.min(90, alt[i] ?? 0));
      data[i] = Math.round((a / 90) * 255);
    }
    const tex = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, n, 1, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, data);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 4);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    this.skylineTex = tex;
    this.onChange();
  }

  /**
   * Draw the sky. `zenith` is null when the site is unknown; `north` and
   * `east` place the skyline, which is left off without them.
   */
  draw(view: View, zenith: Vec3 | null, north: Vec3 | null = null, east: Vec3 | null = null) {
    const dpr = this.canvas.width / Math.max(1, view.width);
    this.frame++;
    const { gl, rot, sx, sy } = this.uniforms(view);
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0.008, 0.012, 0.04, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);

    gl.useProgram(this.tileProg);
    gl.uniformMatrix3fv(gl.getUniformLocation(this.tileProg, 'u_rot'), false, rot);
    gl.uniform2f(gl.getUniformLocation(this.tileProg, 'u_scale'), sx, sy);
    gl.uniform1f(gl.getUniformLocation(this.tileProg, 'u_gain'), this.gain);
    gl.uniform1f(gl.getUniformLocation(this.tileProg, 'u_black'), this.black);
    gl.activeTexture(gl.TEXTURE0);

    const target = this.targetOrder(view, dpr);
    // The all-sky image holds each order-3 tile at 64 px, 1/8 of a full tile.
    const pxPerRad = (view.pxPerDeg * dpr * 180) / Math.PI;
    const allskyEnough = target === BASE_ORDER && (1.0233 / 2 ** (BASE_ORDER + 6)) * pxPerRad <= 2.5;
    const queue: { t: Tile; dist: number }[] = [];
    let level: Tile[] = [];
    for (let n = 0; n < tileCount(BASE_ORDER); n++) {
      const t = this.tile(BASE_ORDER, n);
      if (this.visible(t, view)) level.push(t);
    }
    if (this.allsky) for (const t of level) this.drawMesh(this.allskyBuffer(t.npix), this.allsky);

    for (let order = BASE_ORDER; order <= target; order++) {
      if (order > BASE_ORDER) {
        const next: Tile[] = [];
        for (const parent of level) {
          for (const c of children(parent.npix)) {
            const t = this.tile(order, c);
            if (this.visible(t, view)) next.push(t);
          }
        }
        level = next;
      }
      if (order === BASE_ORDER && allskyEnough) continue;
      for (const t of level) {
        const l = this.loaded.get(t.key);
        if (l) {
          l.used = this.frame;
          this.drawMesh(l.vbo, l.tex);
        } else if (order === target) {
          this.request(t, view, queue);
        }
      }
    }
    this.startLoads(queue);

    if (zenith && this.showGround) {
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.useProgram(this.groundProg);
      // The inverse of a rotation is its transpose.
      const inv = new Float32Array([
        rot[0], rot[3], rot[6],
        rot[1], rot[4], rot[7],
        rot[2], rot[5], rot[8],
      ]);
      gl.uniformMatrix3fv(gl.getUniformLocation(this.groundProg, 'u_inv'), false, inv);
      gl.uniform2f(gl.getUniformLocation(this.groundProg, 'u_scale'), sx, sy);
      gl.uniform3f(gl.getUniformLocation(this.groundProg, 'u_zenith'), zenith[0], zenith[1], zenith[2]);
      gl.uniform1f(gl.getUniformLocation(this.groundProg, 'u_min_sin'), Math.sin((this.minAltDeg * Math.PI) / 180));
      gl.uniform1f(gl.getUniformLocation(this.groundProg, 'u_on'), 1);
      const skyline = this.skylineTex && north && east;
      gl.uniform1f(gl.getUniformLocation(this.groundProg, 'u_has_skyline'), skyline ? 1 : 0);
      if (skyline) {
        gl.uniform3f(gl.getUniformLocation(this.groundProg, 'u_north'), north[0], north[1], north[2]);
        gl.uniform3f(gl.getUniformLocation(this.groundProg, 'u_east'), east[0], east[1], east[2]);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.skylineTex);
        gl.uniform1i(gl.getUniformLocation(this.groundProg, 'u_skyline'), 0);
      }
      const loc = gl.getAttribLocation(this.groundProg, 'a_clip');
      gl.bindBuffer(gl.ARRAY_BUFFER, this.quad);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
      gl.disable(gl.BLEND);
    }
  }

  /** True while tiles for the current view are still arriving. */
  get busy(): boolean {
    return this.inflight.size > 0;
  }

  destroy() {
    const gl = this.gl;
    for (const l of this.loaded.values()) {
      gl.deleteTexture(l.tex);
      gl.deleteBuffer(l.vbo);
    }
    for (const b of this.allskyVbo.values()) gl.deleteBuffer(b);
    if (this.allsky) gl.deleteTexture(this.allsky);
    this.loaded.clear();
  }
}
