# Ensemble MSLP Viewer — NOAA GEFS · ECMWF IFS ENS · Blend

A static, GitHub-Pages-hosted web map of **ensemble mean sea-level pressure (MSLP)** from two ensemble families — **NOAA GEFS (31 members)** and **ECMWF IFS ENS (51 members)** — viewable **per family or as a blend of both**, rebuilt automatically every model cycle by a GitHub Action.

| | |
|---|---|
| **Ensemble families** | **GEFS** (31), **ECMWF IFS ENS** (51), and **Blend** (82 members, both systems). Switch with the *Ensemble* toolbar buttons / menu, or keys **G**, **E**, **B**. Every product below exists for each of the three. |
| **Product 1 – Ensemble mean + member lows** | Filled contours of the ensemble-mean MSLP, with an **“L” at every low-pressure centre (< 1005 hPa) of every ensemble member** (GEFS `gec00` + `gep01`…`gep30`; ECMWF `ec00` + `ep01`…`ep50`). Each L is labelled with its central pressure in hPa. |
| **Product 2 – Member-wise minimum** | Filled contours of the *lowest* MSLP forecast by any member at each grid point (the “deepest-low envelope”), with an **L + hPa value at each local minimum** of that envelope. |
| **Viewer** | Pan / zoom (mouse, touch, keyboard), ensemble-family and product switches, forecast-hour slider + animation, opacity, lat/lon grid, state/province and lake toggles, scale bar, shareable URL (`#e=blend&p=min&f=132&v=lat,lon,zoom`). |
| **Data** | NOAA GEFS on AWS Open Data (`s3://noaa-gefs-pds`) and ECMWF open data on AWS (`s3://ecmwf-forecasts`); both public, no credentials. |

```
.
├── config.toml                  # every tunable: domain, hours, rate limits, lows, palettes, contour classes
├── requirements.txt / -dev.txt
├── gefs_pipeline/               # the backend (python -m gefs_pipeline ...)
│   ├── fetcher.py               #   polite HTTP client: rate limit, backoff+jitter, Range requests, .idx parsing
│   ├── gefs.py                  #   GEFS bucket layout, 0.25°→0.5° fallback
│   ├── sources.py               #   ensemble families (GEFS, ECMWF IFS ENS): members, schedule, byte-range fetch, cycle readiness
│   ├── decode.py                #   in-memory GRIB2 decode (ecCodes) + domain subset
│   ├── products.py              #   mean / min, low detection, filled-contour GeoJSON
│   ├── palette.py               #   ColorBrewer ramps, OKLab interpolation
│   ├── pipeline.py / cli.py     #   orchestration + command line
│   └── basemap.py               #   Natural Earth → clipped, simplified basemap layers
├── site/                        # the viewer (plain HTML/CSS/JS + vendored Leaflet, no build step)
│   └── data/basemap/*.json      #   committed basemap vectors
├── tests/                       # pytest (mock S3 server with Range + 503s, GRIB round-trip, contours, palettes)
└── .github/workflows/build-and-deploy.yml
```

---

## 1. Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q                                     # 26 tests, no network needed

# quick smoke test: 6 members, first 24 h (≈ 65 requests, ≈ 16 MB)
python -m gefs_pipeline build --out dist --max-fh 24 --members 6        # --families gefs  (or ifs) to build one system only

# full production build: GEFS 31 + ECMWF 51 members, f000-f240 = 10 days (≈ 6 300 requests, ≈ 1.7 GB downloaded, ~10-15 min; site ≈ 150 MB)
python -m gefs_pipeline build --out dist

