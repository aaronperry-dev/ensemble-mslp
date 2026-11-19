// Node harness: decode one probability level only (decodeBin opts.level) and compare with the full decode.
//   node plane_only.js <prob.bin.gz> <level_hPa>
const fs = require('fs');
const E = require('../../site/engine.js');
(async () => {
  const [path, level] = process.argv.slice(2);
  const buf = fs.readFileSync(path);
  const raw = await E.inflate(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const full = E.decodeBin(raw), one = E.decodeBin(raw, { level: +level });
  const k = full.levels.indexOf(+level);
  console.log(JSON.stringify({
    k, filled: one.planes.map((p, i) => (p ? i : -1)).filter((i) => i >= 0), nfull: full.planes.length,
    same: Array.from(one.planes[k]).every((v, i) => v === full.planes[k][i]),
    pct: Array.from(E.probPercent(one, k)).every((v, i) => v === E.probPercent(full, k)[i]),
    members: one.members, levels: one.levels,
  }));
})();
