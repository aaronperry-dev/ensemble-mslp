// Node harness: decode pipeline output with the browser engine and print results as JSON for the Python tests.
//   node parity.js <run_dir> <fh> <lows_min json> <domain json> <systems comma list>
const fs = require('fs');
const path = require('path');
const E = require('../../site/engine.js');

async function load(dir, sys, kind, fh) {
  const buf = fs.readFileSync(path.join(dir, sys, kind, `f${String(fh).padStart(3, '0')}.bin.gz`));
  return E.decodeBin(await E.inflate(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)));
}

(async () => {
  const [dir, fhArg, lowsMin, domain, systems] = process.argv.slice(2);
  const fh = +fhArg, lm = JSON.parse(lowsMin), dom = JSON.parse(domain), sys = systems.split(',');
  const out = { systems: {}, blend: null, swath: null };
  for (const k of sys) {
    const g = await load(dir, k, 'min', fh);
    const f = E.fieldOf(g, E.toHpa(g.t));
    out.systems[k] = { tenths: Array.from(g.t), owner: Array.from(g.owner),
      lows: E.detectLows(f, lm, dom).map((c) => [c.lon, c.lat, c.p, c.j, c.i]), geom: E.geomOf(g) };
  }
  // blend of all systems (minimum): lowest over members, regridded to the first system's grid
  const gs = [];
  for (const k of sys) gs.push(await load(dir, k, 'min', fh));
  const ref = E.geomOf(gs[0]);
  let off = 0;
  const parts = gs.map((g, i) => {
    const v = E.toHpa(g.t), geom = E.geomOf(g);
    const member = g.members;
    const p = E.sameGeom(geom, ref) ? { v, owner: g.owner } : (() => { const plan = E.regridPlan(geom, ref); return { v: E.regridLinear(v, plan), owner: E.regridNearest(g.owner, plan, 0) }; })();
    const r = { ...p, offset: off }; off += member; return r;
  });
  const low = E.lowestOf(parts);
  out.blend = { v: Array.from(low.v, (x) => (x === x ? Math.round(x * 100) / 100 : null)), owner: Array.from(low.owner), ref };
  // swath of the first system over every hour up to fh
  const frames = fs.readdirSync(path.join(dir, sys[0], 'min')).map((n) => +n.slice(1, 4)).sort((a, b) => a - b).filter((h) => h <= fh);
  let st = null;
  for (const h of frames) { const g = await load(dir, sys[0], 'min', h); st = st ? E.swathFold(st, g) : E.swathStart(g); }
  out.swath = { tenths: Array.from(st.t), owner: Array.from(st.owner), when: Array.from(st.when), hours: frames };
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