python -m http.server 8000 --directory dist             # open http://localhost:8000
```

Useful flags: `--cycle 2026111806` (pin a run), `--max-fh`, `--members N`, `--rps 4` (slow down), `--workers`, `--no-cache`, `-v`.
`python -m gefs_pipeline resolve-cycle` just prints the newest complete cycle.

## 2. What the pipeline does

1. **Pick the cycle.** Starting at the current time, step back through the cycles in `[run] cycles` (default **00Z and 12Z** – the ECMWF runs that reach day 15) until one is *complete for every family* (GEFS: last forecast hour exists for all 31 members; ECMWF: last step of the ensemble and control streams is published), so a half-published run is never shown and both systems always share the same initialisation time. Add 06/18Z to `cycles` for a GEFS-led site; ECMWF then ends at F144 for those cycles (the viewer shows exactly the hours each system has).
2. **Pick the grid per forecast hour – highest resolution first.**
   `pgrb2sp25` (**0.25°**) is probed first; if the object does not exist, `pgrb2ap5` (0.5°) is used.
   Verified against the bucket: **0.25° exists for f000–f240, exactly the 10-day window this project is configured for (`[forecast] stop = 240`)**, so every frame is 0.25°. The 0.5° product is only a safety net if a 0.25° object is missing; it can also serve longer runs (raise `stop` up to 384: f246+ exist only at 0.5° and are cubic-spline-upsampled for contouring). The viewer labels every frame with its grid.
3. **Download only the MSLP bytes (HTTP Range).**
   For each member × hour the client reads the tiny `.idx` inventory, finds the `PRMSL:mean sea level` record and requests exactly `bytes=start-end`. That is **~0.5 MB instead of the ~13.5 MB file (≈ 25× less)**. Every ranged reply is validated (`206`, `Content-Range`, length, `GRIB…7777` framing) and cached on disk atomically so re-runs are free.
4. **Decode in memory** (`eccodes`, no temp files), roll longitudes 0…360 → −180…180, flip to south→north, convert Pa → hPa, cut to the configured domain (+ padding).
5. **Products** – for each family: ensemble mean (`nanmean`), member-wise minimum (`nanmin`), per-member lows (see below); then the **blend** (see below). Everything is drawn as **filled contour polygons** (`contourpy`) written as compact GeoJSON, one file per family × product × forecast hour (`data/<run>/<family>/<product>/fNNN.json`), plus `manifest.json` (schema 2: run info, families and member lists, class edges, ColorBrewer colours, frames and which families each frame has). All views share one colour scale, so they compare directly.

### ECMWF IFS ENS data
Open data from `s3://ecmwf-forecasts` (`<date>/<cycle>z/ifs/0p25/`): the **`enfo`** stream holds the **50 perturbed members**, the control forecast is the deterministic **`oper`** run (the ENS control is identical to HRES since IFS 50r1; older cycles fall back to `enfo` `cf`). Each step has a JSON-lines `.index`; the client reads it once per step and then downloads only the `msl` message of each member by byte range (≈ 0.5 MB each instead of whole multi-GB files). 00/12Z runs have 3-hourly steps to 144 h then 6-hourly to 360 h (the 10-day window uses F000–F240); 06/18Z runs stop at F144. ECMWF GRIB2 uses CCSDS packing (needs ecCodes ≥ 2.2x with AEC support – the pip wheel has it) and its longitudes start at 180°E; the decoder builds longitudes from the grid increments and rolls them to −180…180.

### Blend (GEFS + ECMWF IFS ENS)
* **Blended ensemble mean** – by default the **equal-weight average of the two family means** (`[blend] weighting = "equal"`), so each system counts once regardless of member count. `"pooled"` instead averages all 82 members equally (ECMWF then carries 51/82 of the weight).
* **Blended member minimum** – the lowest MSLP over **all 82 members** at each grid point; each low centre of that envelope is credited to the member that supplies the minimum.
* **Blend lows** – every low centre of every member of both systems, drawn in two colours by system (toggle: *View ▸ Colour blend lows by system*).
* **Cursor read-out** – shows the blended value together with each system's own value and their difference (ECMWF − GEFS).
* The blend frame exists for an hour only when both families have it (so, on a 06/18Z cycle, to F144). Grids are combined on the first family's grid (both are 0.25° and identical in practice; the pipeline resamples if they ever differ).

### Low-pressure centres
For every member, MSLP is lightly Gaussian-smoothed (0.5°) *only to locate* minima; a point is a low when it is the minimum within a 2° radius, below 1005 hPa (as in the reference GEFS image), at least 1.5° from a deeper low, and not on the data border. The *reported* pressure is the raw grid minimum. All of this is in `[lows]` in `config.toml`.

