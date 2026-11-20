// Node harness for the swath of a MEAN field: fold hourly float fields (NaN = missing) into a running extreme and the hour it was reached.
const E = require('../../site/engine.js');
const g = { nx: 4, ny: 2, lon0: -100, lat0: 30, dlon: 1, dlat: 1 };
const mk = (a) => Object.assign({}, g, { v: Float32Array.from(a) });
const f0 = mk([1000, 1010, NaN, 990, 5, 6, 7, 8]), f6 = mk([1005, 1000, 1001, NaN, 9, 2, 7, NaN]), f12 = mk([995, 1020, 999, 980, 4, 12, 7.5, 1]);
const run = (dir) => {
  let st = E.fieldSwathStart(f0, 0);
  const snap = E.fieldSwathCopy(st);
  st = E.fieldSwathFold(st, f6, 6, dir);
  const mid = E.fieldSwathCopy(st);
  st = E.fieldSwathFold(st, f12, 12, dir);
  return { first: Array.from(snap.v), firstWhen: Array.from(snap.when), mid: Array.from(mid.v), midWhen: Array.from(mid.when), v: Array.from(st.v), when: Array.from(st.when), fh: st.fh, copyIndependent: snap.v !== st.v && snap.v.buffer !== st.v.buffer };
};
console.log(JSON.stringify({ low: run(-1), high: run(1) }));
