// Node harness: probability planes + strike counts from the browser engine, for the Python tests.
//   node strike_prob.js <prob.bin.gz> <input.json>      input: {tracks, geom, radius_km, level, frame_fhs}
const fs = require('fs');
const E = require('../../site/engine.js');
(async () => {
  const [probPath, inPath] = process.argv.slice(2);
  const buf = fs.readFileSync(probPath);
  const g = E.decodeBin(await E.inflate(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)));
  const inp = JSON.parse(fs.readFileSync(inPath, 'utf8'));
  const res = E.strikeCounts(inp.tracks, inp.geom, inp.radius_km, inp.level, inp.frame_fhs);
  console.log(JSON.stringify({
    levels: g.levels, members: g.members, planes: g.planes.map((p) => Array.from(p)),
    pct0: Array.from(E.probPercent(g, 0).slice(0, 5)),
    cum: res.cum.map((a) => Array.from(a)),
    pctLast: Array.from(E.strikePercent(res, res.cum.length - 1, inp.n_members).slice(0, 8)),
  }));
})();