**Member-minimum product:** the same detector runs on the member-wise minimum field (`[lows_min]`, wider 3° radius because the envelope is broad). Each L is attributed to the member that supplies the minimum at that grid cell.

**Cursor read-out:** every frame also carries a decimated value grid (`[tooltip] grid_deg`, default 0.5°, stored as tenths of hPa). The browser interpolates it bilinearly, so a popup follows the pointer anywhere on the map for both products, showing the pressure, a colour swatch, lat/lon and any low-centre markers under the cursor (tap on touch screens). Use `grid_deg = 0.25` for sharper values at roughly double the per-frame size.

### Rate-limit / server-friendliness
* One global request limiter (`requests_per_second = 8`) shared by all threads; only `workers = 6` keep-alive connections.
* Exponential backoff **with jitter** (1, 2, 4 … ≤ 60 s) on `429`, `5xx` (S3 *SlowDown* = 503) and network errors; honours `Retry-After`; 404 is never retried; hard stop after `max_retries`.
* Range requests + `.idx` (≈ 25× less data), on-disk cache, one-frame look-ahead (next frame downloads while the current one is contoured), explicit `User-Agent` (auto-set to your repo in Actions).
* Members that fail after retries are dropped; a frame is published only if ≥ 80 % of members arrived (`min_member_fraction`).
* The scheduled workflow skips the whole build when the live site already serves the newest cycle.

### Interface

A flat, operational-workstation layout: title bar, **menu bar** (File · View · Ensemble · Products · Help), **toolbar** (ensemble-family switch GEFS / ECMWF IFS ENS / Blend, product switch, data opacity, reset view, theme), map with a CAVE-style three-line header legend (run · product · forecast hour/valid time), a flat colour bar, a time bar, and a **status bar** showing the cursor's lat/lon, pressure and nearest low. A cursor-following pop-up gives the same read-out on the map.

**Dark / light theme:** View ▸ Theme, the toolbar button, or the **T** key. Dark is the default; the choice is remembered in the browser. Map colours per theme (land, borders, graticule, L markers) live in `THEMES` at the top of `site/app.js`; page chrome colours are CSS variables at the top of `site/style.css`.

## 3. Cartography & colour

* **Basemap**: Natural Earth 1:10m (public domain), clipped to the data domain and simplified at build time. Stacking order, bottom → top: land fill · **data** · lakes · coastline · state/province lines · international boundaries.
* **Line hierarchy** (standard practice – weight and contrast follow administrative rank): international boundaries are the heaviest and darkest, with a light casing so they stay legible over both dark and light fills; disputed/indefinite boundaries are dashed; treaty/median maritime lines are fine grey dots; first-order divisions (states, provinces) are thinner mid-grey with a faint casing; coastlines are finest. **All widths scale ≈ 20 % per zoom level.** Lakes are drawn as water cut-outs above the data. The lat/lon graticule is a thin dotted vector layer drawn *beneath* the coast/state/country lines (only its degree labels sit on top), with density adapting to zoom (10° → 1°). State/province lines use every Natural Earth admin-1 class (including "statistical" and "meta bounds" classes, which are real state borders for the US and Canada), so no border segments are dropped.
* **Colour** – one shared 4 hPa-class scale for both products (edges and ramps are set in `config.toml` → `[scale]`; official hex values are unit-tested against `palettable`):
  * **≥ 1000 hPa:** ColorBrewer diverging **RdYlBu (9-class)**, reversed so blue is just above 1000 hPa, pale yellow ≈ 1016–1020 and red ≥ 1032.
  * **980–1000 hPa:** sequential **Blues**, darkening steadily as pressure falls (navy at 980).
  * **< 980 hPa:** sequential **RdPu**, from bright pink at 976–980 to deep purple below 948, so deep lows read as a distinct, intense tier.
  * Colours are interpolated in OKLab between the ColorBrewer anchors. The legend marks the 980 and 1000 hPa hinges.
