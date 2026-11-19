/* Animated GIF encoder tuned for map animations (no DOM; runs in the browser and under Node for the tests).
 *
 *   const g = MslpGif.createGif(width, height, { loop: 0, comment: 'title | subtitle' });   // comment is optional
 *   g.addFrame(rgbaUint8, delayMs);            // as many as needed (stored as 15-bit colour, 2 bytes per pixel)
 *   const bytes = g.finish();                  // Uint8Array containing the .gif file
 *
 * What keeps the file small:
 *   - ONE global palette for the whole animation (median cut over the colours of every frame, weighted by use),
 *     so there are no per-frame colour tables.  When the frames use <= 255 distinct 15-bit colours the palette is exact.
 *   - Frame differencing: after the first frame each frame stores only the bounding box of what changed, and the
 *     unchanged pixels inside it are the transparent index (disposal "leave in place").  Base map, legend and
 *     title cost nothing after frame 1; identical frames cost a single pixel.
 *   - Variable-width LZW with a typed-array hash (no flat dithering noise: pixels are mapped, not dithered).
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

  /** Median cut over a 15-bit colour histogram. Returns {pal: Uint8Array(3n), map: Uint8Array(32768) colour -> palette index}. */
  function medianCut(hist, maxColors) {
    const used = [];
    for (let c = 0; c < 32768; c++) if (hist[c]) used.push(c);
    const rgb = (c) => [(c >> 10) & 31, (c >> 5) & 31, c & 31];
    const map = new Uint8Array(32768);
    let boxes = used.length > maxColors ? [used] : used.map((c) => [c]);            // few colours: the palette is exact
    if (used.length > maxColors) {
      const score = (box) => {
        let lo = [31, 31, 31], hi = [0, 0, 0], tot = 0;
        for (const c of box) { const v = rgb(c); tot += hist[c]; for (let k = 0; k < 3; k++) { if (v[k] < lo[k]) lo[k] = v[k]; if (v[k] > hi[k]) hi[k] = v[k]; } }
        const range = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
        const ch = range[1] >= range[0] && range[1] >= range[2] ? 1 : range[0] >= range[2] ? 0 : 2;
        return { s: box.length < 2 ? -1 : range[ch] * Math.sqrt(tot), ch, tot };
      };
      let sc = [score(used)];
      while (boxes.length < maxColors) {
        let bi = -1, best = 0;
        for (let i = 0; i < boxes.length; i++) if (sc[i].s > best) { best = sc[i].s; bi = i; }
        if (bi < 0) break;
        const box = boxes[bi], ch = sc[bi].ch, sh = [10, 5, 0][ch];
        box.sort((a, b) => ((a >> sh) & 31) - ((b >> sh) & 31));
        let acc = 0, cut = 0;
        for (; cut < box.length - 1; cut++) { acc += hist[box[cut]]; if (acc >= sc[bi].tot / 2) break; }
        cut = Math.min(Math.max(cut + 1, 1), box.length - 1);
        const a = box.slice(0, cut), b = box.slice(cut);
        boxes[bi] = a; sc[bi] = score(a);
        boxes.push(b); sc.push(score(b));
      }
    }
    const pal = new Uint8Array(boxes.length * 3);
    boxes.forEach((box, i) => {
      let r = 0, g = 0, b = 0, tot = 0;
      for (const c of box) { const v = rgb(c), w = hist[c]; r += v[0] * w; g += v[1] * w; b += v[2] * w; tot += w; map[c] = i; }
      const e = (x) => { const v = Math.round(x / tot); return (v << 3) | (v >> 2); };       // 5 bit -> 8 bit
      pal[i * 3] = e(r); pal[i * 3 + 1] = e(g); pal[i * 3 + 2] = e(b);
    });
    return { pal, map, n: boxes.length };
  }

  function createGif(width, height, opts) {
    opts = opts || {};
    const frames = [], hist = new Uint32Array(32768), npx = width * height;
    return {
      /** rgba: Uint8ClampedArray / Uint8Array of width*height*4 (alpha ignored). */
      addFrame(rgba, delayMs) {
        const c = new Uint16Array(npx);
        for (let i = 0, j = 0; i < npx; i++, j += 4) {
          const v = ((rgba[j] >> 3) << 10) | ((rgba[j + 1] >> 3) << 5) | (rgba[j + 2] >> 3);
          c[i] = v; hist[v]++;
        }
        frames.push({ c, delay: Math.max(2, Math.round((delayMs || 100) / 10)) });
      },
      get frameCount() { return frames.length; },
      finish() {
        if (!frames.length) throw new Error('no frames');
        const { pal, map, n } = medianCut(hist, 255);
        let bits = 2; while ((1 << bits) < n + 1) bits++;                 // table holds the palette + one transparent entry
        const tsize = 1 << bits, T = n;                                    // transparent index = first unused entry
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
        let prev = null;
        for (const f of frames) {
          const idx = new Uint8Array(npx);
          for (let i = 0; i < npx; i++) idx[i] = map[f.c[i]];
          let x0 = 0, y0 = 0, w = width, h = height, region = idx, transp = false;
          if (prev) {
            let minx = width, miny = height, maxx = -1, maxy = -1;
            for (let y = 0, k = 0; y < height; y++) for (let x = 0; x < width; x++, k++) {
              if (idx[k] !== prev[k]) { if (x < minx) minx = x; if (x > maxx) maxx = x; if (y < miny) miny = y; if (y > maxy) maxy = y; }
            }
            if (maxx < 0) { x0 = 0; y0 = 0; w = 1; h = 1; region = new Uint8Array([T]); transp = true; }     // nothing changed
            else {
              x0 = minx; y0 = miny; w = maxx - minx + 1; h = maxy - miny + 1;
              region = new Uint8Array(w * h); transp = true;
              for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
                const k = (y0 + y) * width + x0 + x;
                region[y * w + x] = idx[k] === prev[k] ? T : idx[k];
              }
            }
          }
          out.byte(0x21); out.byte(0xf9); out.byte(4); out.byte((1 << 2) | (transp ? 1 : 0)); out.u16(f.delay); out.byte(transp ? T : 0); out.byte(0);
          out.byte(0x2c); out.u16(x0); out.u16(y0); out.u16(w); out.u16(h); out.byte(0);
          lzw(out, region, bits);
          prev = idx;
        }
        out.byte(0x3b);
        frames.length = 0;
        return out.done();
      },
    };
  }

  return { createGif, medianCut };
});
