/* Ensemble MSLP viewer (NOAA GEFS, ECMWF IFS ENS, Canadian GEPS, NOAA AIGEFS, and blends of any two or more of them) — Leaflet, no build step, no runtime deps.
 *
 * Data contract (written by gefs_pipeline, manifest schema 3):
 *   data/manifest.json                          run info, families {gefs, ifs, geps, aigefs} (+ member lists), blend weighting,
 *                                               lows_min parameters, products (class edges + colours), frames[{fh, valid, avail}]
 *   data/<run>/<system>/mean/fNNN.bin.gz        ensemble mean grid + member lows       (gzip'd, see gefs_pipeline/gridio.py)
 *   data/<run>/<system>/min/fNNN.bin.gz         member-wise minimum grid + owner member
 *   data/basemap/{land,lakes,coast,admin0,admin1}.json.gz   Natural Earth vectors
 *
 * Only these per-system grids are shipped.  The browser (engine.js) inflates and decodes them and computes everything
 * else: blends of any systems, the swath (running minimum), minimum-field lows, colour classes, tooltips.
 */
(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const pad3 = (n) => String(n).padStart(3, '0');
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  // ---------------------------------------------------------------------------------------------
  // small helpers
  // ---------------------------------------------------------------------------------------------
  const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const fmtValid = (iso) => {
    const d = new Date(iso);
    return `${DOW[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${iso.slice(11, 16)} UTC`;
  };
  const fmtInit = (iso) => {
    const d = new Date(iso);
    return `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()} ${iso.slice(11, 13)}Z`;
  };

  const FONT = '"Liberation Sans", "DejaVu Sans", "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif';
  // Map colours per theme (the page chrome is themed in style.css).  Lines keep the same cartographic
  // hierarchy in both: country > state/province > coast > graticule.
  const THEMES = {
    dark: {
      land: '#1e242c', coast: '#b9c8da', coastOp: 0.8,
      st: '#a3afbd', stOp: 0.78, stCase: '#000000', stCaseOp: 0.5,
      a0: '#ffffff', a0Case: '#000000', a0CaseOp: 0.65, a0Sea: '#9aa7b6',
      lakeStroke: '#6ea6d9', lakeFill: '#000000', lakeFillOp: 0.18,
      grat: '#ffffff', gratOp: 0.22, gratText: '#d9e1ec', gratHalo: 'rgba(0,0,0,0.85)',
      low: '#ff3a3a', sys: { gefs: '#ff3a3a', ifs: '#ffd23f', geps: '#37e8cf', aigefs: '#d79bff' }, halo: 'rgba(0,0,0,0.92)', dataOp: 100,
    },
    light: {
      land: '#e8ebe6', coast: '#222a36', coastOp: 0.85,
      st: '#323a47', stOp: 0.8, stCase: '#ffffff', stCaseOp: 0.32,
      a0: '#141922', a0Case: '#ffffff', a0CaseOp: 0.55, a0Sea: '#3b4452',
      lakeStroke: '#3d5f7e', lakeFill: '#ffffff', lakeFillOp: 0.22,
      grat: '#141922', gratOp: 0.3, gratText: '#2a3342', gratHalo: 'rgba(255,255,255,0.85)',
      low: '#cb181d', sys: { gefs: '#cb181d', ifs: '#b85f00', geps: '#007a73', aigefs: '#7b2cbf' }, halo: 'rgba(255,255,255,0.96)', dataOp: 85,
    },
  };
  const readTheme = () => {
    let t = document.documentElement.getAttribute('data-theme');
    try { t = localStorage.getItem('gefs-theme') || t; } catch (e) { /* private mode */ }
    return THEMES[t] ? t : 'dark';
  };
  let themeName = readTheme();
  let th = THEMES[themeName];
  const state = { family: 'gefs', product: 'mean', idx: 0, playing: false, lows: [], lowsPx: [], grid: null, when: null, tipGrids: null, lowSize: 24 };
  const fmtCave = (iso) => {
    const d = new Date(iso);
    const mm = iso.slice(14, 16);                                    // '06Z' like the init time; minutes only if not on the hour
    return `${DOW[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()} ${iso.slice(11, 13)}${mm && mm !== '00' ? ':' + mm : ''}Z`;
  };

  // ---------------------------------------------------------------------------------------------
  // boot
  // ---------------------------------------------------------------------------------------------
  async function main() {
    let manifest;
    try {
      const r = await fetch('data/manifest.json', { cache: 'no-cache' });
      if (!r.ok) throw new Error(r.status);
      manifest = await r.json();
    } catch (e) {
      $('#runline').textContent = 'No forecast data published yet — the first build is still running.';
      setStatus('Could not load data/manifest.json', true);
      return;
    }
    const frames = manifest.frames;
    const run = manifest.run;
    const dom = manifest.domain;
    const bounds = L.latLngBounds([dom.south, dom.west], [dom.north, dom.east]);
    const E = window.MslpEngine;
    const fams = manifest.families;
    const isBlend = (k) => !!(fams[k] && fams[k].parts);
    const bases = (manifest.views || Object.keys(fams)).filter((k) => fams[k] && !fams[k].parts && frames.some((f) => f.avail[k]));   // single systems
    // Every combination of two or more systems is a blend (largest first); the browser computes them, so none are pre-built.
    const combos = (arr, n) => (n === 0 ? [[]] : arr.flatMap((x, i) => combos(arr.slice(i + 1), n - 1).map((c) => [x, ...c])));
    const weighting = (manifest.blend && manifest.blend.weighting) || 'equal';
    const blendViews = [];
    for (let n = bases.length; n >= 2; n--) {
      for (const c of combos(bases, n)) {
        if (!frames.some((f) => c.every((k) => f.avail[k]))) continue;
        const key = c.join('_');
        fams[key] = { label: 'Blend', name: `${c.map((k) => fams[k].label).join(' + ')} blend`, org: '',
          note: weighting === 'equal' ? 'Equal-weight mean of the system means (each system counts once)' : 'Mean of all members pooled (each member counts once)',
          members: c.flatMap((k) => fams[k].members), parts: c, weighting };
        blendViews.push(key);
      }
    }
    const availableViews = [...bases, ...blendViews];
    const viewHas = (f, k) => (isBlend(k) ? fams[k].parts.every((p) => f.avail[p]) : !!f.avail[k]);
    const availOf = (f, k) => (isBlend(k)
      ? { grid_deg: f.avail[fams[k].parts[0]].grid_deg, members: fams[k].parts.reduce((n, p) => n + f.avail[p].members, 0) }
      : f.avail[k]);
    const shortOf = (k) => (isBlend(k) ? 'Blend' : ({ gefs: 'GEFS', ifs: 'ECMWF', geps: 'GEPS', aigefs: 'AIGEFS' }[k] || fams[k].label));
    // Flat vector flag of the organisation that publishes each system (USA for NOAA's GEFS and AIGEFS, EU for ECMWF, Canada for GEPS).
    // Drawn as inline SVG rather than emoji, so they are flat (no waving) and look the same on every platform.
    const SVG_OPEN = '<svg viewBox="0 0 30 20" aria-hidden="true" focusable="false">';
    const FLAG_SVG = {
      us: SVG_OPEN + '<rect width="30" height="20" fill="#fff"/>' +
        [0, 2, 4, 6, 8, 10, 12].map((i) => `<rect y="${(i * 20 / 13).toFixed(3)}" width="30" height="${(20 / 13).toFixed(3)}" fill="#b22234"/>`).join('') +
        '<rect width="12.3" height="10.77" fill="#3c3b6e"/>' +
        [0, 1, 2, 3].flatMap((r) => [0, 1, 2, 3, 4].map((c) => `<circle cx="${(1.6 + c * 2.3 + (r % 2 ? 1.15 : 0)).toFixed(2)}" cy="${(1.7 + r * 2.4).toFixed(2)}" r=".7" fill="#fff"/>`)).join('') + '</svg>',
      eu: SVG_OPEN + '<rect width="30" height="20" fill="#039"/>' +
        Array.from({ length: 12 }, (_, i) => { const t = i * Math.PI / 6; return `<circle cx="${(15 + 6 * Math.sin(t)).toFixed(2)}" cy="${(10 - 6 * Math.cos(t)).toFixed(2)}" r="1.15" fill="#fc0"/>`; }).join('') + '</svg>',
      ca: SVG_OPEN + '<rect width="30" height="20" fill="#fff"/><rect width="7.5" height="20" fill="#d52b1e"/><rect x="22.5" width="7.5" height="20" fill="#d52b1e"/>' +
        '<polygon fill="#d52b1e" transform="translate(15 9.6) scale(.95)" points="0,-6 1.3,-3.5 2.6,-4 2.2,-1.2 5,-2.2 4.4,.2 5.6,1 2.6,2.6 3,3.8 .4,3.2 .4,6.4 -.4,6.4 -.4,3.2 -3,3.8 -2.6,2.6 -5.6,1 -4.4,.2 -5,-2.2 -2.2,-1.2 -2.6,-4 -1.3,-3.5"/></svg>',
    };
    const FLAGS = { gefs: ['us', 'United States'], aigefs: ['us', 'United States'], ifs: ['eu', 'European Union'], geps: ['ca', 'Canada'] };
    const flagOf = (k) => (FLAGS[k] ? `<span class="flag" role="img" aria-label="${FLAGS[k][1]} flag">${FLAG_SVG[FLAGS[k][0]]}</span>` : '');
    const keyOf = (k) => flagOf(k) || `<i style="--c:${sysColor(k)}"></i>`;   // legend key: flag (coloured square if a system has none)
    const curParts = () => (isBlend(state.family) ? fams[state.family].parts : null);
    const SYS_FALLBACK = ['#ff3a3a', '#ffd23f', '#37e8cf', '#c792ea'];
    const sysColor = (k) => (th.sys && th.sys[k]) || SYS_FALLBACK[Math.max(0, bases.indexOf(k))];
    // which system a member index belongs to inside the CURRENT blend's concatenated member list
    const partOfMember = (mi) => {
      const ps = curParts();
      let off = 0;
      for (const k of ps) { off += fams[k].members.length; if (mi < off) return k; }
      return ps[ps.length - 1];
    };
    state.family = bases[0] || availableViews[0];
    state.blendSel = blendViews.length ? fams[blendViews[0]].parts.slice() : [];   // systems currently ticked for the blend
    // Probability / strike options: pressure level (hPa) and strike radius (km)
    const probLevels = (manifest.prob && manifest.prob.levels) || [];
    const strikeLevels = probLevels.filter((v) => v <= (manifest.lows_threshold_hpa == null ? 1005 : manifest.lows_threshold_hpa));
    const radii = (manifest.strike && manifest.strike.radii_km) || [];
    state.level = { prob: probLevels.includes(1000) ? 1000 : probLevels[0], strike: strikeLevels.includes(1005) ? 1005 : strikeLevels[strikeLevels.length - 1] };
    state.radius = manifest.strike ? manifest.strike.default_km : 200;
    const hasProduct = (p) => !!manifest.products[p] && (p === 'prob' ? probLevels.length > 0 : p === 'strike' ? (strikeLevels.length > 0 && !!manifest.tracks) : true);

    const HASH_EVERY_MS = 750;                                       // see writeHash(); declared early because map events can reach it during set-up
    let hashTimer = 0, hashLast = 0;
    // frames available for the selected family (the ECMWF 06/18Z runs, e.g., stop at F144)
    let vframes = frames;
    const rebuildViewFrames = () => { vframes = frames.filter((f) => viewHas(f, state.family)); };
    rebuildViewFrames();

    $('#runline').textContent =
      `Init: ${fmtInit(run.init)} · ${bases.map((k) => `${shortOf(k)} ${fams[k].members.length}`).join(' + ')} members · ${gridSummary(frames, bases)}`;

    // ---- map ----------------------------------------------------------------------------------
    const map = L.map('map', {
      preferCanvas: true,          // every pane gets its own canvas renderer
      zoomSnap: 0.25, zoomDelta: 0.5, wheelPxPerZoomLevel: 110,
      maxZoom: 10,
      attributionControl: true,
      zoomControl: false,
    });
    map.attributionControl.setPrefix(false);
    map.attributionControl.addAttribution(
      'NOAA <a href="https://registry.opendata.aws/noaa-gefs/" target="_blank" rel="noopener">GEFS</a> &amp; <a href="https://registry.opendata.aws/noaa-nws-graphcastgfs-pds/" target="_blank" rel="noopener">AIGEFS</a> (AI, CC0) via AWS Open Data · ' +
      'IFS ENS: © <a href="https://www.ecmwf.int" target="_blank" rel="noopener">ECMWF</a>, <a href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noopener">CC BY 4.0</a> (processed) · ' +
      'GEPS: Data Source: <a href="https://eccc-msc.github.io/open-data/licence/readme_en/" target="_blank" rel="noopener">Environment and Climate Change Canada</a> · ' +
      'Basemap: <a href="https://www.naturalearthdata.com/" target="_blank" rel="noopener">Natural Earth</a> · ' +
      'Colours: <a href="https://colorbrewer2.org" target="_blank" rel="noopener">ColorBrewer</a> · ' +
      '<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
    L.control.zoom({ position: 'topright' }).addTo(map);
    L.control.scale({ position: 'bottomright', maxWidth: 130, imperial: true, metric: true }).addTo(map);

    const Home = L.Control.extend({
      onAdd() {
        const box = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
        const a = L.DomUtil.create('a', 'home-btn', box);
        a.href = '#'; a.title = 'Reset view'; a.setAttribute('role', 'button'); a.setAttribute('aria-label', 'Reset view');
        a.textContent = '⌂';
        L.DomEvent.on(a, 'click', (e) => { L.DomEvent.stop(e); map.fitBounds(initial); });
        L.DomEvent.disableClickPropagation(box);
        return box;
      },
    });
    new Home({ position: 'topright' }).addTo(map);

    // Panes give a deterministic cartographic stacking order (bottom -> top):
    //   land fill < DATA < lakes < graticule < coast < state casing < state lines < country casing < country lines
    const pane = (name, z) => { const p = map.createPane(name); p.style.zIndex = z; p.style.pointerEvents = 'none'; return p; };
    pane('land', 210); pane('data', 250); pane('lakes', 300); pane('grat', 310); pane('coast', 320);
    pane('admin1c', 335); pane('admin1', 340); pane('admin0c', 355); pane('admin0', 360);

    // The viewer is locked to the data domain: it cannot be panned or zoomed out past the data, so the
    // viewport is always filled.  It opens on the (smaller) initial view, e.g. CONUS.
    const initial = dom.initial
      ? L.latLngBounds([dom.initial[0], dom.initial[1]], [dom.initial[2], dom.initial[3]]) : bounds;
    const hash = parseHash();
    map.setMaxBounds(bounds);
    map.options.maxBoundsViscosity = 1.0;
    const lockZoom = () => map.setMinZoom(map.getBoundsZoom(bounds, true));
    map.setView(initial.getCenter(), 4);
    lockZoom();
    if (hash.v) map.setView([hash.v[0], hash.v[1]], hash.v[2]);
    else map.fitBounds(initial);

    // ---- basemap (vector, cartographic hierarchy; colours from the active theme) ---------------
    const lineLayers = [];
    const w = (base, z, min) => Math.max(min, base * Math.pow(1.2, z - 4));   // width grows ~20 % per zoom level
    const addLines = (data, paneName, styleFn) => {
      const l = L.geoJSON(data, { pane: paneName, interactive: false, smoothFactor: 0.7, style: (f) => styleFn(f, map.getZoom()) });
      l.addTo(map);
      lineLayers.push({ l, styleFn });
      return l;
    };
    const layers = {};
    const loadBase = async (n) => {                            // *.json.gz inflated here; plain .json still works (dev server)
      try {
        const r = await fetch(`data/basemap/${n}.json.gz`);
        if (r.ok) return JSON.parse(new TextDecoder().decode(await E.inflate(await r.arrayBuffer())));
      } catch (e) { /* fall through */ }
      return (await fetch(`data/basemap/${n}.json`)).json();
    };
    const [land, lakes, coast, admin0, admin1] = await Promise.all(['land', 'lakes', 'coast', 'admin0', 'admin1'].map(loadBase));

    const landLayer = L.geoJSON(land, { pane: 'land', interactive: false, smoothFactor: 0.8,
      style: () => ({ stroke: false, fillColor: th.land, fillOpacity: 1 }) }).addTo(map);
    const lakeStyle = () => ({ color: th.lakeStroke, weight: 0.8, opacity: 0.9, fillColor: th.lakeFill, fillOpacity: th.lakeFillOp });
    layers.lakes = L.geoJSON(lakes, { pane: 'lakes', interactive: false, smoothFactor: 0.8, style: lakeStyle }).addTo(map);   // data stays visible under water

    addLines(coast, 'coast', (f, z) => ({ color: th.coast, weight: w(0.8, z, 0.6), opacity: th.coastOp, lineJoin: 'round' }));

    // 1st-order divisions: thin, mid-tone, with a faint contrasting casing so they read on dark AND light fills
    layers.states = [
      addLines(admin1, 'admin1c', (f, z) => ({ color: th.stCase, weight: w(0.8, z, 0.8) + 1.4, opacity: th.stCaseOp, lineCap: 'round' })),
      addLines(admin1, 'admin1', (f, z) => ({ color: th.st, weight: w(0.75, z, 0.55), opacity: th.stOp, lineCap: 'round' })),
    ];
    // International boundaries: the heaviest line on the map (with a casing so it reads on any fill);
    // dashed where indefinite / disputed, fine dots at sea (treaty / median lines).
    const a0 = (f, z) => {
      const t = f.properties.t;
      return t === 0 ? { weight: w(1.5, z, 1.1), dashArray: null, color: th.a0, opacity: 1 }
        : t === 1 ? { weight: w(1.2, z, 1.0), dashArray: '6 4', color: th.a0, opacity: 1 }
        : { weight: w(0.9, z, 0.8), dashArray: '1 5', color: th.a0Sea, opacity: 0.85 };
    };
    addLines(admin0, 'admin0c', (f, z) => (f.properties.t === 2
      ? { opacity: 0, weight: 0 }
      : { color: th.a0Case, weight: a0(f, z).weight + 2.2, opacity: th.a0CaseOp, dashArray: f.properties.t === 1 ? '6 4' : null, lineCap: 'round' }));
    addLines(admin0, 'admin0', (f, z) => ({ ...a0(f, z), lineCap: 'butt', lineJoin: 'round' }));

    map.on('zoomend', () => {
      const z = map.getZoom();
      lineLayers.forEach(({ l, styleFn }) => l.setStyle((f) => styleFn(f, z)));
    });

    // ---- canvas overlays living above the map pane (graticule + low centres) -------------------
    const mkCanvas = (cls) => {
      const c = document.createElement('canvas');
      c.className = `overlay-cv ${cls}`;
      map.getContainer().appendChild(c);
      return c;
    };
    const gratCv = mkCanvas('grat');
    const tracksCv = mkCanvas('tracks');
    const lowsCv = mkCanvas('lows');
    const sizeCv = () => {
      const s = map.getSize(), dpr = window.devicePixelRatio || 1;
      [gratCv, tracksCv, lowsCv].forEach((c) => {
        c.width = Math.round(s.x * dpr); c.height = Math.round(s.y * dpr);
        c.style.width = `${s.x}px`; c.style.height = `${s.y}px`;
        c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
      });
    };
    sizeCv();

    const opts = { graticule: true, lows: true, states: true, lakes: true, byFamily: true, tracks: false };

    // Graticule LINES are vector layers in the 'grat' pane (below coast/state/country lines, as is standard);
    // only the degree labels are drawn on the overlay canvas.
    const gratStep = (z) => (z < 4.6 ? 10 : z < 6 ? 5 : z < 7.5 ? 2 : 1);
    let gratLayer = null, gratLayerStep = 0;
    function rebuildGraticule(force) {
      const step = gratStep(map.getZoom());
      if (!force && gratLayer && step === gratLayerStep && opts.graticule) return;
      if (gratLayer) { map.removeLayer(gratLayer); gratLayer = null; }
      if (!opts.graticule) return;
      gratLayerStep = step;
      const st = { pane: 'grat', interactive: false, color: th.grat, opacity: th.gratOp, weight: 0.7, dashArray: '2 4' };
      const lines = [];
      for (let lat = Math.ceil(dom.south / step) * step; lat <= dom.north; lat += step) lines.push(L.polyline([[lat, dom.west], [lat, dom.east]], st));
      for (let lon = Math.ceil(dom.west / step) * step; lon <= dom.east; lon += step) lines.push(L.polyline([[dom.south, lon], [dom.north, lon]], st));
      gratLayer = L.layerGroup(lines).addTo(map);
    }

    function drawGraticule() {
      const ctx = gratCv.getContext('2d');
      const s = map.getSize();
      ctx.clearRect(0, 0, s.x, s.y);
      if (!opts.graticule) return;
      const step = gratStep(map.getZoom());
      const b = map.getBounds();
      const lat0 = Math.ceil(b.getSouth() / step) * step, lon0 = Math.ceil(b.getWest() / step) * step;
      ctx.font = '600 12.5px ' + FONT;
      ctx.lineJoin = 'round';
      ctx.textAlign = 'left';
      const put = (t, x, y) => {
        ctx.lineWidth = 3; ctx.strokeStyle = th.gratHalo; ctx.strokeText(t, x, y);
        ctx.fillStyle = th.gratText; ctx.fillText(t, x, y);
      };
      for (let lat = lat0; lat <= b.getNorth(); lat += step) put(`${Math.abs(lat)}°${lat < 0 ? 'S' : 'N'}`, 5, map.latLngToContainerPoint([lat, b.getWest()]).y - 3);
      for (let lon = lon0; lon <= b.getEast(); lon += step) put(`${Math.abs(lon)}°${lon < 0 ? 'W' : 'E'}`, map.latLngToContainerPoint([b.getSouth(), lon]).x + 3, 14);
    }

    const byFamily = () => isBlend(state.family) && opts.byFamily;
    function drawLows() {
      const ctx = lowsCv.getContext('2d');
      const s = map.getSize();
      ctx.clearRect(0, 0, s.x, s.y);
      state.lowsPx = [];
      if (!opts.lows || !manifest.products[state.product].has_lows) return;
      const z = map.getZoom();
      const size = clamp(17 + (z - 4) * 2.2, 17, 32);           // "L" height in px (bold)
      const fs = Math.round(size * 0.74);                       // hPa label size
      state.lowSize = size;
      const pts = [];
      for (const [lon, lat, p, mi, th_] of state.lows) {
        const pt = map.latLngToContainerPoint([lat, lon]);
        if (pt.x < -40 || pt.y < -60 || pt.x > s.x + 40 || pt.y > s.y + 40) continue;
        pts.push({ x: pt.x, y: pt.y, p, mi, t: th_, col: byFamily() ? sysColor(partOfMember(mi)) : th.low });
      }
      state.lowsPx = pts.map((q) => [q.x, q.y, q.p, q.mi, q.t]);
      pts.sort((a, b) => b.p - a.p);                            // shallow first -> the deepest lows end up on top
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      ctx.font = `900 ${size}px ${FONT}`;
      // two passes (all halos, then all glyphs) so a tight cluster of members reads as one clean red group
      ctx.lineWidth = Math.max(3.5, size * 0.18); ctx.strokeStyle = th.halo;
      for (const q of pts) ctx.strokeText('L', q.x, q.y);
      for (const q of pts) { ctx.fillStyle = q.col; ctx.fillText('L', q.x, q.y); }
      // hPa value directly above each L.  Deepest lows are labelled first; a label that would overlap one
      // already placed is skipped (zoom in and more appear; the cursor read-out lists every low under it).
      ctx.font = `800 ${fs}px ${FONT}`;
      const placed = [];
      for (let i = pts.length - 1; i >= 0; i--) {
        const q = pts[i];
        const t = String(Math.round(q.p));
        const w = ctx.measureText(t).width + 4, h = fs + 2;
        const cx = q.x, cy = q.y - size * 0.5 - fs * 0.58;
        const r = [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2];
        if (placed.some((o) => r[0] < o[2] && r[2] > o[0] && r[1] < o[3] && r[3] > o[1])) continue;
        placed.push(r);
        ctx.lineWidth = Math.max(3.5, fs * 0.22); ctx.strokeStyle = th.halo; ctx.strokeText(t, cx, cy);
        ctx.fillStyle = q.col; ctx.fillText(t, cx, cy);
      }
    }


    // ---- member low tracks (canvas overlay below the L markers) -----------------------------------
    // data/<run>/<system>/tracks.json.gz: every member's low centres linked from hour to hour (pipeline: tracks.py).
    const trackData = new Map(), trackLoaded = new Map();            // system -> Promise / loaded {tracks, members}
    function tracksFor(sys) {
      if (!trackData.has(sys)) {
        const meta = manifest.tracks && manifest.tracks[sys];
        const p = meta
          ? fetch(`data/${meta.file}`).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.arrayBuffer(); })
            .then(E.inflate).then((b) => JSON.parse(new TextDecoder().decode(b)))
          : Promise.resolve({ tracks: [], members: [] });
        p.then((d) => trackLoaded.set(sys, d)).catch(() => trackData.delete(sys));
        trackData.set(sys, p);
      }
      return trackData.get(sys);
    }
    let trackPx = [], hoverTrack = null;                             // projected tracks of the last draw / the one under the pointer
    function drawTracks() {
      const ctx = tracksCv.getContext('2d');
      const s = map.getSize();
      ctx.clearRect(0, 0, s.x, s.y);
      trackPx = [];
      if (!opts.tracks) return;
      const f = vframes[state.idx];
      if (!f) return;
      const parts = isBlend(state.family) ? fams[state.family].parts : [state.family];
      let missing = false;
      for (const k of parts) {
        const d = trackLoaded.get(k);
        if (!d) { missing = true; tracksFor(k).then(() => drawTracks()).catch(() => {}); continue; }
        const col = byFamily() ? sysColor(k) : th.low;
        for (const t of d.tracks) {
          const xy = t.pts.map((p) => { const q = map.latLngToContainerPoint([p[2], p[1]]); return [q.x, q.y]; });
          trackPx.push({ sys: k, t, xy });
        }
        trackPx.forEach((q) => { if (q.sys === k) q.col = col; });
      }
      ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      const stroke = (q, from, to, w, col, alpha, dash) => {
        if (to - from < 1) return;
        ctx.beginPath();
        ctx.moveTo(q.xy[from][0], q.xy[from][1]);
        for (let i = from + 1; i <= to; i++) ctx.lineTo(q.xy[i][0], q.xy[i][1]);
        ctx.globalAlpha = alpha; ctx.lineWidth = w; ctx.strokeStyle = col; ctx.setLineDash(dash || []);
        ctx.stroke();
      };
      for (const q of trackPx) {                                     // future part: faint, dashed
        let last = -1;
        q.t.pts.forEach((p, i) => { if (p[0] <= f.fh) last = i; });
        q.last = last;
        stroke(q, Math.max(last, 0), q.t.pts.length - 1, 1.1, q.col, 0.32, [3, 4]);
      }
      for (const q of trackPx) {                                     // up to the hour shown: dark casing + solid line
        stroke(q, 0, q.last, 3.6, th.halo, 0.55);
        stroke(q, 0, q.last, 1.9, q.col, 0.95);
      }
      ctx.setLineDash([]);
      if (hoverTrack) {
        const q = trackPx.find((x) => x.t === hoverTrack);
        if (q) { stroke(q, 0, q.xy.length - 1, 6, th.halo, 0.8); stroke(q, 0, q.xy.length - 1, 3.2, q.col, 1); }
      }
      ctx.globalAlpha = 1;
    }
    // pointer proximity -> highlight the member track under the cursor
    function pickTrack(x, y) {
      let best = null, bd = 9;
      for (const q of trackPx) {
        for (let i = 1; i < q.xy.length; i++) {
          const [x0, y0] = q.xy[i - 1], [x1, y1] = q.xy[i];
          const dx = x1 - x0, dy = y1 - y0, L2 = dx * dx + dy * dy;
          const u = L2 ? Math.max(0, Math.min(1, ((x - x0) * dx + (y - y0) * dy) / L2)) : 0;
          const d = Math.hypot(x - (x0 + u * dx), y - (y0 + u * dy));
          if (d < bd) { bd = d; best = q; }
        }
      }
      return best;
    }
    function trackInfo(q) {
      const pts = q.t.pts, deep = pts.reduce((a, p) => (p[3] < a[3] ? p : a));
      return `${fams[q.sys].members[q.t.m]} · F${pad3(pts[0][0])}–F${pad3(pts[pts.length - 1][0])} · deepest ${deep[3].toFixed(1)} hPa at F${pad3(deep[0])}`;
    }

    let raf = 0;
    const redrawOverlays = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { drawGraticule(); drawTracks(); drawLows(); }); };
    map.on('move moveend zoomend viewreset', redrawOverlays);
    map.on('zoomend', rebuildGraticule);
    map.on('resize', () => { lockZoom(); sizeCv(); redrawOverlays(); });
    map.on('zoomstart', () => { gratCv.style.opacity = 0; lowsCv.style.opacity = 0; tracksCv.style.opacity = 0; });   // avoid mis-registered overlays during the zoom animation
    map.on('zoomend', () => { gratCv.style.opacity = 1; lowsCv.style.opacity = 1; tracksCv.style.opacity = 1; });
    map.on('moveend', () => {
      const c = map.getCenter();
      state.view = [+c.lat.toFixed(3), +c.lng.toFixed(3), +map.getZoom().toFixed(2)];
      writeHash();
    });

    // ---- cursor-following read-out (both products) ---------------------------------------------
    // Shows the interpolated MSLP under the pointer plus every low-centre marker beneath it.  It tracks the
    // cursor everywhere on the map and is also refreshed when the frame or product changes.
    const tip = $('#tip');
    const tipLabel = () => (state.product === 'mean'
      ? (isBlend(state.family) ? 'Blended ensemble mean' : 'Ensemble-mean MSLP')
      : state.product === 'prob'
        ? `Members with MSLP below ${state.level.prob} hPa`
        : state.product === 'strike'
          ? `Low below ${state.level.strike} hPa within ${state.radius} km, through F${pad3(vframes[state.idx].fh)}`
      : state.product === 'swath'
        ? (isBlend(state.family) ? 'Swath: lowest of all members so far' : 'Swath: lowest member so far')
        : (isBlend(state.family) ? 'Lowest of all members' : 'Lowest member MSLP'));
    // intensity band (hurricane category / storm / gale) whose pressure range contains v, if any
    const catOf = (v) => {
      const g = ((manifest.scale && manifest.scale.segments) || []).find((x) => x.phrase
        && (x.from_hpa == null || v >= x.from_hpa) && v < x.to_hpa);
      return g ? g.phrase : '';
    };
    const validOf = (fh) => { const fr = frames.find((x) => x.fh === fh); return fr ? fmtValid(fr.valid) : ''; };
    let lastPt = null;                                           // last pointer position {x, y, ll}
    const valueAt = (lat, lon, g) => E.valueAt(g || state.grid, lat, lon);   // g = a field {lon0, lat0, dlon, dlat, nx, ny, v hPa}
    const classColor = (v) => {
      const P = manifest.products[state.product];
      let k = 0;
      while (k < P.edges.length && v >= P.edges[k]) k++;
      return P.colors[k] === 'transparent' ? 'rgba(128,128,128,0.25)' : P.colors[k];
    };
    const isPct = () => manifest.products[state.product].units === '%';
    const fmtVal = (x) => (isPct() ? x.toFixed(0) : x.toFixed(1));
    const fmtLat = (v) => `${Math.abs(v).toFixed(2)}°${v < 0 ? 'S' : 'N'}`;
    const fmtLon = (v) => `${Math.abs(v).toFixed(2)}°${v < 0 ? 'W' : 'E'}`;
    const memberName = (mi) => fams[state.family].members[mi];
    const PTAG = { mean: 'MEAN', min: 'MIN', swath: 'SWATH', prob: 'P(<' + 'LEVEL)', strike: 'STRIKE' };
    const sb = { pos: $('#sbPos'), val: $('#sbVal'), low: $('#sbLow'), zoom: $('#sbZoom') };
    const updateZoomReadout = () => { sb.zoom.textContent = `ZOOM ${map.getZoom().toFixed(2)}`; };
    function updateTip() {
      if (!lastPt) {
        tip.hidden = true;
        sb.pos.textContent = 'LAT —  LON —'; sb.val.textContent = 'MSLP —'; sb.low.textContent = '';
        return;
      }
      const { x, y, ll } = lastPt;
      const v = valueAt(ll.lat, ll.lng);
      const blendVals = (isBlend(state.family) && state.tipGrids)
        ? curParts().filter((k) => state.tipGrids[k]).map((k) => [k, valueAt(ll.lat, ll.lng, state.tipGrids[k])]) : null;
      const reached = (state.product === 'swath' && state.when) ? E.nearestAt(state.grid, state.when, ll.lat, ll.lng) : null;
      const hit = Math.max(16, state.lowSize * 0.75);
      const near = state.lowsPx.filter(([px, py]) => Math.hypot(px - x, py - y - state.lowSize * 0.15) < hit).sort((a, b) => a[2] - b[2]);
      let html = '';
      if (v != null) {
        const pct = isPct();
        html += `<div class="tt-val"><i style="background:${classColor(v)}"></i><b>${fmtVal(v)}</b><span>${pct ? '%' : 'hPa'}</span></div>` +
                `<div class="tt-sub">${shortOf(state.family)} · ${tipLabel()}</div>` +
                (!pct && catOf(v) ? `<div class="tt-cat">\u2248 ${catOf(v)} pressure</div>` : '') +
                (pct && state.members && !(isBlend(state.family) && fams[state.family].weighting === 'equal') ? `<div class="tt-cat">\u2248 ${Math.round(v * state.members.total / 100)} of ${state.members.total} members</div>` : '');
        if (blendVals && blendVals.every(([, x]) => x != null)) {
          const xs = blendVals.map(([, x]) => x);
          const sgn = (d) => `${d >= 0 ? '+' : '\u2212'}${pct ? Math.abs(d).toFixed(0) : Math.abs(d).toFixed(1)}`;
          const diff = blendVals.length === 2
            ? [`${shortOf(blendVals[1][0])} \u2212 ${shortOf(blendVals[0][0])}`, sgn(xs[1] - xs[0])]
            : ['Spread (max \u2212 min)', fmtVal(Math.max(...xs) - Math.min(...xs))];
          html += '<div class="tt-fams">' +
            blendVals.map(([k, x]) => `<div class="tt-frow">${keyOf(k)}<span>${shortOf(k)}</span><b>${fmtVal(x)}${pct ? '%' : ''}</b></div>`).join('') +
            `<div class="tt-frow tt-d"><span></span><span>${diff[0]}</span><b>${diff[1]}</b></div></div>`;
        }
      } else {
        html += `<div class="tt-sub">No data here</div>`;
      }
      if (v != null && reached != null && reached >= 0) html += `<div class="tt-sub">Lowest reached at F${pad3(reached)} · ${validOf(reached)}</div>`;
      html += `<div class="tt-ll">${fmtLat(ll.lat)}  ${fmtLon(ll.lng)}</div>`;
      if (hoverTrack) { const q = trackPx.find((z) => z.t === hoverTrack); if (q) html += `<div class="tt-lows"><div class="tt-h">Member low track</div><div class="tt-row"><span style="color:${q.col}">${trackInfo(q)}</span></div></div>`; }
      if (near.length) {
        const who = state.product === 'min' ? 'Envelope low (lowest member)' : state.product === 'swath' ? 'Swath low (deepest so far)' : 'Member lows here';
        html += `<div class="tt-lows"><div class="tt-h">${who}</div>` +
          near.slice(0, 6).map(([, , p, mi, t]) => `<div class="tt-row"><b${byFamily() ? ` style="color:${sysColor(partOfMember(mi))}"` : ''}>L ${p.toFixed(1)}</b><span>${memberName(mi)}${t != null ? ` · F${pad3(t)}` : ''}</span></div>`).join('') +
          (near.length > 6 ? `<div class="tt-more">+${near.length - 6} more</div>` : '') + '</div>';
      }
      sb.pos.textContent = `LAT ${fmtLat(ll.lat)}  LON ${fmtLon(ll.lng)}`;
      sb.val.textContent = v != null
        ? `${shortOf(state.family).toUpperCase()} ${state.product === 'prob' ? `P(MSLP<${state.level.prob})` : PTAG[state.product]} ${fmtVal(v)} ${isPct() ? '%' : 'hPa'}` +
          (blendVals && blendVals.every(([, x]) => x != null) ? `  (${blendVals.map(([k, x]) => `${shortOf(k)} ${fmtVal(x)}`).join(' | ')})` : '')
        : 'MSLP —';
      sb.low.textContent = near.length ? `L ${near[0][2].toFixed(1)} hPa ${memberName(near[0][3])}${near.length > 1 ? ` (+${near.length - 1})` : ''}` : '';
      tip.innerHTML = html;
      tip.hidden = false;
      const sz = map.getSize(), tw = tip.offsetWidth, tipH = tip.offsetHeight;
      let left = x + 18, top = y + 18;
      if (left + tw > sz.x - 6) left = x - tw - 18;                // flip to the other side of the cursor near edges
      if (top + tipH > sz.y - 6) top = y - tipH - 18;
      tip.style.left = `${Math.max(6, left)}px`;
      tip.style.top = `${Math.max(6, top)}px`;
    }
    const onPointer = (e) => {
      lastPt = { x: e.containerPoint.x, y: e.containerPoint.y, ll: e.latlng };
      if (opts.tracks) {
        const q = pickTrack(e.containerPoint.x, e.containerPoint.y), t = q ? q.t : null;
        if (t !== hoverTrack) { hoverTrack = t; drawTracks(); }
      }
      updateTip();
    };
    map.on('mousemove', onPointer);
    map.on('click', onPointer);                                   // touch: tap to read a point
    map.on('mouseout', () => { lastPt = null; updateTip(); });
    map.on('zoomstart', () => { tip.hidden = true; });
    map.on('zoomend moveend', updateZoomReadout);
    updateZoomReadout();

    // ---- data layer ---------------------------------------------------------------------------
    // The per-system grids are fetched as .bin.gz, inflated and decoded here.  Blends, the swath, the lows of derived
    // minimum fields and the colour classes are all computed in the browser (engine.js).
    const gridCache = new Map();                                      // `${system}/${kind}/${fh}` -> Promise<decoded grid>
    function fetchGrid(sys, kind, fh) {
      const key = `${sys}/${kind}/${fh}`;
      if (gridCache.has(key)) return gridCache.get(key);
      const p = fetch(`data/${run.id}/${sys}/${kind}/f${pad3(fh)}.bin.gz`)
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.arrayBuffer(); })
        .then(E.inflate).then(E.decodeBin);
      gridCache.set(key, p);
      p.catch(() => gridCache.delete(key));
      if (gridCache.size > 360) gridCache.delete(gridCache.keys().next().value);   // ~0.3 MB each once decoded
      return p;
    }

    // swath = running minimum of each system's member-wise minimum over the forecast hours; one running state per system,
    // calls are serialised so playback folds in one hour at a time and a jump back restarts from the first hour.
    const swathRec = new Map(), swathChain = new Map();
    async function swathStep(sys, fh) {
      let rec = swathRec.get(sys);
      if (rec && rec.st.fh > fh) rec = null;
      const have = rec ? rec.st.fh : -1;
      const hours = frames.filter((f) => f.avail[sys] && f.fh <= fh && f.fh > have).map((f) => f.fh);
      const grids = await Promise.all(hours.map((h) => fetchGrid(sys, 'min', h)));
      let st = rec ? rec.st : null;
      for (const g of grids) st = st ? E.swathFold(st, g) : E.swathStart(g);
      swathRec.set(sys, { st });
      return E.swathCopy(st);
    }
    const swathAt = (sys, fh) => {
      const next = (swathChain.get(sys) || Promise.resolve()).catch(() => {}).then(() => swathStep(sys, fh));
      swathChain.set(sys, next);
      return next;
    };

    // strike probability: per system / level / radius, computed once from that system's member tracks (all hours at once)
    const strikeGeom = (() => {
      const d = 0.25;
      return { nx: Math.round((dom.east - dom.west) / d) + 1, ny: Math.round((dom.north - dom.south) / d) + 1, lon0: dom.west, lat0: dom.south, dlon: d, dlat: d };
    })();
    const strikeCache = new Map();
    function strikeFor(sys, level, radius) {
      const key = `${sys}/${level}/${radius}`;
      if (!strikeCache.has(key)) {
        const p = tracksFor(sys).then((d) => {
          const fhs = frames.filter((f) => f.avail[sys]).map((f) => f.fh);
          return { res: E.strikeCounts(d.tracks, strikeGeom, radius, level, fhs), fhs };
        });
        strikeCache.set(key, p);
        p.catch(() => strikeCache.delete(key));
        if (strikeCache.size > 12) strikeCache.delete(strikeCache.keys().next().value);
      }
      return strikeCache.get(key);
    }

    /** Everything needed to draw one view / product / hour: {field, lows, tips{system: field}, when?, members?} */
    async function buildResult(view, prod, fh, opt) {
      const parts = isBlend(view) ? fams[view].parts : [view];
      const off = []; let o = 0;
      for (const k of parts) { off.push(o); o += fams[k].members.length; }   // each system's member indices inside this view's member list
      const tips = {};
      if (prod === 'prob') {
        const gs = await Promise.all(parts.map((k) => fetchGrid(k, 'prob', fh)));
        const li = gs[0].levels.indexOf(opt.level);
        const fields = gs.map((g, i) => { const f = E.fieldOf(g, E.probPercent(g, li)); tips[parts[i]] = f; return f; });
        let field = fields[0];
        if (parts.length > 1) {
          const vs = fields.map((f, i) => (i ? E.regridLinear(f.v, E.regridPlan(f, fields[0])) : f.v));
          field = Object.assign(E.geomOf(fields[0]), { v: E.blendMean(vs, weighting === 'equal' ? parts.map(() => 1) : gs.map((g) => g.members)) });
        }
        return { field, lows: [], tips, members: { total: gs.reduce((n, g) => n + g.members, 0) } };
      }
      if (prod === 'strike') {
        const per = await Promise.all(parts.map((k) => strikeFor(k, opt.level, opt.radius)));
        const fields = per.map((st, i) => {
          const fi = st.fhs.indexOf(fh), nm = fams[parts[i]].members.length;
          const f = Object.assign({}, st.res.geom, { v: E.strikePercent(st.res, fi, nm) });
          tips[parts[i]] = f;
          return f;
        });
        const field = parts.length > 1
          ? Object.assign({}, fields[0], { v: E.blendMean(fields.map((f) => f.v), weighting === 'equal' ? parts.map(() => 1) : parts.map((k) => fams[k].members.length)) })
          : fields[0];
        return { field, lows: [], tips, members: { total: parts.reduce((n, k) => n + fams[k].members.length, 0) } };
      }
      if (prod === 'mean') {
        const gs = await Promise.all(parts.map((k) => fetchGrid(k, 'mean', fh)));
        const fields = gs.map((g, i) => { const f = E.fieldOf(g, E.toHpa(g.t)); tips[parts[i]] = f; return f; });
        let field = fields[0];
        if (parts.length > 1) {
          const vs = fields.map((f, i) => (i ? E.regridLinear(f.v, E.regridPlan(f, fields[0])) : f.v));
          field = Object.assign(E.geomOf(fields[0]), { v: E.blendMean(vs, weighting === 'equal' ? parts.map(() => 1) : gs.map((g) => g.members)) });
        }
        const lows = gs.flatMap((g, i) => (g.lows || []).map((l) => [l[0], l[1], l[2], l[3] + off[i]]));
        return { field, lows, tips };
      }
      // member-wise minimum ('min') or its running minimum over the forecast ('swath')
      const srcs = await Promise.all(parts.map(async (k) => {
        if (prod === 'swath') {
          const st = await swathAt(k, fh);
          return { geom: st.geom, v: E.toHpa(st.t), owner: st.owner, when: st.when };
        }
        const g = await fetchGrid(k, 'min', fh);
        return { geom: E.geomOf(g), v: E.toHpa(g.t), owner: g.owner };
      }));
      srcs.forEach((s_, i) => { tips[parts[i]] = Object.assign({}, s_.geom, { v: s_.v }); });
      const ref = srcs[0].geom;
      const aligned = srcs.map((s_, i) => {
        if (!i || E.sameGeom(s_.geom, ref)) return Object.assign({ offset: off[i] }, s_);
        const plan = E.regridPlan(s_.geom, ref);
        return { offset: off[i], v: E.regridLinear(s_.v, plan), owner: E.regridNearest(s_.owner, plan, 0),
          when: s_.when ? E.regridNearest(s_.when, plan, -1) : undefined };
      });
      const low = E.lowestOf(aligned);
      const field = Object.assign({}, ref, { v: low.v });
      const dets = E.detectLows(field, manifest.lows_min, dom);
      const lows = dets.map((c) => {
        const row = [+c.lon.toFixed(2), +c.lat.toFixed(2), +c.p.toFixed(1), low.owner[c.j * ref.nx + c.i]];
        if (prod === 'swath') row.push(low.when[c.j * ref.nx + c.i]);
        return row;
      });
      return { field, lows, tips, when: low.when };
    }
    // Finished views are kept for replays and loops: least-recently-used first out, limited by memory rather than a fixed count
    // (a 41-frame loop used to be evicted before it came round again). ~40 MB per GB of device RAM, between 80 and 320 MB.
    const resultCache = new Map(), resultBytes = new Map();
    const RESULT_BUDGET = Math.max(80, Math.min(320, (navigator.deviceMemory || 4) * 40)) * 1048576;
    const bytesOf = (R) => {
      const seen = new Set(); let n = 0;
      const add = (a) => { if (a && a.buffer && !seen.has(a.buffer)) { seen.add(a.buffer); n += a.byteLength; } };
      add(R.field.v); add(R.disp.v); add(R.when); Object.values(R.tips || {}).forEach((f) => add(f.v));
      return n;
    };
    function trimResults(keep) {
      let total = 0;
      resultBytes.forEach((v) => { total += v; });
      for (const k of resultCache.keys()) {
        if (total <= RESULT_BUDGET) break;
        if (k === keep || !resultBytes.has(k)) continue;           // never drop the newest or one still being computed
        total -= resultBytes.get(k); resultCache.delete(k); resultBytes.delete(k);
      }
    }
    const optOf = (prod) => ({ level: state.level[prod], radius: state.radius });     // options that change a product's values
    const optKey = (prod) => (prod === 'prob' ? `${state.level.prob}` : prod === 'strike' ? `${state.level.strike}/${state.radius}` : '');
    function computeView(view, prod, fh) {
      const key = `${view}/${prod}/${optKey(prod)}/${fh}`;
      if (resultCache.has(key)) { const hit = resultCache.get(key); resultCache.delete(key); resultCache.set(key, hit); return hit; }   // refresh its place
      const p = buildResult(view, prod, fh, optOf(prod)).then((R) => {
        // probabilities are smooth enough to be shown bilinearly; pressure fields get Catmull-Rom smoothing
        R.disp = (prod === 'prob' || prod === 'strike') ? R.field : E.upsample(R.field, R.field.dlon < 0.4 ? 2 : 4);
        resultBytes.set(key, bytesOf(R));
        trimResults(key);
        return R;
      });
      resultCache.set(key, p);
      p.catch(() => { resultCache.delete(key); resultBytes.delete(key); });
      return p;
    }

    // ---- raster layer: the field is classified into the colour bands pixel by pixel on a canvas in the 'data' pane -----
    const dataCv = document.createElement('canvas');
    dataCv.className = 'data-cv';
    map.getPane('data').appendChild(dataCv);
    let dispField = null, dispImg = null, dispBuf = null;
    const luts = {};
    const lutFor = (prod) => luts[prod] || (luts[prod] = E.makeLut(manifest.products[prod].edges, manifest.products[prod].colors));
    function drawData() {
      const s = map.getSize();
      if (dataCv.width !== s.x || dataCv.height !== s.y || !dispImg) {
        dataCv.width = s.x; dataCv.height = s.y;
        dispImg = new ImageData(s.x, s.y); dispBuf = new Uint32Array(dispImg.data.buffer);
      }
      L.DomUtil.setPosition(dataCv, map.containerPointToLayerPoint([0, 0]));
      const ctx = dataCv.getContext('2d');
      if (!dispField) { ctx.clearRect(0, 0, s.x, s.y); return; }
      const lons = new Float64Array(s.x), lats = new Float64Array(s.y);
      for (let x = 0; x < s.x; x++) lons[x] = map.containerPointToLatLng([x + 0.5, 0]).lng;
      for (let y = 0; y < s.y; y++) lats[y] = map.containerPointToLatLng([0, y + 0.5]).lat;
      E.rasterize(dispField, lons, lats, lutFor(state.product), dispBuf);
      ctx.putImageData(dispImg, 0, 0);
    }
    let rafData = 0;
    const scheduleData = () => { cancelAnimationFrame(rafData); rafData = requestAnimationFrame(drawData); };
    map.on('move moveend zoomend viewreset resize', scheduleData);

    let token = 0, showLoop = null, showAgain = false;
    // show() = "display whatever state now asks for". While one view is being computed, further calls (dragging the slider, key repeat)
    // only update the time read-out and set a flag: when the running one finishes the loop goes round once more for the LATEST request,
    // so frames that were skipped over are never computed. The returned promise resolves when the latest request is on screen.
    function show() {
      if (showLoop) { showAgain = true; if (vframes[state.idx]) updateTimeline(vframes[state.idx]); return showLoop; }
      showLoop = (async () => { try { do { showAgain = false; await showOnce(); } while (showAgain); } finally { showLoop = null; } })();
      return showLoop;
    }
    async function showOnce() {
      const my = ++token;
      const f = vframes[state.idx];
      const prod = state.product, fam = state.family;
      const slow = setTimeout(() => setStatus(prod === 'swath' ? `Accumulating the swath to F${pad3(f.fh)}…` : prod === 'strike' ? 'Computing strike probability…' : `Loading F${pad3(f.fh)}…`), 250);
      try {
        updateTimeline(f);
        const R = await computeView(fam, prod, f.fh);
        if (my !== token || showAgain) return;                      // superseded while computing: do not draw it
        dispField = R.disp;
        state.lows = R.lows;
        state.grid = R.field;
        state.when = R.when || null;
        state.members = R.members || null;
        state.tipGrids = isBlend(fam) ? R.tips : null;
        drawData();
        drawTracks();
        drawLows();
        updateTip();
        setStatus('');
        prefetch();
      } catch (e) {
        if (my === token) setStatus(`Could not load F${pad3(f.fh)} (${e.message})`, true);
      } finally {
        clearTimeout(slow);
      }
    }
    function prefetch() {
      const parts = isBlend(state.family) ? fams[state.family].parts : [state.family];
      for (let k = 1; k <= 2; k++) {
        const nf = vframes[(state.idx + k) % vframes.length];
        const kind = { mean: 'mean', prob: 'prob', strike: null }[state.product] === undefined ? 'min' : { mean: 'mean', prob: 'prob', strike: null }[state.product];
        if (kind) parts.forEach((p) => fetchGrid(p, kind, nf.fh).catch(() => {}));
      }
    }

    // ---- legends --------------------------------------------------------------------------------
    // (1) flat colour bar with numeric labels (bottom-left); (2) CAVE-style three-line header (top-left).
    function buildLegend() {
      const P = manifest.products[state.product];
      const n = P.colors.length;
      const e = P.edges;
      if (P.units === '%') { buildPctLegend(P); return; }
      const segs = (manifest.scale && manifest.scale.segments) || [];
      const barPx = Math.min(500, window.innerWidth - 20) - 26;
      const hinges = segs.map((g) => g.to_hpa).filter((v) => v != null);          // every intensity threshold, e.g. [925, 950, ..., 1005]
      const at = (v) => ((e.indexOf(v) + 1) / n) * 100;                            // legend position (%) of an edge
      // numbers: thresholds plus round values above them, skipping any that would touch the previous number
      const pxPerHpa = barPx / n / (e[1] - e[0]);
      const gap = 36 / pxPerHpa;
      const want = [];
      e.filter((v) => hinges.includes(v) || (v > hinges[hinges.length - 1] && v % 10 === 0))
        .forEach((v) => { if (!want.length || v - want[want.length - 1] >= gap) want.push(v); });
      let ticks = '';
      e.forEach((v, k) => {
        const hinge = hinges.includes(v);
        if (!hinge && !want.includes(v)) return;
        ticks += `<span class="${hinge ? 'hinge' : ''}${v === 1000 ? ' b' : ''}" style="left:${(((k + 1) / n) * 100).toFixed(2)}%">${want.includes(v) ? v : ''}</span>`;
      });
      // names of the intensity bands, centred under their span of classes (the first one starts at the left edge)
      let cats = '';
      segs.forEach((g, i) => {
        if (!g.label) return;
        const lo = g.from_hpa == null ? 0 : at(g.from_hpa), hi = g.to_hpa == null ? 100 : at(g.to_hpa);
        cats += `<span style="left:${i === 0 ? 0 : ((lo + hi) / 2).toFixed(2)}%"${i === 0 ? ' class="first"' : ''} title="${g.name}">${g.label}</span>`;
      });
      const sample = (col) => `<span class="lg-low"${col ? ` style="--c:${col}"` : ''}><i>987</i><b>L</b></span>`;
      const bySys = byFamily() ? curParts() : null;
      const lowsRow = P.has_lows
        ? `<div class="lg-lows">${bySys ? bySys.map((k) => `${sample(sysColor(k))}<span>${shortOf(k)}</span>`).join('') + '<span class="lg-sep">·</span>' : sample('')}<span>${
            state.product === 'swath' ? 'Deepest point of the swath' : state.product === 'min' ? 'Local minimum of the lowest-member field' : (isBlend(state.family) ? 'Low centre of any member' : 'Member low centre')} (hPa)</span></div>` : '';
      $('#legend').innerHTML =
        `<div class="lg-bar" role="img" aria-label="Colour scale from below ${e[0]} to above ${e[e.length - 1]} hPa">${P.colors.map((c) => `<i style="background:${c}"></i>`).join('')}</div>` +
        `<div class="lg-ticks">${ticks}</div>${cats ? `<div class="lg-cats" title="Typical pressure for each wind-intensity class (NOAA Dvorak wind-pressure table, rounded to 5 hPa) - a rough guide only">${cats}</div>` : ''}${lowsRow}`;
      updateCaveLegend(vframes[state.idx]);
    }
    // probability products: one class per step of the percentage scale, no pressure bands
    function buildPctLegend(P) {
      const e = P.edges, n = P.colors.length;
      const barPx = Math.min(500, window.innerWidth - 20) - 26;
      const every = barPx / n < 30 ? 2 : 1;
      const ticks = e.map((v, k) => (k % every ? '' : `<span style="left:${(((k + 1) / n) * 100).toFixed(2)}%">${v}</span>`)).join('');
      const what = state.product === 'prob'
        ? `Share of ${isBlend(state.family) ? 'the systems\u2019 members' : 'members'} with MSLP below ${state.level.prob} hPa${levelName(state.level.prob) ? ` (${levelName(state.level.prob)} pressure)` : ''}`
        : `Share of members with a low below ${state.level.strike} hPa within ${state.radius} km, through the hour shown`;
      $('#legend').innerHTML =
        `<div class="lg-bar" role="img" aria-label="Probability scale from below ${e[0]} % to above ${e[e.length - 1]} %">${P.colors.map((c) => `<i${c === 'transparent' ? ' class="none"' : ` style="background:${c}"`}></i>`).join('')}</div>` +
        `<div class="lg-ticks">${ticks}</div><div class="lg-note">${what} (%)</div>`;
      updateCaveLegend(vframes[state.idx]);
    }
    // Map header: a two-line title block in the standard model-graphic layout.
    //   line 1  <system> | <product title>                                   <units>
    //   line 2  Init: <run> · F<hh> · Valid: <time> · <n> members
    // Everything is sentence case; the product title says what the map shows, including the chosen level / radius.
    function productTitle() {
      const P = manifest.products[state.product];
      if (state.product === 'mean' && curParts()) {
        return `${fams[state.family].weighting === 'equal' ? 'Equal-weight' : 'Pooled-member'} ensemble mean MSLP with member low centres`;
      }
      if (state.product === 'prob') return `Probability of MSLP below ${state.level.prob} hPa`;
      if (state.product === 'strike') return `Strike probability: low below ${state.level.strike} hPa within ${state.radius} km`;
      return P.title;
    }
    function updateCaveLegend(f) {
      if (!f) return;
      const P = manifest.products[state.product];
      const F = fams[state.family];
      const ps = curParts();
      const sys = ps ? `${ps.map((k) => shortOf(k)).join(' + ')} blend` : F.label;
      const nm = ps ? ps.map((k) => fams[k].members.length).join(' + ') : F.members.length;
      const cumulative = state.product === 'swath' || state.product === 'strike';
      const when = cumulative
        ? `F${pad3(vframes[0].fh)}\u2013F${pad3(f.fh)} \u00b7 Valid through: ${fmtCave(f.valid)}`
        : `F${pad3(f.fh)} \u00b7 Valid: ${fmtCave(f.valid)}`;
      $('#caveLegend').innerHTML =
        `<div class="cv-top"><div class="cv-title"><span class="cv-sys">${sys}</span><span class="cv-sep">|</span><span class="cv-prod">${productTitle()}</span></div>` +
        `<span class="cv-units">${P.units === '%' ? '% of members' : 'hPa'}</span></div>` +
        `<div class="cv-sub"><span>Init: ${fmtInit(run.init)}</span> <span>\u00b7 ${when}</span> <span class="cv-m">\u00b7 ${nm} members</span></div>`;
    }

    // ---- timeline / transport ------------------------------------------------------------------
    const slider = $('#slider');
    function buildTicks() {
      slider.max = vframes.length - 1;
      $('#ticks').innerHTML = vframes.map((f, i) => (f.fh % 48 === 0 && f.fh > 0 && vframes.length > 1)
        ? `<span style="left:${(i / (vframes.length - 1)) * 100}%${i === vframes.length - 1 ? ';transform:translateX(-100%)' : ''}">+${f.fh / 24}d</span>` : '').join('');
    }
    buildTicks();

    function updateTimeline(f) {
      slider.value = state.idx;
      $('#vFh').textContent = `F${pad3(f.fh)}`;
      $('#vTime').textContent = `Valid ${fmtValid(f.valid)}`;
      updateCaveLegend(f);
      const a = availOf(f, state.family);
      const cover = vframes.length < frames.length ? ` · F${pad3(vframes[0].fh)}–F${pad3(vframes[vframes.length - 1].fh)} ONLY` : '';
      const ps = curParts();
      const resNote = [];
      if (ps) for (const k of ps) { const d = f.avail[k].grid_deg; if (d !== a.grid_deg) resNote.push(`${shortOf(k)} ${d}\u00b0\u2192${a.grid_deg}\u00b0`); }
      $('#vGrid').textContent = `${a.grid_deg}\u00b0 GRID \u00b7 ${a.members} MEMBERS` +
        (ps ? ` (${ps.map((k) => f.avail[k].members).join(' + ')})` : '') +
        (resNote.length ? ` \u00b7 REGRIDDED: ${resNote.join(', ')}` : '') + cover;
      writeHash();
    }
    function setIdx(i) { state.idx = clamp(i, 0, vframes.length - 1); show(); }

    let timer = 0;
    async function tick() {
      if (!state.playing) return;
      const next = (state.idx + 1) % vframes.length;
      try { await computeView(state.family, state.product, vframes[next].fh); } catch (e) { /* show() reports it */ }
      if (!state.playing) return;
      setIdx(next);
      timer = setTimeout(tick, +$('#speed').value);
    }
    function setPlaying(on) {
      state.playing = on;
      const b = $('#btnPlay');
      b.innerHTML = on ? '&#10074;&#10074;' : '&#9654;';
      b.setAttribute('aria-label', on ? 'Pause' : 'Play');
      clearTimeout(timer);
      if (on) tick();
    }
    $('#btnPlay').addEventListener('click', () => setPlaying(!state.playing));
    $('#btnPrev').addEventListener('click', () => { setPlaying(false); setIdx(state.idx - 1); });
    $('#btnNext').addEventListener('click', () => { setPlaying(false); setIdx(state.idx + 1); });
    slider.addEventListener('input', () => { setPlaying(false); setIdx(+slider.value); });
    window.addEventListener('keydown', (e) => {
      if (e.target.matches('input[type=text],select')) return;
      if (e.key === 'ArrowRight') { setPlaying(false); setIdx(state.idx + 1); }
      else if (e.key === 'ArrowLeft') { setPlaying(false); setIdx(state.idx - 1); }
      else if (e.key === ' ' && !e.target.matches('button')) { e.preventDefault(); setPlaying(!state.playing); }
      else if (e.key === '1') setProduct('mean');
      else if (e.key === '2') setProduct('min');
      else if (e.key === '3') setProduct('swath');
      else if (e.key === '4') setProduct('prob');
      else if (e.key === '5') setProduct('strike');
      else if (e.key === 'k' || e.key === 'K') setTracks(!opts.tracks);
      else if (e.key === 'g' || e.key === 'G') setFamily('gefs');
      else if (e.key === 'e' || e.key === 'E') setFamily('ifs');
      else if (e.key === 'c' || e.key === 'C') setFamily('geps');
      else if (e.key === 'a' || e.key === 'A') setFamily('aigefs');
      else if (e.key === 'b' || e.key === 'B') setFamily('blend');
      else if (e.key === 't' || e.key === 'T') applyTheme(themeName === 'dark' ? 'light' : 'dark', true);
      else if (e.key === 'Escape') closeMenus();
    });

    // ---- product switch + display options ------------------------------------------------------
    function syncProductUI() {
      document.querySelectorAll('[data-act=product]').forEach((b) => {
        const on = b.dataset.product === state.product;
        b.setAttribute(b.getAttribute('role') === 'tab' ? 'aria-selected' : 'aria-checked', String(on));
      });
    }
    // ---- ensemble family (GEFS / ECMWF IFS ENS / Blend) -------------------------------------------
    const FAM_KEY = { gefs: 'G', ifs: 'E', geps: 'C', aigefs: 'A', blend: 'B' };
    const seg = bases.map((k) => ({ k, label: fams[k].label, name: fams[k].name, note: fams[k].note, key: FAM_KEY[k] || '' }));
    if (blendViews.length) seg.push({ k: 'blend', label: 'Blend', name: 'Blend of selected systems', note: 'Tick the systems to blend', key: 'B' });
    $('#famSeg').innerHTML = seg.map((e) =>
      `<button type="button" role="tab" data-act="family" data-family="${e.k}" aria-selected="false" title="${e.name}${e.note ? ' \u2014 ' + e.note : ''} (${e.key})"><span class="seg-main">${e.label}</span><span class="seg-sub" data-sub="${e.k}"></span></button>`).join('');
    $('#famMenu').innerHTML = seg.map((e) =>
      `<button type="button" class="mi" role="menuitemradio" data-act="family" data-family="${e.k}" aria-checked="false"><span class="ck"></span>${e.name}<kbd>${e.key}</kbd></button>`).join('');
    $('#blendChips').innerHTML = bases.map((k) =>
      `<button type="button" class="chip" data-act="blendpart" data-part="${k}" aria-pressed="false" style="--c:${sysColor(k)}">${keyOf(k)}${shortOf(k)}</button>`).join('');
    const blendKeyFor = (sel) => bases.filter((k) => sel.includes(k)).join('_');
    function syncFamilyUI() {
      const inBlend = isBlend(state.family);
      document.querySelectorAll('[data-act=family]').forEach((b) => {
        const on = b.dataset.family === 'blend' ? inBlend : b.dataset.family === state.family;
        b.setAttribute(b.getAttribute('role') === 'tab' ? 'aria-selected' : 'aria-checked', String(on));
      });
      bases.forEach((k) => { const el = document.querySelector(`[data-sub="${k}"]`); if (el) el.textContent = `${fams[k].org.replace('/NCEP', '')} \u00b7 ${fams[k].members.length}`; });
      const bs = document.querySelector('[data-sub="blend"]');
      if (bs) {
        const sel = inBlend ? fams[state.family].parts : state.blendSel;
        bs.textContent = `${sel.length} systems \u00b7 ${sel.reduce((n, k) => n + fams[k].members.length, 0)}`;
      }
      $('#blendGroup').hidden = !inBlend;
      $('#optFamRow').hidden = !inBlend;
      document.querySelectorAll('[data-act=blendpart]').forEach((b) => {
        const k = b.dataset.part, sel = state.blendSel, on = sel.includes(k);
        const next = on ? sel.filter((x) => x !== k) : bases.filter((x) => sel.includes(x) || x === k);
        b.setAttribute('aria-pressed', String(on));
        b.disabled = !(next.length >= 2 && availableViews.includes(blendKeyFor(next)));
        b.title = b.disabled ? (on ? 'A blend needs at least two systems' : 'This combination is not available') : (on ? `Remove ${fams[k].label} from the blend` : `Add ${fams[k].label} to the blend`);
      });
    }
    // 'blend' = the combination currently ticked; otherwise a concrete view key (a base system or e.g. gefs_ifs)
    const resolveView = (k) => (k === 'blend' ? (availableViews.includes(blendKeyFor(state.blendSel)) ? blendKeyFor(state.blendSel) : blendViews[0]) : k);
    function setFamily(k) {
      const v = resolveView(k);
      if (!v || !availableViews.includes(v) || v === state.family) return;
      const wantFh = vframes[state.idx] ? vframes[state.idx].fh : 0;
      state.family = v;
      if (isBlend(v)) state.blendSel = fams[v].parts.slice();
      rebuildViewFrames();
      // keep the same forecast hour; if this system does not cover it, show the closest earlier one
      let j = 0;
      vframes.forEach((f, i) => { if (f.fh <= wantFh) j = i; });
      state.idx = j;
      syncFamilyUI();
      buildTicks();
      buildLegend();
      state.lows = []; state.grid = null; state.when = null; state.tipGrids = null; drawLows(); updateTip();
      if (vframes[j].fh !== wantFh) {
        setStatus(`${shortOf(v)} has no F${pad3(wantFh)} on this cycle \u2014 showing F${pad3(vframes[j].fh)}`);
        setTimeout(() => setStatus(''), 3500);
      }
      show();
    }
    function toggleBlendPart(k) {
      const sel = state.blendSel;
      const next = sel.includes(k) ? sel.filter((x) => x !== k) : bases.filter((x) => sel.includes(x) || x === k);
      const key = blendKeyFor(next);
      if (next.length < 2 || !availableViews.includes(key)) return;
      state.blendSel = next;
      setFamily(key);
    }


    // ---- animated GIF export ----------------------------------------------------------------------
    // The picture is composed from what is on screen: every map canvas (in pane order, with the pane's opacity), then the
    // title block and the colour legend, which are copied from their HTML (position, colours, fonts), so the GIF always
    // matches the current theme and product. Frames are stepped through with the normal show() path.
    const gifdlg = $('#gifdlg');
    const gif = { busy: false, cancel: false };
    const gifEls = { from: $('#gifFrom'), to: $('#gifTo'), size: $('#gifSize'), prog: $('#gifProg'), msg: $('#gifMsg'), go: $('#gifGo'), close: $('#gifCancel'), what: $('#gifWhat') };
    const nextPaint = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    const mapPx = () => { const r = map.getContainer().getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
    function gifSizes() {
      const m = mapPx(), base = Math.min(1, 1000 / m.w);
      return [1, 0.75, 0.5, 0.35].map((k) => { const sc = base * k; return { sc, w: Math.max(2, Math.round(m.w * sc)), h: Math.max(2, Math.round(m.h * sc)) }; });
    }
    function openGif() {
      if (gif.busy) { gifdlg.showModal && !gifdlg.open && gifdlg.showModal(); return; }
      setPlaying(false);
      const opt = vframes.map((f, i) => `<option value="${i}">F${pad3(f.fh)} · ${fmtCave(f.valid)}</option>`).join('');
      gifEls.from.innerHTML = opt; gifEls.to.innerHTML = opt;
      gifEls.from.value = 0; gifEls.to.value = vframes.length - 1;
      const sizes = gifSizes();
      gifEls.size.innerHTML = sizes.map((z, i) => `<option value="${i}">${z.w} × ${z.h} px</option>`).join('');
      gifEls.size.value = Math.max(0, sizes.findIndex((z) => z.w <= 800));
      const P = manifest.products[state.product];
      gifEls.what.textContent = `${shortOf(state.family)} · ${P.short || P.title}${state.product === 'prob' ? ` (below ${state.level.prob} hPa)` : state.product === 'strike' ? ` (below ${state.level.strike} hPa, ${state.radius} km)` : ''} · the map as currently shown (view, layers, theme and opacity).`;
      gifEls.msg.textContent = ''; gifEls.prog.hidden = true; gifEls.go.disabled = false;
      if (!gifdlg.open) gifdlg.showModal();
    }
    // draw text/boxes of an HTML subtree onto ctx (coordinates relative to the map container)
    function paintDom(ctx, root, origin) {
      const rr = root.getBoundingClientRect();
      const box = (el) => { const r = el.getBoundingClientRect(); return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height }; };
      const walk = (el, top) => {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return;
        const b = box(el);
        if (b.w <= 0 || b.h <= 0) return;
        const bg = cs.backgroundColor;
        if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') { ctx.fillStyle = bg; ctx.fillRect(b.x, b.y, b.w, b.h); }
        else if (el.classList.contains('none') && cs.backgroundImage !== 'none') {        // hatched "no colour" class
          ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--line') || '#888';
          ctx.save(); ctx.beginPath(); ctx.rect(b.x, b.y, b.w, b.h); ctx.clip();
          for (let x = -b.h; x < b.w; x += 4) { ctx.beginPath(); ctx.moveTo(b.x + x, b.y + b.h); ctx.lineTo(b.x + x + b.h, b.y); ctx.stroke(); }
          ctx.restore();
        }
        const bw = parseFloat(cs.borderTopWidth);
        if (bw > 0 && cs.borderTopStyle !== 'none') { ctx.strokeStyle = cs.borderTopColor; ctx.lineWidth = bw; ctx.strokeRect(b.x + bw / 2, b.y + bw / 2, b.w - bw, b.h - bw); }
        if (el.matches('.lg-ticks > span')) {                                               // tick marks (CSS pseudo-elements are not readable)
          const hinge = el.classList.contains('hinge'), cx = b.x + b.w / 2;
          ctx.fillStyle = hinge ? cs.color : 'rgba(128,128,128,0.9)';
          const th_ = hinge ? 19 : 4, w_ = hinge ? 1.5 : 1;
          ctx.fillRect(cx - w_ / 2, b.y - th_, w_, th_);
        }
        for (const n of el.childNodes) {
          if (n.nodeType === 3 && n.textContent.trim()) {
            ctx.font = cs.font; ctx.fillStyle = cs.color; ctx.textBaseline = 'middle';
            const range = document.createRange(); range.selectNodeContents(n);
            const t = range.getBoundingClientRect();
            ctx.textAlign = 'left';
            ctx.fillText(n.textContent.trim(), t.left - origin.left, t.top - origin.top + t.height / 2 + 0.5);
          }
        }
        for (const c of el.children) walk(c, false);
      };
      walk(root, true);
    }
    function composeFrame(out, sc, withText) {
      const ctx = out.getContext('2d', { willReadFrequently: true });
      const mc = map.getContainer(), origin = mc.getBoundingClientRect();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.globalAlpha = 1;
      ctx.fillStyle = getComputedStyle(mc).backgroundColor; ctx.fillRect(0, 0, out.width, out.height);
      ctx.setTransform(sc, 0, 0, sc, 0, 0);
      ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
      const layers = [];
      mc.querySelectorAll('canvas').forEach((cv) => {
        const pn = cv.closest('.leaflet-pane:not(.leaflet-map-pane)');
        const z = pn ? +getComputedStyle(pn).zIndex || 0 : +getComputedStyle(cv).zIndex || 0;
        let op = parseFloat(getComputedStyle(cv).opacity);
        if (pn) op *= parseFloat(getComputedStyle(pn).opacity);
        if (cv.width && cv.height && op > 0 && getComputedStyle(cv).display !== 'none') layers.push({ cv, z, op, i: layers.length });
      });
      layers.sort((a, b) => a.z - b.z || a.i - b.i);
      for (const L_ of layers) {
        const r = L_.cv.getBoundingClientRect();
        ctx.globalAlpha = L_.op;
        ctx.drawImage(L_.cv, r.left - origin.left, r.top - origin.top, r.width, r.height);
      }
      ctx.globalAlpha = 1;
      if (withText) { paintDom(ctx, $('#caveLegend'), origin); paintDom(ctx, $('#legend'), origin); }
      return ctx.getImageData(0, 0, out.width, out.height);
    }
    async function runGif() {
      if (gif.busy) return;
      let a = +gifEls.from.value, b = +gifEls.to.value;
      if (a > b) [a, b] = [b, a];
      const z = gifSizes()[+gifEls.size.value] || gifSizes()[0];
      const delay = +$('#gifSpeed').value, hold = +$('#gifHold').value, withText = $('#gifLegend').checked;
      const startIdx = state.idx, wasTracks = hoverTrack;
      gif.busy = true; gif.cancel = false;
      gifEls.go.disabled = true; gifEls.prog.hidden = false; gifEls.prog.max = b - a + 1; gifEls.prog.value = 0;
      for (const el of [gifEls.from, gifEls.to, gifEls.size]) el.disabled = true;
      const out = document.createElement('canvas');
      out.width = z.w; out.height = z.h;
      const enc = MslpGif.createGif(z.w, z.h, { loop: 0 });
      hoverTrack = null; tip.hidden = true;
      try {
        for (let i = a; i <= b; i++) {
          if (gif.cancel) throw new Error('cancelled');
          gifEls.msg.textContent = `Rendering frame ${i - a + 1} of ${b - a + 1} (F${pad3(vframes[i].fh)})…`;
          state.idx = i;
          await show();
          await nextPaint();
          const img = composeFrame(out, z.sc, withText);
          enc.addFrame(img.data, delay + (i === b ? hold : 0));
          gifEls.prog.value = i - a + 1;
        }
        gifEls.msg.textContent = 'Optimising colours and compressing…';
        await new Promise((r) => setTimeout(r, 30));
        const bytes = enc.finish();
        const blob = new Blob([bytes], { type: 'image/gif' });
        const nm = ['ensemble-mslp', state.family, state.product, state.product === 'prob' || state.product === 'strike' ? state.level[state.product] : '', run.id].filter((x) => x !== '').join('_').replace(/[^\w.-]+/g, '-') + '.gif';
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a'); link.href = url; link.download = nm; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        const mb = bytes.length / 1048576;
        gifEls.msg.textContent = `Done: ${b - a + 1} frames, ${z.w} × ${z.h} px, ${mb >= 1 ? mb.toFixed(1) + ' MB' : Math.round(bytes.length / 1024) + ' kB'} — saved as ${nm}`;
        window.gifLast = { frames: b - a + 1, width: z.w, height: z.h, bytes: bytes.length, name: nm };
      } catch (e) {
        gifEls.msg.textContent = e.message === 'cancelled' ? 'Cancelled.' : `Could not create the GIF: ${e.message}`;
      } finally {
        gif.busy = false;
        for (const el of [gifEls.from, gifEls.to, gifEls.size]) el.disabled = false;
        gifEls.go.disabled = false; gifEls.prog.hidden = true;
        state.idx = startIdx; hoverTrack = wasTracks; show();
      }
    }
    gifEls.go.addEventListener('click', runGif);
    gifEls.close.addEventListener('click', () => { if (gif.busy) gif.cancel = true; else gifdlg.close(); });
    gifdlg.addEventListener('cancel', (e) => { if (gif.busy) { e.preventDefault(); gif.cancel = true; } });

    // phones: the blend / level bars sit behind an "Options" toggle (collapsed by default) so the map stays visible
    const stackToggle = $('#stackToggle'), stackbars = $('#stackbars');
    const narrow = window.matchMedia('(max-width: 720px)');
    const setStackOpen = (open) => { stackbars.classList.toggle('collapsed', !open); stackToggle.setAttribute('aria-expanded', String(open)); stackToggle.innerHTML = open ? 'Options &#9652;' : 'Options &#9662;'; };
    stackToggle.hidden = false;
    setStackOpen(!narrow.matches);
    stackToggle.addEventListener('click', () => setStackOpen(stackbars.classList.contains('collapsed')));
    narrow.addEventListener('change', (e) => setStackOpen(!e.matches));

    // ---- probability / strike options (pressure level, radius) -----------------------------------
    // a level's name = the intensity band whose upper edge it is ("1000" -> Storm+, "975" -> Cat 2+, "925" -> Cat 5)
    function levelName(lv) {
      const g = ((manifest.scale && manifest.scale.segments) || []).find((x) => x.label && x.to_hpa === lv);
      return g ? `${g.label}${g.label === 'Cat 5' ? '' : '+'}` : '';
    }
    function syncOptBar() {
      const prod = state.product, isP = prod === 'prob', isS = prod === 'strike';
      $('#optGroup').hidden = !(isP || isS);
      if (!(isP || isS)) return;
      const lvls = isP ? probLevels : strikeLevels, cur = state.level[prod];
      $('#lvlLabel').textContent = isP ? 'MSLP below' : 'Low deeper than';
      $('#optLevels').innerHTML = lvls.map((lv) =>
        `<button type="button" class="chip plain" data-act="level" data-level="${lv}" aria-pressed="${lv === cur}" title="${lv}${levelName(lv) ? ` hPa: ${levelName(lv)} pressure` : ' hPa'}">${lv}${levelName(lv) ? ` <small>${levelName(lv)}</small>` : ''}</button>`).join('');
      $('#radLabel').hidden = !isS; $('#optRadii').hidden = !isS;
      $('#optRadii').innerHTML = isS ? radii.map((r) => `<button type="button" class="chip plain" data-act="radius" data-radius="${r}" aria-pressed="${r === state.radius}">${r} km</button>`).join('') : '';
    }
    function setLevel(lv) {
      const prod = state.product;
      if (!(prod === 'prob' || prod === 'strike') || state.level[prod] === lv) return;
      state.level[prod] = lv;
      syncOptBar(); buildLegend(); writeHash(); show();
    }
    function setRadius(r) {
      if (state.radius === r) return;
      state.radius = r;
      syncOptBar(); buildLegend(); writeHash(); show();
    }
    function setProduct(p) {
      if (!hasProduct(p) || p === state.product) return;
      state.product = p;
      syncProductUI();
      syncOptBar();
      buildLegend();
      state.lows = []; state.grid = null; state.when = null; state.tipGrids = null; state.members = null; drawLows(); updateTip();
      writeHash();
      show();
    }
    function setTracks(on) {
      opts.tracks = on; $('#optTracks').checked = on;
      hoverTrack = null; drawTracks(); updateTip(); writeHash();
    }

    let opacityTouched = false;                                   // until the user moves the slider, each theme uses its own default
    const setDataOpacity = (v) => { $('#opacity').value = v; map.getPane('data').style.opacity = v / 100; };
    $('#opacity').addEventListener('input', (e) => { opacityTouched = true; setDataOpacity(e.target.value); });
    setDataOpacity($('#opacity').value);
    $('#optLows').addEventListener('change', (e) => { opts.lows = e.target.checked; drawLows(); });
    $('#optTracks').addEventListener('change', (e) => setTracks(e.target.checked));
    $('#optFam').addEventListener('change', (e) => { opts.byFamily = e.target.checked; buildLegend(); drawLows(); updateTip(); });
    $('#optGrat').addEventListener('change', (e) => { opts.graticule = e.target.checked; rebuildGraticule(); drawGraticule(); });
    $('#optStates').addEventListener('change', (e) => layers.states.forEach((l) => e.target.checked ? l.addTo(map) : map.removeLayer(l)));
    $('#optLakes').addEventListener('change', (e) => e.target.checked ? layers.lakes.addTo(map) : map.removeLayer(layers.lakes));

    // ---- theme (dark / light) -------------------------------------------------------------------
    function applyTheme(name, persist) {
      if (!THEMES[name]) return;
      themeName = name; th = THEMES[name];
      document.documentElement.setAttribute('data-theme', name);
      if (persist) { try { localStorage.setItem('gefs-theme', name); } catch (e) { /* ignore */ } }
      if (!opacityTouched) setDataOpacity(th.dataOp);
      landLayer.setStyle({ fillColor: th.land });
      layers.lakes.setStyle(lakeStyle());
      const z = map.getZoom();
      lineLayers.forEach(({ l, styleFn }) => l.setStyle((f) => styleFn(f, z)));
      rebuildGraticule(true); drawGraticule(); drawLows();
      document.querySelectorAll('[data-act=theme]').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.theme === name)));
      $('#themeLabel').textContent = name === 'dark' ? 'Dark' : 'Light';
    }

    // ---- menu bar + toolbar actions -------------------------------------------------------------
    const menubar = $('.menubar');
    const openMenu = (m) => { m.classList.add('open'); m.querySelector('.mb').setAttribute('aria-expanded', 'true'); };
    function closeMenus() {
      menubar.querySelectorAll('.menu.open').forEach((m) => { m.classList.remove('open'); m.querySelector('.mb').setAttribute('aria-expanded', 'false'); });
    }
    menubar.querySelectorAll('.menu').forEach((m) => {
      const b = m.querySelector('.mb');
      b.addEventListener('click', (e) => { e.stopPropagation(); const was = m.classList.contains('open'); closeMenus(); if (!was) openMenu(m); });
      b.addEventListener('mouseenter', () => { if (menubar.querySelector('.menu.open') && !m.classList.contains('open')) { closeMenus(); openMenu(m); } });
    });
    document.addEventListener('click', (e) => { if (!e.target.closest('.dd')) closeMenus(); });   // check-boxes keep the menu open

    async function copyLink() {
      writeHashNow();                                              // make sure the copied link has the current view
      try { await navigator.clipboard.writeText(location.href); setStatus('Link copied to clipboard'); }
      catch (e) { setStatus('Copy failed — copy the address bar instead', true); }
      setTimeout(() => setStatus(''), 1800);
    }
    document.addEventListener('click', (e) => {
      const it = e.target.closest('[data-act]');
      if (!it) return;
      switch (it.dataset.act) {
        case 'product': setProduct(it.dataset.product); break;
        case 'level': setLevel(+it.dataset.level); break;
        case 'radius': setRadius(+it.dataset.radius); break;
        case 'gif': openGif(); break;
        case 'family': setFamily(it.dataset.family); break;
        case 'blendpart': toggleBlendPart(it.dataset.part); break;
        case 'theme': applyTheme(it.dataset.theme, true); break;
        case 'toggletheme': applyTheme(themeName === 'dark' ? 'light' : 'dark', true); break;
        case 'reset': map.fitBounds(initial); break;
        case 'fullscreen':
          if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.();
          break;
        case 'copylink': copyLink(); break;
        case 'reload': location.reload(); break;
        case 'help': $('#help').showModal(); break;
        default: break;
      }
      closeMenus();
    });

    // ---- shareable URL state: #p=mean&f=132&v=lat,lon,zoom --------------------------------------
    // Called on every frame / pan, so it is throttled (at most one history write per HASH_EVERY_MS): Safari and every iOS browser throw
    // a SecurityError after 100 history.replaceState calls in 30 s, which used to abort the redraw mid-way. A failed write is ignored.
    function writeHashNow() {
      clearTimeout(hashTimer); hashTimer = 0; hashLast = performance.now();
      const f = vframes[state.idx];
      const hp = [`e=${state.family}`, `p=${state.product}`, `f=${f ? f.fh : 0}`];
      if (state.product === 'prob' || state.product === 'strike') hp.push(`l=${state.level[state.product]}`);
      if (state.product === 'strike') hp.push(`r=${state.radius}`);
      if (opts.tracks) hp.push('k=1');
      if (state.view) hp.push(`v=${state.view.join(',')}`);
      try { history.replaceState(null, '', `#${hp.join('&')}`); } catch (e) { /* rate-limited or unavailable: the URL just lags behind */ }
    }
    function writeHash() {
      if (hashTimer) return;                                       // a write is already scheduled; it will read the latest state
      const wait = HASH_EVERY_MS - (performance.now() - hashLast);
      if (wait <= 0) writeHashNow(); else hashTimer = setTimeout(writeHashNow, wait);
    }

    // ---- go ------------------------------------------------------------------------------------
    if (hash.p && hasProduct(hash.p)) state.product = hash.p;
    if (hash.l != null) { if (probLevels.includes(hash.l)) state.level.prob = hash.l; if (strikeLevels.includes(hash.l)) state.level.strike = hash.l; }
    if (hash.r != null && radii.includes(hash.r)) state.radius = hash.r;
    if (hash.k) { opts.tracks = true; $('#optTracks').checked = true; }
    const hv = hash.e === 'blend' ? blendViews[0] : hash.e;                  // '#e=blend' (old links) = blend of everything
    if (hv && availableViews.includes(hv)) {
      state.family = hv;
      if (isBlend(hv)) state.blendSel = fams[hv].parts.slice();
      rebuildViewFrames(); buildTicks();
    }
    syncProductUI();
    syncOptBar();
    syncFamilyUI();
    applyTheme(themeName, false);
    if (hash.f != null) {
      const i = vframes.findIndex((f) => f.fh === hash.f);
      state.idx = i >= 0 ? i : 0;
    }
    buildLegend();
    rebuildGraticule();
    drawGraticule();
    show();
    window.addEventListener('resize', () => map.invalidateSize());
    window.gefsViewer = { map, state, manifest };   // handy for debugging / automated tests
  }

  // ---------------------------------------------------------------------------------------------
  function gridSummary(frames, bases) {
    const deg = (f) => { const k = bases.find((x) => f.avail[x]); return f.avail[k].grid_deg; };
    const have = frames.filter((f) => bases.some((x) => f.avail[x]));
    const g = [...new Set(have.map(deg))].sort((a, b) => a - b);
    const lo = (r) => have.filter((f) => deg(f) === r).map((f) => f.fh);
    return g.map((r) => `${r}\u00b0 grid, F${pad3(Math.min(...lo(r)))}\u2013F${pad3(Math.max(...lo(r)))}`).join(' \u00b7 ');
  }
  function parseHash() {
    const h = {};
    location.hash.replace(/^#/, '').split('&').forEach((kv) => {
      const [k, v] = kv.split('=');
      if (k === 'p') h.p = v;
      else if (k === 'e') h.e = v;
      else if (k === 'l' && v !== undefined && !isNaN(+v)) h.l = +v;
      else if (k === 'r' && v !== undefined && !isNaN(+v)) h.r = +v;
      else if (k === 'k') h.k = v === '1';
      else if (k === 'f' && v !== undefined && !isNaN(+v)) h.f = +v;
      else if (k === 'v' && v) { const a = v.split(',').map(Number); if (a.length === 3 && a.every(Number.isFinite)) h.v = a; }
    });
    return h;
  }
  function setStatus(msg, err) {
    const s = $('#status');
    s.hidden = !msg;
    s.textContent = msg || '';
    s.classList.toggle('err', !!err);
  }

  main();
})();
