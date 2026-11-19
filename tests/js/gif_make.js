// Node harness: build a synthetic animation with the GIF encoder and write it to a file.
//   node gif_make.js <out.gif> <frames> <width> <height> [many_colours]
const fs = require('fs');
const G = require('../../site/gif.js');
const [out, nfr, W, H, many] = process.argv.slice(2);
const w = +W, h = +H, n = +nfr;
const g = G.createGif(w, h, { loop: 0 });
let seed = 12345; const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
const frames = [];
for (let f = 0; f < n; f++) {
  const px = new Uint8Array(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const k = (y * w + x) * 4;
    let c = [20 + (x >> 3) % 2 * 8, 30, 50];                                   // static "basemap"
    const dx = x - (10 + f * 3), dy = y - h / 2;
    if (dx * dx + dy * dy < 100) c = [220, 40 + (f * 10) % 200, 60];             // moving disc
    if (y > h - 6) c = [240, 240, 240];                                            // static "legend"
    if (many === '1') c = [(x * 7 + f) % 256, (y * 5) % 256, (x + y + f * 3) % 256]; // > 255 colours
    px[k] = c[0]; px[k + 1] = c[1]; px[k + 2] = c[2]; px[k + 3] = 255;
  }
  frames.push(px);
  g.addFrame(px, f === n - 1 ? 1500 : 200);
}
const bytes = g.finish();
fs.writeFileSync(out, bytes);
fs.writeFileSync(out + '.frames', Buffer.concat(frames.map((p) => Buffer.from(p))));
console.log(JSON.stringify({ bytes: bytes.length }));
