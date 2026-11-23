/* Animated GIF encoder tuned for map animations (no DOM; runs in the browser and under Node for the tests).
 *
 *   const g = MslpGif.createGif(width, height, { loop: 0, comment: 'title | subtitle' });   // comment is optional
 *   g.addFrame(rgbaUint8, delayMs);            // as many as needed (each frame is kept as 2 bytes per pixel)
 *   const bytes = g.finish({ maxColors: 255, tolerance: 0 });   // Uint8Array containing the .gif file (options are optional)
 *   g.stats                                    // {colors, exact, frames, written} of the last finish()
 *   g.dispose();                               // free the frames (finish() may be called again with other options until then)
 *
 * Quality (crisp, no banding, no noise):
 *   - Colours are EXACT 24-bit values: every distinct colour of every frame goes into one dictionary (up to ~65 000; beyond
 *     that the rarest new ones snap to a 15-bit cell).  When the animation uses <= 255 colours (flat class colours, a
 *     legend, a plain base map) the palette is exact and every pixel is reproduced bit for bit.
 *   - More colours (anti-aliased text, lines and coastlines over many fills) are reduced to ONE global palette: weighted
 *     median cut, refined by a few k-means passes, and any colour that dominates its palette entry is kept EXACTLY, so the big
 *     flat areas stay exactly right and only the anti-aliasing blends are approximated.  Each pixel then takes its NEAREST
 *     palette colour.  No dithering: dithering would add noise to flat areas and cost file size.
 * What keeps the file small:
 *   - One global palette, no per-frame colour tables.
 *   - Frame differencing: after the first frame each frame stores only the bounding box of what changed; unchanged pixels
 *     inside it are the transparent index (disposal "leave in place") - or, when that makes a longer run for the LZW coder,
 *     their own colour (identical on screen).  Frames identical to the previous one are folded into its delay.
 *   - Variable-width LZW with a typed-array hash.
 *   - Optional `tolerance` (0 = lossless): a pixel whose new colour is within that RGB distance of what is already shown is
 *     left alone.  `maxColors` shrinks the palette.  Both are only used when a file would otherwise be too large.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MslpGif = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  class Sink {
    constructor() { this.b = new Uint8Array(1 << 16); this.n = 0; }
    ensure(k) { if (this.n + k > this.b.length) { const nb = new Uint8Array(Math.max(this.b.length * 2, this.n + k)); nb.set(this.b.subarray(0, this.n)); this.b = nb; } }
    byte(v) { this.ensure(1); this.b[this.n++] = v; }
    u16(v) { this.byte(v & 255); this.byte(v >> 8); }
    str(s) { for (let i = 0; i < s.length; i++) this.byte(s.charCodeAt(i)); }
    bytes(a) { this.ensure(a.length); this.b.set(a, this.n); this.n += a.length; }
    done() { return this.b.slice(0, this.n); }
  }

  /** Variable-width LZW of an index stream, written as GIF data sub-blocks (min code size byte first). */
  function lzw(sink, pix, minCode) {
    const clear = 1 << minCode, eoi = clear + 1, HS = 16384, MASK = HS - 1;
    const keys = new Int32Array(HS), vals = new Uint16Array(HS);
    keys.fill(-1);
    let nextCode = eoi + 1, codeSize = minCode + 1, cur = 0, shift = 0;
    const block = new Uint8Array(255); let bn = 0;
    const flushBlock = () => { if (bn) { sink.byte(bn); sink.bytes(block.subarray(0, bn)); bn = 0; } };
    const put = (code) => {
      cur |= code << shift; shift += codeSize;
      while (shift >= 8) { block[bn++] = cur & 255; cur >>>= 8; shift -= 8; if (bn === 255) flushBlock(); }
    };
    sink.byte(minCode);
    put(clear);
    let ib = pix[0];
    for (let i = 1; i < pix.length; i++) {
      const k = pix[i], key = (ib << 8) | k;
      let h = Math.imul(key, 2654435761) >>> 18 & MASK;                 // 14-bit multiplicative hash
      while (keys[h] !== -1 && keys[h] !== key) h = (h + 1) & MASK;
      if (keys[h] === key) { ib = vals[h]; continue; }
      put(ib);
      if (nextCode === 4096) {
        put(clear); nextCode = eoi + 1; codeSize = minCode + 1; keys.fill(-1);
      } else {
        if (nextCode >= (1 << codeSize)) codeSize++;
        keys[h] = key; vals[h] = nextCode++;
      }
      ib = k;
    }
    put(ib); put(eoi);
    if (shift > 0) { block[bn++] = cur & 255; if (bn === 255) flushBlock(); }
    flushBlock();
    sink.byte(0);
  }

  // colour distance: the eye is most sensitive to green, least to blue
  const WR = 3, WG = 4, WB = 2;
  const CAP_EXACT = 32767;                       // exact colours in the dictionary; the rest of the 65535 ids are 15-bit cells

  /**
   * Reduce the colour dictionary to at most maxColors palette entries.
   *   cols  Uint8Array(3 * nIds)  RGB of every dictionary colour       cnt  Uint32Array(nIds)  pixels using it (all frames)
   * Returns {pal: Uint8Array(3n), remap: Uint8Array(nIds) dictionary id -> palette index, n}.
   */
  function quantize(cols, cnt, nIds, maxColors) {
    const remap = new Uint8Array(nIds);
    if (nIds <= maxColors) {                                                        // few colours: the palette is exact
      for (let i = 0; i < nIds; i++) remap[i] = i;
      return { pal: cols.slice(0, nIds * 3), remap, n: nIds };
    }
    // 1. weighted median cut
    const ch = (id, c) => cols[id * 3 + c];
    const score = (box) => {
      const lo = [255, 255, 255], hi = [0, 0, 0]; let tot = 0;
      for (let i = 0; i < box.length; i++) { const id = box[i]; tot += cnt[id]; for (let c = 0; c < 3; c++) { const v = cols[id * 3 + c]; if (v < lo[c]) lo[c] = v; if (v > hi[c]) hi[c] = v; } }
      const r = [(hi[0] - lo[0]) * Math.sqrt(WR), (hi[1] - lo[1]) * Math.sqrt(WG), (hi[2] - lo[2]) * Math.sqrt(WB)];
      const c = r[1] >= r[0] && r[1] >= r[2] ? 1 : r[0] >= r[2] ? 0 : 2;
      return { s: box.length < 2 ? -1 : r[c] * Math.sqrt(tot), c, tot };
    };
    const all = new Array(nIds); for (let i = 0; i < nIds; i++) all[i] = i;
    const boxes = [all], sc = [score(all)];
    while (boxes.length < maxColors) {
      let bi = -1, best = 0;
      for (let i = 0; i < boxes.length; i++) if (sc[i].s > best) { best = sc[i].s; bi = i; }
      if (bi < 0) break;
      const box = boxes[bi], c = sc[bi].c, keys = new Uint32Array(box.length);
      for (let i = 0; i < box.length; i++) keys[i] = (ch(box[i], c) << 16) | box[i];
      keys.sort();
      let acc = 0, cut = 0;
      for (; cut < keys.length - 1; cut++) { acc += cnt[keys[cut] & 0xffff]; if (acc >= sc[bi].tot / 2) break; }
      cut = Math.min(Math.max(cut + 1, 1), keys.length - 1);
      const a = new Array(cut), b = new Array(keys.length - cut);
      for (let i = 0; i < cut; i++) a[i] = keys[i] & 0xffff;
      for (let i = cut; i < keys.length; i++) b[i - cut] = keys[i] & 0xffff;
      boxes[bi] = a; sc[bi] = score(a);
      boxes.push(b); sc.push(score(b));
    }
    const n = boxes.length, cen = new Float64Array(n * 3);
    const centroids = () => {
      const sum = new Float64Array(n * 3), w = new Float64Array(n);
      for (let i = 0; i < nIds; i++) { const p = remap[i], k = cnt[i]; w[p] += k; for (let c = 0; c < 3; c++) sum[p * 3 + c] += cols[i * 3 + c] * k; }
      for (let p = 0; p < n; p++) if (w[p] > 0) for (let c = 0; c < 3; c++) cen[p * 3 + c] = sum[p * 3 + c] / w[p];
      return w;
    };
    boxes.forEach((box, p) => { for (let i = 0; i < box.length; i++) remap[box[i]] = p; });
    centroids();
    // 2. nearest-colour assignment + centroid update (k-means), then 3. a palette entry dominated by one colour becomes that exact colour
    const assign = () => {
      let moved = 0;
      for (let i = 0; i < nIds; i++) {
        const r = cols[i * 3], g = cols[i * 3 + 1], b = cols[i * 3 + 2];
        let bp = 0, bd = Infinity;
        for (let p = 0; p < n; p++) {
          const dr = r - cen[p * 3], dg = g - cen[p * 3 + 1], db = b - cen[p * 3 + 2], d = WR * dr * dr + WG * dg * dg + WB * db * db;
          if (d < bd) { bd = d; bp = p; }
        }
        if (remap[i] !== bp) { remap[i] = bp; moved++; }
      }
      return moved;
    };
    for (let it = 0; it < 4; it++) { if (assign() === 0) break; centroids(); }
    const w = centroids(), top = new Int32Array(n).fill(-1);
    for (let i = 0; i < nIds; i++) { const p = remap[i]; if (top[p] < 0 || cnt[i] > cnt[top[p]]) top[p] = i; }
    const snapped = new Uint8Array(n);
    for (let p = 0; p < n; p++) if (top[p] >= 0 && cnt[top[p]] >= 0.4 * w[p]) { snapped[p] = 1; for (let c = 0; c < 3; c++) cen[p * 3 + c] = cols[top[p] * 3 + c]; }
    assign();
    const keep = Float64Array.from(cen);                                           // the exact colours survive the final centroid update
    centroids();
    for (let p = 0; p < n; p++) if (snapped[p]) for (let c = 0; c < 3; c++) cen[p * 3 + c] = keep[p * 3 + c];
    // 4. drop entries nothing maps to
    const used = new Int32Array(n).fill(-1); let m = 0;
    for (let i = 0; i < nIds; i++) if (used[remap[i]] < 0) used[remap[i]] = m++;
    const pal = new Uint8Array(m * 3);
    for (let p = 0; p < n; p++) if (used[p] >= 0) for (let c = 0; c < 3; c++) pal[used[p] * 3 + c] = Math.round(cen[p * 3 + c]);
    for (let i = 0; i < nIds; i++) remap[i] = used[remap[i]];
    return { pal, remap, n: m };
  }

  function createGif(width, height, opts) {
    opts = opts || {};
    const frames = [], npx = width * height;
    const HB = 17, HMASK = (1 << HB) - 1;
    const tkeys = new Int32Array(1 << HB).fill(-1), tvals = new Uint16Array(1 << HB);     // exact 24-bit colour -> dictionary id
    const coarse = new Int32Array(32768).fill(-1);                                         // 15-bit cell -> id (only once the exact table is full)
    const cols = new Uint8Array(65536 * 3), cnt = new Uint32Array(65536);
    let nIds = 0, nExact = 0;

    const g = {
      /** rgba: Uint8ClampedArray / Uint8Array of width*height*4 (alpha ignored). */
      addFrame(rgba, delayMs) {
        const ids = new Uint16Array(npx);
        let lastC = -1, lastId = 0;
        for (let i = 0, j = 0; i < npx; i++, j += 4) {
          const r = rgba[j], gg = rgba[j + 1], b = rgba[j + 2], c = (r << 16) | (gg << 8) | b;
          if (c !== lastC) {                                                 // runs of one colour are the common case
            let h = Math.imul(c, 2654435761) >>> (32 - HB);
            while (tkeys[h] !== -1 && tkeys[h] !== c) h = (h + 1) & HMASK;
            let id;
            if (tkeys[h] === c) id = tvals[h];
            else if (nExact < CAP_EXACT) {
              id = nIds++; nExact++; tkeys[h] = c; tvals[h] = id;
              cols[id * 3] = r; cols[id * 3 + 1] = gg; cols[id * 3 + 2] = b;
            } else {                                                         // dictionary full: snap to the 15-bit cell
              const cell = ((r >> 3) << 10) | ((gg >> 3) << 5) | (b >> 3);
              id = coarse[cell];
              if (id < 0) { id = coarse[cell] = nIds++; cols[id * 3] = (r & 248) | 4; cols[id * 3 + 1] = (gg & 248) | 4; cols[id * 3 + 2] = (b & 248) | 4; }
            }
            lastC = c; lastId = id;
          }
          ids[i] = lastId; cnt[lastId]++;
        }
        frames.push({ ids, delay: Math.max(2, Math.round((delayMs || 100) / 10)) });
      },
      get frameCount() { return frames.length; },
      /** Encode.  o.maxColors (2..255, default 255) and o.tolerance (RGB distance, default 0 = lossless). */
      finish(o) {
        if (!frames.length) throw new Error('no frames');
        o = o || {};
        const maxColors = Math.max(2, Math.min(255, Math.round(o.maxColors || 255))), tol = Math.max(0, o.tolerance || 0);
        const { pal, remap, n } = quantize(cols, cnt, nIds, maxColors);
        let bits = 2; while ((1 << bits) < n + 1) bits++;                 // table holds the palette + one transparent entry
        const tsize = 1 << bits, T = n;                                    // transparent index = first unused entry
        // pairs of palette colours closer than the tolerance (only when it is used)
        let close = null;
        if (tol > 0) {
          close = new Uint8Array(n * n); const t2 = tol * tol;
          for (let a = 0; a < n; a++) for (let b = 0; b < n; b++) {
            const dr = pal[a * 3] - pal[b * 3], dg = pal[a * 3 + 1] - pal[b * 3 + 1], db = pal[a * 3 + 2] - pal[b * 3 + 2];
            close[a * n + b] = dr * dr + dg * dg + db * db <= t2 ? 1 : 0;
          }
        }
        const shown = new Uint8Array(npx), entries = [], idx = new Uint8Array(npx);
        for (const f of frames) {
          for (let i = 0; i < npx; i++) idx[i] = remap[f.ids[i]];
          if (!entries.length) { shown.set(idx); entries.push({ x0: 0, y0: 0, w: width, h: height, region: idx.slice(), transp: false, delay: f.delay }); continue; }
          let minx = width, miny = height, maxx = -1, maxy = -1;
          for (let y = 0, k = 0; y < height; y++) for (let x = 0; x < width; x++, k++) {
            const a = idx[k], s = shown[k];
            if (a !== s && !(close && close[a * n + s])) { if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y; }
          }
          if (maxx < 0) { const e = entries[entries.length - 1]; e.delay = Math.min(65535, e.delay + f.delay); continue; }       // nothing changed: just show the last frame longer
          const w = maxx - minx + 1, h = maxy - miny + 1, region = new Uint8Array(w * h);
          for (let y = 0; y < h; y++) {
            let left = T;
            for (let x = 0; x < w; x++) {
              const k = (miny + y) * width + minx + x, a = idx[k], s = shown[k];
              let v;
              if (a !== s && !(close && close[a * n + s])) { v = a; shown[k] = a; }
              else v = left === s ? s : T;                                   // unchanged: its own colour if that continues a run, else transparent
              region[y * w + x] = v; left = v;
            }
          }
          entries.push({ x0: minx, y0: miny, w, h, region, transp: true, delay: f.delay });
        }
        const out = new Sink();
        out.str('GIF89a'); out.u16(width); out.u16(height);
        out.byte(0x80 | ((bits - 1) << 4) | (bits - 1)); out.byte(0); out.byte(0);
        const table = new Uint8Array(tsize * 3); table.set(pal); out.bytes(table);
        out.byte(0x21); out.byte(0xff); out.byte(11); out.str('NETSCAPE2.0'); out.byte(3); out.byte(1); out.u16(opts.loop || 0); out.byte(0);
        if (opts.comment) {                                                // Comment Extension: the file describes itself (title, subtitle, run, hours)
          const text = String(opts.comment).replace(/[^\x20-\x7e]/g, '?');           // GIF comments are 7-bit ASCII
          out.byte(0x21); out.byte(0xfe);
          for (let i = 0; i < text.length; i += 255) { const part = text.slice(i, i + 255); out.byte(part.length); out.str(part); }
          out.byte(0);
        }
        for (const e of entries) {
          out.byte(0x21); out.byte(0xf9); out.byte(4); out.byte((1 << 2) | (e.transp ? 1 : 0)); out.u16(e.delay); out.byte(e.transp ? T : 0); out.byte(0);
          out.byte(0x2c); out.u16(e.x0); out.u16(e.y0); out.u16(e.w); out.u16(e.h); out.byte(0);
          lzw(out, e.region, bits);
        }
        out.byte(0x3b);
        g.stats = { colors: n, exact: nIds <= maxColors, frames: entries.length, written: frames.length, dictionary: nIds };
        return out.done();
      },
      dispose() { frames.length = 0; },
      stats: null,
    };
    return g;
  }

  return { createGif, quantize };
});
