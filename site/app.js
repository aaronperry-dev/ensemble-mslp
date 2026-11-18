/* GEFS ensemble MSLP viewer — Leaflet, no build step, no external runtime dependencies.
 *
 * Data contract (written by gefs_pipeline):
 *   data/manifest.json                         run info, products (class edges + ColorBrewer colours), frames
 *   data/<run>/<product>/fNNN.json             GeoJSON FeatureCollection of filled contour bands
 *                                              ("mean" also carries `lows`: [[lon, lat, hPa, memberIdx], ...])
 *   data/basemap/{land,lakes,coast,admin0,admin1}.json   Natural Earth vectors
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

  const PRODUCT_TEXT = {
    mean: 'Filled: ensemble-mean MSLP. Each red “L” is one member’s low-pressure centre, labelled with its central pressure.',
    min: 'At each point, the lowest MSLP forecast by any member (the deepest-low envelope). “L” marks each local minimum of that envelope.',
  };
  const FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
  const LOW_RED = '#cb181d';          // ColorBrewer Reds (7): bold red "L"
  const state = { product: 'mean', idx: 0, playing: false, lows: [], lowsPx: [], grid: null, lowSize: 24 };

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
    const nMembers = manifest.members.length;

    $('#runline').textContent =
      `GEFS ${fmtInit(run.init)} · ${nMembers} members · ${gridSummary(frames)}`;

    // ---- map ----------------------------------------------------------------------------------
    const map = L.map('map', {
      preferCanvas: true,          // every pane gets its own canvas renderer
      zoomSnap: 0.25, zoomDelta: 0.5, wheelPxPerZoomLevel: 110,
      maxZoom: 10,
      attributionControl: true,
      zoomControl: true,
    });
    map.attributionControl.setPrefix(false);
    map.attributionControl.addAttribution(
      'GEFS: NOAA/NCEP via <a href="https://registry.opendata.aws/noaa-gefs/" target="_blank" rel="noopener">AWS Open Data</a> · ' +
      'Basemap: <a href="https://www.naturalearthdata.com/" target="_blank" rel="noopener">Natural Earth</a> · ' +
      'Colours: <a href="https://colorbrewer2.org" target="_blank" rel="noopener">ColorBrewer</a> · ' +
      '<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
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
    new Home({ position: 'topleft' }).addTo(map);

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

    // ---- basemap (vector, cartographic hierarchy) ----------------------------------------------
    const lineLayers = [];
    const w = (base, z, min) => Math.max(min, base * Math.pow(1.2, z - 4));   // width grows ~20 % per zoom level
    const addLines = (data, paneName, styleFn) => {
      const l = L.geoJSON(data, { pane: paneName, interactive: false, smoothFactor: 0.7, style: (f) => styleFn(f, map.getZoom()) });
      l.addTo(map);
      lineLayers.push({ l, styleFn });
      return l;
    };
    const layers = {};
    const loadBase = (n) => fetch(`data/basemap/${n}.json`).then((r) => r.json());
    const [land, lakes, coast, admin0, admin1] = await Promise.all(['land', 'lakes', 'coast', 'admin0', 'admin1'].map(loadBase));

    L.geoJSON(land, { pane: 'land', interactive: false, smoothFactor: 0.8,
      style: { stroke: false, fillColor: '#e8ebe6', fillOpacity: 1 } }).addTo(map);

    layers.lakes = L.geoJSON(lakes, { pane: 'lakes', interactive: false, smoothFactor: 0.8,
      style: { color: '#3d5f7e', weight: 0.8, opacity: 0.9, fillColor: '#ffffff', fillOpacity: 0.22 } }).addTo(map);   // data stays visible under water

    addLines(coast, 'coast', (f, z) => ({ color: '#222a36', weight: w(0.8, z, 0.6), opacity: 0.85, lineJoin: 'round' }));

    // 1st-order divisions: thin, mid-grey, with a faint light casing so they read on dark AND light fills
    layers.states = [
      addLines(admin1, 'admin1c', (f, z) => ({ color: '#ffffff', weight: w(0.8, z, 0.8) + 1.4, opacity: 0.32, lineCap: 'round' })),
      addLines(admin1, 'admin1', (f, z) => ({ color: '#323a47', weight: w(0.75, z, 0.55), opacity: 0.8, lineCap: 'round' })),
    ];
    // International boundaries: the heaviest line on the map (with a light casing so it reads on any fill);
    // dashed where indefinite / disputed, fine grey dots at sea (treaty / median lines).
    const a0 = (f, z) => {
      const t = f.properties.t;
      return t === 0 ? { weight: w(1.5, z, 1.1), dashArray: null, color: '#141922', opacity: 1 }
        : t === 1 ? { weight: w(1.2, z, 1.0), dashArray: '6 4', color: '#141922', opacity: 1 }
        : { weight: w(0.9, z, 0.8), dashArray: '1 5', color: '#3b4452', opacity: 0.85 };
    };
    addLines(admin0, 'admin0c', (f, z) => (f.properties.t === 2
      ? { opacity: 0, weight: 0 }
      : { color: '#ffffff', weight: a0(f, z).weight + 2.2, opacity: 0.55, dashArray: f.properties.t === 1 ? '6 4' : null, lineCap: 'round' }));
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
    const lowsCv = mkCanvas('lows');
    const sizeCv = () => {
      const s = map.getSize(), dpr = window.devicePixelRatio || 1;
      [gratCv, lowsCv].forEach((c) => {
        c.width = Math.round(s.x * dpr); c.height = Math.round(s.y * dpr);
        c.style.width = `${s.x}px`; c.style.height = `${s.y}px`;
        c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
      });
    };
    sizeCv();

    const opts = { graticule: true, lows: true, states: true, lakes: true };

    // Graticule LINES are vector layers in the 'grat' pane (below coast/state/country lines, as is standard);
    // only the degree labels are drawn on the overlay canvas.
    const gratStep = (z) => (z < 4.6 ? 10 : z < 6 ? 5 : z < 7.5 ? 2 : 1);
    let gratLayer = null, gratLayerStep = 0;
    function rebuildGraticule() {
      const step = gratStep(map.getZoom());
      if (gratLayer && step === gratLayerStep && opts.graticule) return;
      if (gratLayer) { map.removeLayer(gratLayer); gratLayer = null; }
      if (!opts.graticule) return;
      gratLayerStep = step;
      const st = { pane: 'grat', interactive: false, color: '#141922', opacity: 0.3, weight: 0.7, dashArray: '2 4' };
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
        ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(255,255,255,0.85)'; ctx.strokeText(t, x, y);
        ctx.fillStyle = '#2a3342'; ctx.fillText(t, x, y);
      };
      for (let lat = lat0; lat <= b.getNorth(); lat += step) put(`${Math.abs(lat)}°${lat < 0 ? 'S' : 'N'}`, 5, map.latLngToContainerPoint([lat, b.getWest()]).y - 3);
      for (let lon = lon0; lon <= b.getEast(); lon += step) put(`${Math.abs(lon)}°${lon < 0 ? 'W' : 'E'}`, map.latLngToContainerPoint([b.getSouth(), lon]).x + 3, 14);
    }

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
      for (const [lon, lat, p, mi] of state.lows) {
        const pt = map.latLngToContainerPoint([lat, lon]);
        if (pt.x < -40 || pt.y < -60 || pt.x > s.x + 40 || pt.y > s.y + 40) continue;
        pts.push({ x: pt.x, y: pt.y, p, mi });
      }
      state.lowsPx = pts.map((q) => [q.x, q.y, q.p, q.mi]);
      pts.sort((a, b) => b.p - a.p);                            // shallow first -> the deepest lows end up on top
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.lineJoin = 'round';
      ctx.font = `900 ${size}px ${FONT}`;
      // two passes (all halos, then all glyphs) so a tight cluster of members reads as one clean red group
      ctx.lineWidth = Math.max(3.5, size * 0.18); ctx.strokeStyle = 'rgba(255,255,255,0.96)';
      for (const q of pts) ctx.strokeText('L', q.x, q.y);
      ctx.fillStyle = LOW_RED;
      for (const q of pts) ctx.fillText('L', q.x, q.y);
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
        ctx.lineWidth = Math.max(3.5, fs * 0.22); ctx.strokeStyle = 'rgba(255,255,255,0.97)'; ctx.strokeText(t, cx, cy);
        ctx.fillStyle = LOW_RED; ctx.fillText(t, cx, cy);
      }
    }

    let raf = 0;
    const redrawOverlays = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { drawGraticule(); drawLows(); }); };
    map.on('move moveend zoomend viewreset', redrawOverlays);
    map.on('zoomend', rebuildGraticule);
    map.on('resize', () => { lockZoom(); sizeCv(); redrawOverlays(); });
    map.on('zoomstart', () => { gratCv.style.opacity = 0; lowsCv.style.opacity = 0; });   // avoid mis-registered overlays during the zoom animation
    map.on('zoomend', () => { gratCv.style.opacity = 1; lowsCv.style.opacity = 1; });
    map.on('moveend', () => {
      const c = map.getCenter();
      state.view = [+c.lat.toFixed(3), +c.lng.toFixed(3), +map.getZoom().toFixed(2)];
      writeHash();
    });

    // ---- cursor-following read-out (both products) ---------------------------------------------
    // Shows the interpolated MSLP under the pointer plus every low-centre marker beneath it.  It tracks the
    // cursor everywhere on the map and is also refreshed when the frame or product changes.
    const tip = $('#tip');
    const TIP_LABEL = { mean: 'Ensemble-mean MSLP', min: 'Lowest member MSLP' };
    let lastPt = null;                                           // last pointer position {x, y, ll}
    function valueAt(lat, lon) {
      const g = state.grid;
      if (!g) return null;
      const fx = (lon - g.lon0) / g.d, fy = (lat - g.lat0) / g.d;
      if (fx < 0 || fy < 0 || fx > g.nx - 1 || fy > g.ny - 1) return null;
      const x0 = Math.min(Math.floor(fx), g.nx - 2), y0 = Math.min(Math.floor(fy), g.ny - 2);
      const tx = fx - x0, ty = fy - y0;
      const v = (x, y) => g.v[y * g.nx + x];
      const a = v(x0, y0), b = v(x0 + 1, y0), c = v(x0, y0 + 1), d = v(x0 + 1, y0 + 1);
      if (a == null || b == null || c == null || d == null) return null;
      return ((a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty) / 10;
    }
    const classColor = (v) => {
      const P = manifest.products[state.product];
      let k = 0;
      while (k < P.edges.length && v >= P.edges[k]) k++;
      return P.colors[k];
    };
    const fmtLat = (v) => `${Math.abs(v).toFixed(2)}°${v < 0 ? 'S' : 'N'}`;
    const fmtLon = (v) => `${Math.abs(v).toFixed(2)}°${v < 0 ? 'W' : 'E'}`;
    function updateTip() {
      if (!lastPt) { tip.hidden = true; return; }
      const { x, y, ll } = lastPt;
      const v = valueAt(ll.lat, ll.lng);
      const hit = Math.max(16, state.lowSize * 0.75);
      const near = state.lowsPx.filter(([px, py]) => Math.hypot(px - x, py - y - state.lowSize * 0.15) < hit).sort((a, b) => a[2] - b[2]);
      let html = '';
      if (v != null) {
        html += `<div class="tt-val"><i style="background:${classColor(v)}"></i><b>${v.toFixed(1)}</b><span>hPa</span></div>` +
                `<div class="tt-sub">${TIP_LABEL[state.product]}</div>`;
      } else {
        html += `<div class="tt-sub">No data here</div>`;
      }
      html += `<div class="tt-ll">${fmtLat(ll.lat)}  ${fmtLon(ll.lng)}</div>`;
      if (near.length) {
        const who = state.product === 'min' ? 'Envelope low (lowest member)' : 'Member lows here';
        html += `<div class="tt-lows"><div class="tt-h">${who}</div>` +
          near.slice(0, 6).map(([, , p, mi]) => `<div class="tt-row"><b>L ${p.toFixed(1)}</b><span>${manifest.members[mi]}</span></div>`).join('') +
          (near.length > 6 ? `<div class="tt-more">+${near.length - 6} more</div>` : '') + '</div>';
      }
      tip.innerHTML = html;
      tip.hidden = false;
      const sz = map.getSize(), tw = tip.offsetWidth, th = tip.offsetHeight;
      let left = x + 18, top = y + 18;
      if (left + tw > sz.x - 6) left = x - tw - 18;                // flip to the other side of the cursor near edges
      if (top + th > sz.y - 6) top = y - th - 18;
      tip.style.left = `${Math.max(6, left)}px`;
      tip.style.top = `${Math.max(6, top)}px`;
    }
    const onPointer = (e) => { lastPt = { x: e.containerPoint.x, y: e.containerPoint.y, ll: e.latlng }; updateTip(); };
    map.on('mousemove', onPointer);
    map.on('click', onPointer);                                   // touch: tap to read a point
    map.on('mouseout', () => { lastPt = null; tip.hidden = true; });
    map.on('zoomstart', () => { tip.hidden = true; });

    // ---- data layer ---------------------------------------------------------------------------
    const cache = new Map();
    const frameUrl = (prod, fh) => `data/${run.id}/${prod}/f${pad3(fh)}.json`;
    function loadFrame(prod, fh) {
      const key = `${prod}/${fh}`;
      if (cache.has(key)) return cache.get(key);
      const p = fetch(frameUrl(prod, fh)).then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
      cache.set(key, p);
      p.catch(() => cache.delete(key));
      if (cache.size > 80) cache.delete(cache.keys().next().value);
      return p;
    }

    let dataLayer = null, token = 0;
    async function show() {
      const my = ++token;
      const f = frames[state.idx];
      const prod = state.product;
      const P = manifest.products[prod];
      updateTimeline(f);
      const slow = setTimeout(() => setStatus(`Loading F${pad3(f.fh)}…`), 250);
      try {
        const d = await loadFrame(prod, f.fh);
        if (my !== token) return;
        const colors = P.colors;
        const layer = L.geoJSON(d, {
          pane: 'data', interactive: false, smoothFactor: 0.6,
          style: (ft) => { const c = colors[ft.properties.i]; return { fillColor: c, fillOpacity: 1, color: c, weight: 0.8, opacity: 1, lineJoin: 'round' }; },
        });
        layer.addTo(map);
        if (dataLayer) map.removeLayer(dataLayer);
        dataLayer = layer;
        state.lows = d.lows || [];
        state.grid = d.grid || null;
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
      for (let k = 1; k <= 2; k++) {
        const nf = frames[(state.idx + k) % frames.length];
        loadFrame(state.product, nf.fh).catch(() => {});
      }
    }

    // ---- legend --------------------------------------------------------------------------------
    function buildLegend() {
      const P = manifest.products[state.product];
      const n = P.colors.length;
      const e = P.edges;
      const segs = (manifest.scale && manifest.scale.segments) || [];
      const hinges = segs.map((g) => g.to_hpa).filter((v) => v != null);          // e.g. [980, 1000]
      const barPx = Math.min(480, window.innerWidth - 20) - 28;
      let want = e.filter((v) => v % 8 === 0);
      if (want.length * 37 > barPx) want = e.filter((v) => v % 16 === 8);        // keeps 1000 on narrow screens
      let ticks = '';
      e.forEach((v, k) => {
        const hinge = hinges.includes(v);
        const lab = want.includes(v);
        if (!hinge && !lab) return;
        ticks += `<span class="${hinge ? 'hinge' : ''}${lab && v === 1000 ? ' b' : ''}" style="left:${(((k + 1) / n) * 100).toFixed(2)}%">${lab ? v : ''}</span>`;
      });
      const lowsRow = P.has_lows
        ? `<div class="lg-lows"><span class="lg-low"><i>987</i><b>L</b></span><span>${state.product === 'min' ? 'Local minimum of the member-minimum field' : 'Member low centre'} (hPa)</span></div>` : '';
      $('#legend').innerHTML =
        `<div class="lg-title"><span>${P.title}</span><small>hPa</small></div>` +
        `<div class="lg-bar" role="img" aria-label="Colour scale from below ${e[0]} to above ${e[e.length - 1]} hPa">${P.colors.map((c) => `<i style="background:${c}"></i>`).join('')}</div>` +
        `<div class="lg-ticks">${ticks}</div>${lowsRow}`;
    }

    // ---- timeline / transport ------------------------------------------------------------------
    const slider = $('#slider');
    slider.max = frames.length - 1;
    $('#ticks').innerHTML = frames.map((f, i) => (f.fh % 48 === 0 && f.fh > 0)
      ? `<span style="left:${(i / (frames.length - 1)) * 100}%">+${f.fh / 24}d</span>` : '').join('');

    function updateTimeline(f) {
      slider.value = state.idx;
      $('#vFh').textContent = `F${pad3(f.fh)}`;
      $('#vTime').textContent = `Valid ${fmtValid(f.valid)}`;
      $('#vGrid').textContent = `${f.grid_deg}° grid · ${f.members} members`;
      writeHash();
    }
    function setIdx(i) { state.idx = clamp(i, 0, frames.length - 1); show(); }

    let timer = 0;
    async function tick() {
      if (!state.playing) return;
      const next = (state.idx + 1) % frames.length;
      try { await loadFrame(state.product, frames[next].fh); } catch (e) { /* show() reports it */ }
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
    });

    // ---- product switch + display options ------------------------------------------------------
    function setProduct(p) {
      if (!manifest.products[p] || p === state.product) return;
      state.product = p;
      document.querySelectorAll('.seg button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.product === p)));
      buildLegend();
      state.lows = []; state.grid = null; drawLows(); updateTip();
      show();
    }
    document.querySelectorAll('.seg button').forEach((b) => b.addEventListener('click', () => setProduct(b.dataset.product)));

    $('#opacity').addEventListener('input', (e) => { map.getPane('data').style.opacity = e.target.value / 100; });
    map.getPane('data').style.opacity = $('#opacity').value / 100;
    $('#optLows').addEventListener('change', (e) => { opts.lows = e.target.checked; drawLows(); });
    $('#optGrat').addEventListener('change', (e) => { opts.graticule = e.target.checked; rebuildGraticule(); drawGraticule(); });
    $('#optStates').addEventListener('change', (e) => layers.states.forEach((l) => e.target.checked ? l.addTo(map) : map.removeLayer(l)));
    $('#optLakes').addEventListener('change', (e) => e.target.checked ? layers.lakes.addTo(map) : map.removeLayer(layers.lakes));
    if (window.matchMedia('(max-width:720px)').matches) $('#opts').open = false;

    // ---- shareable URL state: #p=mean&f=132&v=lat,lon,zoom --------------------------------------
    function writeHash() {
      const f = frames[state.idx];
      const parts = [`p=${state.product}`, `f=${f ? f.fh : 0}`];
      if (state.view) parts.push(`v=${state.view.join(',')}`);
      history.replaceState(null, '', `#${parts.join('&')}`);
    }

    // ---- go ------------------------------------------------------------------------------------
    if (hash.p && manifest.products[hash.p]) {
      state.product = hash.p;
      document.querySelectorAll('.seg button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.product === hash.p)));
    }
    if (hash.f != null) {
      const i = frames.findIndex((f) => f.fh === hash.f);
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
  function gridSummary(frames) {
    const g = [...new Set(frames.map((f) => f.grid_deg))].sort((a, b) => a - b);
    const lo = (r) => frames.filter((f) => f.grid_deg === r).map((f) => f.fh);
    return g.map((r) => `${r}° f${pad3(Math.min(...lo(r)))}–f${pad3(Math.max(...lo(r)))}`).join(' · ');
  }
  function parseHash() {
    const h = {};
    location.hash.replace(/^#/, '').split('&').forEach((kv) => {
      const [k, v] = kv.split('=');
      if (k === 'p') h.p = v;
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
