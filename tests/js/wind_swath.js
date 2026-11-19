// Node harness for wind / gust grids: decode with the header scale (half-mph steps) and fold a running MAXIMUM swath + highest-of-systems.
//   node wind_swath.js <a/wmax/f006.bin.gz> <a/wmax/f012.bin.gz> <b/wmax/f012.bin.gz>
const fs = require('fs');
const E = require('../../site/engine.js');
const load = async (p) => { const b = fs.readFileSync(p); return E.decodeBin(await E.inflate(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength))); };
(async () => {
  const [a6, a12, b12] = await Promise.all(process.argv.slice(2).map(load));
  let st = E.swathStart(a6); st = E.swathFold(st, a12, 1);
  const v = E.toHpa(st.t, st.scale);
  // highest of two systems on one grid; owner offsets are applied by lowestOf
  const hi = E.lowestOf([{ v: E.toHpa(a12.t, a12.scale), owner: a12.owner, offset: 0 }, { v: E.toHpa(b12.t, b12.scale), owner: b12.owner, offset: 10 }], 1);
  const lo = E.lowestOf([{ v: E.toHpa(a12.t, a12.scale), owner: a12.owner, offset: 0 }, { v: E.toHpa(b12.t, b12.scale), owner: b12.owner, offset: 10 }]);
  console.log(JSON.stringify({ scale: a6.scale, stscale: st.scale, swath: Array.from(v), when: Array.from(st.when), hi: Array.from(hi.v), hiOwner: Array.from(hi.owner),
    lo: Array.from(lo.v), a12: Array.from(E.toHpa(a12.t, a12.scale)), a6: Array.from(E.toHpa(a6.t, a6.scale)), nx: a6.nx, ny: a6.ny }));
})();
