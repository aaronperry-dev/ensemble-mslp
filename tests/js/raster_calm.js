// rasterize() with the calm-cell skip must give exactly the pixels of the full computation, for every colour table and edge case.
const E = require('../../site/engine.js');
let seed = 12345; const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
const out = { cases: [] };
const tables = {
  wind: { edges: [10, 25, 39, 50, 74], colors: ['transparent', 'transparent', '#aaaaaa', '#bbbbbb', '#cccccc', '#dddddd'] },
  prob: { edges: [5, 10, 20, 50, 90], colors: ['transparent', '#111111', '#222222', '#333333', '#444444', '#555555'] },
  mslp: { edges: [980, 990, 1000, 1010], colors: ['#010101', '#020202', '#030303', '#040404', '#050505'] },        // no transparent class: calm = -Infinity
};
for (const [name, t] of Object.entries(tables)) {
  const lutObj = E.makeLut(t.edges, t.colors), plain = { lo: lutObj.lo, lut: lutObj.lut };                          // plain: no calm -> full path
  const nx = 40, ny = 30, v = new Float32Array(nx * ny);
  const hi = t.edges[t.edges.length - 1] + 20, lo = Math.max(0, t.edges[0] - 15);
  for (let k = 0; k < v.length; k++) {
    const r = rnd();
    v[k] = r < 0.02 ? NaN : r < 0.3 ? t.edges[Math.floor(rnd() * t.edges.length)] - 0.04 - rnd() * 0.05      // hugging an edge from below
      : r < 0.4 ? t.edges[Math.floor(rnd() * t.edges.length)] + rnd() * 0.05 : lo + rnd() * (hi - lo);
  }
  const f = { nx, ny, v, lon0: -80, lat0: 30, dlon: 0.25, dlat: 0.25 };
  for (const [W, H] of [[97, 61], [390, 400]]) {
    const lons = new Float64Array(W), lats = new Float64Array(H);
    for (let x = 0; x < W; x++) lons[x] = -81 + 11 * x / W; for (let y = 0; y < H; y++) lats[y] = 38 - 9 * y / H;
    const a = new Uint32Array(W * H), b = new Uint32Array(W * H);
    E.rasterize(f, lons, lats, lutObj, a); E.rasterize(f, lons, lats, plain, b);
    let diff = 0, drawn = 0; for (let i = 0; i < a.length; i++) { if (a[i] !== b[i]) diff++; if (a[i]) drawn++; }
    out.cases.push({ name, W, H, diff, drawn, calm: lutObj.calm });
  }
}
console.log(JSON.stringify(out));
