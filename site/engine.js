/* Ensemble MSLP viewer - compute engine (no DOM; runs in the browser and under Node for the tests).
 *
 * The pipeline ships only per-system grids (data/<run>/<system>/<mean|min>/fNNN.bin.gz, see gefs_pipeline/gridio.py).
 * Everything else happens here, in the visitor's browser:
 *   - inflating + decoding the grids                    (inflate, decodeBin)
 *   - blends of any systems: weighted mean, lowest-of-all-members with owner, regridding to the first system's grid
 *   - the swath: running minimum of the member-wise minimum over the forecast hours, with the hour it was reached
 *   - low-pressure centres of any derived minimum field (a port of gefs_pipeline.products.detect_lows)
 *   - smooth upsampling + colour-class rasterisation of a field onto the map canvas
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MslpEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MISSING = -32768;

  // ---------------------------------------------------------------------------------------------
  // files
  // ---------------------------------------------------------------------------------------------
  /** Inflate a gzip buffer. A buffer that is not gzip (a server that already decoded it) is returned unchanged. */
  async function inflate(buf) {
    const u8 = new Uint8Array(buf);
    if (!(u8.length > 2 && u8[0] === 0x1f && u8[1] === 0x8b)) return buf;
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Response(stream).arrayBuffer();
  }

  /** Inverse of gridio.pack_field: planar prediction + zig-zag residuals in a low-byte plane then a high-byte plane. */
  function unpackField(b, nx, ny) {
    const n = nx * ny, out = new Int16Array(n);
    for (let j = 0; j < ny; j++) {
      const row = j * nx;
      for (let i = 0; i < nx; i++) {
        const k = row + i;
        const z = b[k] | (b[n + k] << 8);
        const r = (z >>> 1) ^ -(z & 1);
        const left = i ? out[k - 1] : 0, up = j ? out[k - nx] : 0, ul = (i && j) ? out[k - nx - 1] : 0;
        out[k] = left + up - ul + r;                           // Int16Array stores modulo 2^16, exactly like the encoder
      }
    }
    return out;
  }

  /** ArrayBuffer of an (inflated) .bin file -> {header fields..., t: Int16Array tenths, owner?: Uint8Array}. */
  function decodeBin(buf) {
    const u8 = new Uint8Array(buf);
    if (u8[0] !== 0x47 || u8[1] !== 0x4d || u8[2] !== 0x53 || u8[3] !== 0x31) throw new Error('not a grid file');
    const hl = new DataView(buf).getUint32(4, true);
    const hdr = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl)));
    const n = hdr.nx * hdr.ny;
    let off = 8 + hl;
    const g = Object.assign({}, hdr);
    g.t = unpackField(u8.subarray(off, off + 2 * n), hdr.nx, hdr.ny);
    off += 2 * n;
    if (u8.length >= off + n) g.owner = u8.slice(off, off + n);
    return g;
  }

  const geomOf = (g) => ({ nx: g.nx, ny: g.ny, lon0: g.lon0, lat0: g.lat0, dlon: g.dlon, dlat: g.dlat });
  const sameGeom = (a, b) => a.nx === b.nx && a.ny === b.ny && Math.abs(a.lon0 - b.lon0) < 1e-6 && Math.abs(a.lat0 - b.lat0) < 1e-6 &&
    Math.abs(a.dlon - b.dlon) < 1e-6 && Math.abs(a.dlat - b.dlat) < 1e-6;

  /** Int16 tenths -> Float32 hPa (NaN where missing). */
  function toHpa(t) {
    const v = new Float32Array(t.length);
    for (let k = 0; k < t.length; k++) v[k] = t[k] === MISSING ? NaN : t[k] / 10;
    return v;
  }
  const fieldOf = (g, v) => Object.assign(geomOf(g), { v });

  // ---------------------------------------------------------------------------------------------
  // regridding (blends of systems on different grids, e.g. GEPS 0.5 deg onto 0.25 deg)
  // ---------------------------------------------------------------------------------------------
  const planCache = new Map();
  const gkey = (g) => `${g.nx}x${g.ny}@${g.lon0},${g.lat0}+${g.dlon},${g.dlat}`;
  function regridPlan(src, dst) {
    const key = `${gkey(src)}>${gkey(dst)}`;
    if (planCache.has(key)) return planCache.get(key);
    const n = dst.nx * dst.ny;
    const i0 = new Int32Array(n), j0 = new Int32Array(n), fx = new Float32Array(n), fy = new Float32Array(n);
    const eps = 1e-6;
    for (let j = 0; j < dst.ny; j++) {
      const lat = dst.lat0 + j * dst.dlat, y = (lat - src.lat0) / src.dlat;
      for (let i = 0; i < dst.nx; i++) {
        const k = j * dst.nx + i, x = (dst.lon0 + i * dst.dlon - src.lon0) / src.dlon;
        if (x < -eps || y < -eps || x > src.nx - 1 + eps || y > src.ny - 1 + eps) { i0[k] = -1; continue; }
        const xi = Math.min(Math.max(Math.floor(x + eps), 0), src.nx - 2), yj = Math.min(Math.max(Math.floor(y + eps), 0), src.ny - 2);
        i0[k] = xi; j0[k] = yj; fx[k] = Math.min(Math.max(x - xi, 0), 1); fy[k] = Math.min(Math.max(y - yj, 0), 1);
      }
    }
    const plan = { src, dst, i0, j0, fx, fy };
    planCache.set(key, plan);
    return plan;
  }
  /** Bilinear resample of Float32 field values (NaN outside the source or next to a gap). */
  function regridLinear(v, plan) {
    const { src, dst, i0, j0, fx, fy } = plan, out = new Float32Array(dst.nx * dst.ny), nx = src.nx;
    for (let k = 0; k < out.length; k++) {
      if (i0[k] < 0) { out[k] = NaN; continue; }
      const p = j0[k] * nx + i0[k], a = v[p], b = v[p + 1], c = v[p + nx], d = v[p + nx + 1];
      const x = fx[k], y = fy[k];
      out[k] = (a * (1 - x) + b * x) * (1 - y) + (c * (1 - x) + d * x) * y;
    }
    return out;
  }
  /** Nearest-cell resample of an integer array (owner / hour); `fill` outside the source. */
  function regridNearest(arr, plan, fill) {
    const { src, dst, i0, j0, fx, fy } = plan, out = new arr.constructor(dst.nx * dst.ny);
    for (let k = 0; k < out.length; k++) {
      if (i0[k] < 0) { out[k] = fill; continue; }
      out[k] = arr[(j0[k] + (fy[k] >= 0.5 ? 1 : 0)) * src.nx + i0[k] + (fx[k] >= 0.5 ? 1 : 0)];
    }
    return out;
  }

  // ---------------------------------------------------------------------------------------------
  // blends
  // ---------------------------------------------------------------------------------------------
  /** Weighted mean of fields already on the same grid; NaN-aware. weights: [w, ...] */
  function blendMean(vs, weights) {
    const n = vs[0].length, out = new Float32Array(n);
    for (let k = 0; k < n; k++) {
      let num = 0, den = 0;
      for (let s = 0; s < vs.length; s++) { const x = vs[s][k]; if (x === x) { num += x * weights[s]; den += weights[s]; } }
      out[k] = den > 0 ? num / den : NaN;
    }
    return out;
  }

  /** Lowest value over the parts (all on one grid). parts: [{v, owner, offset, when?}] -> {v, owner:Int32Array, when?:Int16Array}.
   *  Ties keep the earlier part (like the Python pipeline did). */
  function lowestOf(parts) {
    const n = parts[0].v.length, v = new Float32Array(parts[0].v), owner = new Int32Array(n);
    const hasWhen = parts[0].when !== undefined, when = hasWhen ? new Int16Array(parts[0].when) : undefined;
    for (let k = 0; k < n; k++) owner[k] = parts[0].owner[k] + parts[0].offset;
    for (let s = 1; s < parts.length; s++) {
      const p = parts[s];
      for (let k = 0; k < n; k++) {
        const x = p.v[k];
        if (x === x && !(v[k] <= x)) {                          // lower than the current value (or the current one is NaN)
          v[k] = x; owner[k] = p.owner[k] + p.offset;
          if (hasWhen) when[k] = p.when[k];
        }
      }
    }
    return { v, owner, when };
  }

  // ---------------------------------------------------------------------------------------------
  // swath = running minimum of the member-wise minimum over forecast hours
  // ---------------------------------------------------------------------------------------------
  /** Start a swath state from the first hour's minimum grid (decodeBin result with t + owner). */
  function swathStart(g) {
    const n = g.t.length, when = new Int16Array(n).fill(g.fh);
    for (let k = 0; k < n; k++) if (g.t[k] === MISSING) when[k] = -1;
    return { fh: g.fh, geom: geomOf(g), t: Int16Array.from(g.t), owner: Uint8Array.from(g.owner), when };
  }
  /** Fold the next hour in place: lower (or newly available) cells take the new value, member and hour. */
  function swathFold(st, g) {
    const t = st.t, o = st.owner, w = st.when, gt = g.t, go = g.owner;
    for (let k = 0; k < t.length; k++) {
      const x = gt[k];
      if (x !== MISSING && (t[k] === MISSING || x < t[k])) { t[k] = x; o[k] = go[k]; w[k] = g.fh; }
    }
    st.fh = g.fh;
    return st;
  }
  const swathCopy = (st) => ({ fh: st.fh, geom: st.geom, t: Int16Array.from(st.t), owner: Uint8Array.from(st.owner), when: Int16Array.from(st.when) });

  // ---------------------------------------------------------------------------------------------
  // low-pressure centres: port of gefs_pipeline.products.detect_lows (gaussian_filter + minimum_filter)
  // ---------------------------------------------------------------------------------------------
  function gaussSmooth(v, nx, ny, sigma) {
    if (!(sigma > 0)) return v;
    const r = Math.floor(4 * sigma + 0.5), w = new Float64Array(2 * r + 1);
    let sum = 0;
    for (let d = -r; d <= r; d++) { w[d + r] = Math.exp(-0.5 * (d * d) / (sigma * sigma)); sum += w[d + r]; }
    for (let d = 0; d <= 2 * r; d++) w[d] /= sum;
    const tmp = new Float32Array(v.length), out = new Float32Array(v.length);
    for (let j = 0; j < ny; j++) {                              // along x, edges replicated ("nearest")
      const row = j * nx;
      for (let i = 0; i < nx; i++) {
        let s = 0;
        for (let d = -r; d <= r; d++) s += w[d + r] * v[row + Math.min(Math.max(i + d, 0), nx - 1)];
        tmp[row + i] = s;
      }
    }
    for (let j = 0; j < ny; j++) {                              // along y
      for (let i = 0; i < nx; i++) {
        let s = 0;
        for (let d = -r; d <= r; d++) s += w[d + r] * tmp[Math.min(Math.max(j + d, 0), ny - 1) * nx + i];
        out[j * nx + i] = s;
      }
    }
    return out;
  }
  function minSquare(v, nx, ny, r) {
    const tmp = new Float32Array(v.length), out = new Float32Array(v.length);
    for (let j = 0; j < ny; j++) {
      const row = j * nx;
      for (let i = 0; i < nx; i++) {
        let m = Infinity;
        for (let a = Math.max(0, i - r), b = Math.min(nx - 1, i + r); a <= b; a++) { const x = v[row + a]; if (x < m) m = x; }
        tmp[row + i] = m;
      }
    }
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        let m = Infinity;
        for (let a = Math.max(0, j - r), b = Math.min(ny - 1, j + r); a <= b; a++) { const x = tmp[a * nx + i]; if (x < m) m = x; }
        out[j * nx + i] = m;
      }
    }
    return out;
  }
  /** Local minima below `threshold_hpa` (smoothed to find the place, raw 3x3 minimum for the value).
   *  f = {nx, ny, lon0, lat0, dlon, dlat, v: Float32Array hPa}; p = {threshold_hpa, radius_deg, smooth_deg, min_separation_deg};
   *  domain = {west, east, south, north}.  Returns [{lon, lat, p, j, i}] deepest first, de-duplicated. */
  function detectLows(f, p, domain) {
    const { nx, ny, v } = f, res = Math.abs(f.dlat);
    const sm = gaussSmooth(v, nx, ny, Math.max(p.smooth_deg / res, 0));
    const mf = minSquare(sm, nx, ny, Math.round(p.radius_deg / res));
    const cands = [];
    for (let j = 1; j < ny - 1; j++) {
      for (let i = 1; i < nx - 1; i++) {
        const k = j * nx + i;
        if (!(sm[k] === mf[k])) continue;
        let best = Infinity, bj = j, bi = i;
        for (let a = j - 1; a <= j + 1; a++) for (let b = i - 1; b <= i + 1; b++) { const x = v[a * nx + b]; if (x < best) { best = x; bj = a; bi = b; } }
        const lat = f.lat0 + bj * f.dlat, lon = f.lon0 + bi * f.dlon;
        if (best < p.threshold_hpa && lon >= domain.west && lon <= domain.east && lat >= domain.south && lat <= domain.north) {
          cands.push({ p: best, lon, lat, j: bj, i: bi });
        }
      }
    }
    cands.sort((a, b) => a.p - b.p || a.lon - b.lon || a.lat - b.lat);
    const kept = [];
    for (const c of cands) {
      let close = false;
      for (const q of kept) {
        const dx = (c.lon - q.lon) * Math.cos(((c.lat + q.lat) / 2) * Math.PI / 180), dy = c.lat - q.lat;
        if (Math.hypot(dx, dy) < p.min_separation_deg) { close = true; break; }
      }
      if (!close) kept.push(c);
    }
    return kept;
  }

  // ---------------------------------------------------------------------------------------------
  // smooth display: Catmull-Rom upsampling and colour-class rasterisation
  // ---------------------------------------------------------------------------------------------
  /** Catmull-Rom upsample by an integer factor k (2 or 4); NaN spreads to the neighbouring cells. */
  function upsample(f, k) {
    const { nx, ny, v } = f, nx2 = (nx - 1) * k + 1, ny2 = (ny - 1) * k + 1;
    const wts = [];
    for (let s = 0; s < k; s++) {
      const t = s / k, t2 = t * t, t3 = t2 * t;
      wts.push([0.5 * (-t3 + 2 * t2 - t), 0.5 * (3 * t3 - 5 * t2 + 2), 0.5 * (-3 * t3 + 4 * t2 + t), 0.5 * (t3 - t2)]);
    }
    const tmp = new Float32Array(ny * nx2), out = new Float32Array(ny2 * nx2);
    for (let j = 0; j < ny; j++) {
      const row = j * nx;
      for (let I = 0; I < nx2; I++) {
        const i = Math.floor(I / k), s = I - i * k;
        if (s === 0) { tmp[j * nx2 + I] = v[row + i]; continue; }
        const w = wts[s], a = v[row + Math.max(i - 1, 0)], b = v[row + i], c = v[row + Math.min(i + 1, nx - 1)], d = v[row + Math.min(i + 2, nx - 1)];
        tmp[j * nx2 + I] = w[0] * a + w[1] * b + w[2] * c + w[3] * d;
      }
    }
    for (let J = 0; J < ny2; J++) {
      const j = Math.floor(J / k), s = J - j * k;
      for (let I = 0; I < nx2; I++) {
        if (s === 0) { out[J * nx2 + I] = tmp[j * nx2 + I]; continue; }
        const w = wts[s];
        out[J * nx2 + I] = w[0] * tmp[Math.max(j - 1, 0) * nx2 + I] + w[1] * tmp[j * nx2 + I] +
          w[2] * tmp[Math.min(j + 1, ny - 1) * nx2 + I] + w[3] * tmp[Math.min(j + 2, ny - 1) * nx2 + I];
      }
    }
    return { nx: nx2, ny: ny2, lon0: f.lon0, lat0: f.lat0, dlon: f.dlon / k, dlat: f.dlat / k, v: out };
  }

  /** Colour look-up for hPa values in tenths: class i covers [edges[i-1], edges[i]) (class 0 open below, last open above). */
  function makeLut(edges, colors) {
    const lo = Math.floor(edges[0]) - 60, hi = Math.ceil(edges[edges.length - 1]) + 60, n = (hi - lo) * 10 + 1;
    const rgba = colors.map((h) => {
      const x = parseInt(h.slice(1), 16);
      return ((255 << 24) | ((x & 255) << 16) | (((x >> 8) & 255) << 8) | (x >> 16)) >>> 0;       // ABGR for a little-endian Uint32 view
    });
    const lut = new Uint32Array(n);
    let c = 0;
    for (let q = 0; q < n; q++) {
      const val = lo + q / 10;
      while (c < edges.length && val >= edges[c]) c++;
      lut[q] = rgba[c];
    }
    return { lo, lut };
  }

  /** Rasterise a field into a (w x h) Uint32 RGBA buffer. lons[x], lats[y] = geographic position of each pixel centre. */
  function rasterize(f, lons, lats, lutObj, buf32) {
    const { nx, ny, v, lon0, lat0, dlon, dlat } = f, w = lons.length, h = lats.length, { lo, lut } = lutObj;
    buf32.fill(0);
    const ix = new Int32Array(w), wx = new Float32Array(w);
    for (let x = 0; x < w; x++) {
      const fx = (lons[x] - lon0) / dlon;
      if (fx < 0 || fx > nx - 1) { ix[x] = -1; continue; }
      const i = Math.min(Math.floor(fx), nx - 2);
      ix[x] = i; wx[x] = fx - i;
    }
    const nlut = lut.length;
    for (let y = 0; y < h; y++) {
      const fy = (lats[y] - lat0) / dlat;
      if (fy < 0 || fy > ny - 1) continue;
      const j = Math.min(Math.floor(fy), ny - 2), ty = fy - j, r0 = j * nx, r1 = r0 + nx, row = y * w;
      for (let x = 0; x < w; x++) {
        const i = ix[x];
        if (i < 0) continue;
        const tx = wx[x];
        const val = (v[r0 + i] * (1 - tx) + v[r0 + i + 1] * tx) * (1 - ty) + (v[r1 + i] * (1 - tx) + v[r1 + i + 1] * tx) * ty;
        if (val !== val) continue;
        let q = Math.round((val - lo) * 10);
        q = q < 0 ? 0 : q >= nlut ? nlut - 1 : q;
        buf32[row + x] = lut[q];
      }
    }
  }

  /** Bilinear value of a field at (lat, lon) in hPa, or null outside / next to a gap. */
  function valueAt(f, lat, lon) {
    if (!f) return null;
    const fx = (lon - f.lon0) / f.dlon, fy = (lat - f.lat0) / f.dlat;
    if (fx < 0 || fy < 0 || fx > f.nx - 1 || fy > f.ny - 1) return null;
    const x0 = Math.min(Math.floor(fx), f.nx - 2), y0 = Math.min(Math.floor(fy), f.ny - 2), tx = fx - x0, ty = fy - y0;
    const p = y0 * f.nx + x0, v = f.v;
    const r = (v[p] * (1 - tx) + v[p + 1] * tx) * (1 - ty) + (v[p + f.nx] * (1 - tx) + v[p + f.nx + 1] * tx) * ty;
    return r === r ? r : null;
  }
  /** Value of an integer array (owner / hour) at the nearest cell, or null. */
  function nearestAt(f, arr, lat, lon) {
    if (!f || !arr) return null;
    const i = Math.round((lon - f.lon0) / f.dlon), j = Math.round((lat - f.lat0) / f.dlat);
    if (i < 0 || j < 0 || i >= f.nx || j >= f.ny) return null;
    return arr[j * f.nx + i];
  }

  return { MISSING, inflate, unpackField, decodeBin, geomOf, sameGeom, toHpa, fieldOf, regridPlan, regridLinear, regridNearest,
    blendMean, lowestOf, swathStart, swathFold, swathCopy, gaussSmooth, minSquare, detectLows, upsample, makeLut, rasterize,
    valueAt, nearestAt };
});
