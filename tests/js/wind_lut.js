// The browser's colour look-up (makeLut) on the wind / gust legend scales: reads {edges, colors, values} as JSON on stdin and prints
// the "#rrggbb" (or "transparent") the engine would paint for every value, plus the transparent-skip threshold.
const E = require('../../site/engine.js');
let input = '';
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', () => {
  const { edges, colors, values } = JSON.parse(input), L = E.makeLut(edges, colors);
  const hex = (u) => u === 0 ? 'transparent' : '#' + [u & 255, (u >> 8) & 255, (u >>> 16) & 255].map((x) => x.toString(16).padStart(2, '0')).join('');
  const got = values.map((v) => { let q = Math.round((v - L.lo) * 10); q = q < 0 ? 0 : q >= L.lut.length ? L.lut.length - 1 : q; return hex(L.lut[q]); });
  console.log(JSON.stringify({ got, calm: L.calm }));
});