* **Low markers** – a bold red **L** (ColorBrewer Reds) with a white halo and the member's central pressure in hPa directly above it. Markers and labels are large and grow with zoom; labels are de-cluttered (deepest lows labelled first; more appear as you zoom in). The cursor read-out lists every low under the pointer with its member and exact value. Both products show L + hPa.
  Change `palette`, `reverse`, `edges`, `diverging_center` in `config.toml`; available ramps are listed in `gefs_pipeline/palette.py`.
* Web-Mercator projection (the web-map standard). The projection of your reference images is not reproduced.

## 4. Publish on GitHub Pages (exact steps)

### One-time
1. **Create an empty repository** on GitHub (e.g. `gefs-mslp-viewer`) – *no* README, `.gitignore` or licence (the project already has them).
2. In that repo: **Settings → Pages → Build and deployment → Source: “GitHub Actions”.**
3. In your terminal, from the unzipped project folder:

```bash
cd gefs-mslp-viewer
git init -b main
git config user.name  "Your Name"            # only if not already set globally
git config user.email "you@example.com"
git add .
git status                                   # sanity check: no dist/, .cache/ or .venv/ listed
git commit -m "Initial commit: GEFS MSLP pipeline and Pages viewer"
git remote add origin https://github.com/<YOUR-USER>/<YOUR-REPO>.git
git push -u origin main
```

Using the GitHub CLI instead (does steps 1–3 in one go; Pages must still be set to “GitHub Actions” in Settings, or run
`gh api -X POST repos/<YOUR-USER>/<YOUR-REPO>/pages -f build_type=workflow`):

```bash
git init -b main && git add . && git commit -m "Initial commit: GEFS MSLP pipeline and Pages viewer"
gh repo create <YOUR-REPO> --public --source=. --remote=origin --push
```

4. The push starts the workflow. Watch **Actions → “Build & deploy GEFS MSLP viewer”**. The first full build takes roughly 10–20 min. If you pushed *before* enabling Pages, the `deploy` job fails – enable Pages, then **Actions → the failed run → Re-run failed jobs**.
5. Your viewer is at **`https://<YOUR-USER>.github.io/<YOUR-REPO>/`**.

### Afterwards
* Runs automatically at 08:35 and 20:35 UTC, with a second attempt at 09:35 / 21:35 (ECMWF open data for the 00Z/12Z runs completes ≈ 7.5–9 h after cycle time; GEFS is done earlier). A run whose cycle is already live is skipped. Edit the `cron` line to change.
* **Run it by hand:** Actions → *Build & deploy* → *Run workflow* (options: pin a `cycle`, limit `max_fh`, `force`).
* Change `config.toml` (domain, hours, palettes…) and push – the site rebuilds.
* GitHub disables scheduled workflows after 60 days without repository activity (public repos) – push any commit or re-enable them in the Actions tab.
* Custom domain? Add a repository *variable* `SITE_URL` (Settings → Secrets and variables → Actions → Variables) so the “already deployed?” check looks at the right address.
* GitHub Pages on a *private* repository needs a paid GitHub plan; public repos are free.
* Pages keeps **only the latest cycle** (each deploy replaces the site). Archive older cycles if you need them.

## 5. Credits & licences
Forecast data: NOAA/NCEP GEFS via the [NOAA Open Data Dissemination](https://registry.opendata.aws/noaa-gefs/) programme (public domain). **ECMWF IFS ENS** open data © European Centre for Medium-Range Weather Forecasts ([ECMWF](https://www.ecmwf.int)), published under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) – attribution is shown in the map and Help dialog; keep it if you re-host. The ECMWF data here have been **modified** (decoded, subset, contoured, averaged and blended) and ECMWF does not accept liability for errors or omissions in the data or this processing. See ECMWF's [open data terms](https://www.ecmwf.int/en/forecasts/datasets/open-data). Basemap: [Natural Earth](https://www.naturalearthdata.com/) (public domain). Colour schemes: [ColorBrewer](https://colorbrewer2.org) © Cynthia Brewer, Mark Harrower & Penn State (Apache-2.0). Map engine: [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause, vendored in `site/vendor/leaflet/`). Add a `LICENSE` of your choice for your own code.

*This is an unofficial display of model guidance, not an official NOAA product; do not use it for life-safety decisions.*
