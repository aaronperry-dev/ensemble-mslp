# Ensemble MSLP Viewer — NOAA GEFS · ECMWF IFS ENS · Canadian GEPS · NOAA AIGEFS · Blends

A static, GitHub-Pages-hosted web map of **ensemble mean sea-level pressure (MSLP)** from four ensemble systems — **NOAA GEFS (31 members)**, **ECMWF IFS ENS (51 members)**, the **Canadian GEPS (21 members)** and **NOAA AIGEFS (31 members, AI-based)** — viewable **per system or as a blend of any two or more**, rebuilt automatically every model cycle by a GitHub Action.

| | |
|---|---|
| **Ensemble families** | **GEFS** (31), **ECMWF IFS ENS** (51), **CMC GEPS** (21), **NOAA AIGEFS** (31) and **Blend**. Switch with the *Ensemble* toolbar buttons / menu, or keys **G**, **E**, **C**, **A**, **B**. Choosing *Blend* shows tick-boxes on the map to pick which systems are blended (any two or more; all four = 134 members). Every product exists for every view, including every blend. |
| **Product 1 – Ensemble mean + member lows** | Filled contours of the ensemble-mean MSLP, with an **“L” at every low-pressure centre (< 1005 hPa) of every ensemble member** (GEFS `gec00` + `gep01`…`gep30`; ECMWF `ec00` + `ep01`…`ep50`; GEPS `cc00` + `cp01`…`cp20`; AIGEFS `ai00` = `mem000`, `ai01`…`ai30`). Each L is labelled with its central pressure in hPa. |
| **Product 2 – Member-wise minimum** | Filled contours of the *lowest* MSLP forecast by any member at each grid point (the “deepest-low envelope”), with an **L + hPa value at each local minimum** of that envelope. |
| **Product 3 – Swath (accumulated minimum)** | The member-wise minimum **accumulated over the forecast so far**: at each point, the lowest MSLP any member forecasts at any hour from F000 to the hour shown. It builds up like a storm-track swath as you step or play through the forecast and never rises. L markers are the deepest points of the swath; the read-out gives the forecast hour (and valid time) at which each minimum was reached and the member that supplies it. Key **3**. |
| **Viewer** | Pan / zoom (mouse, touch, keyboard), ensemble-family and product switches, forecast-hour slider + animation, opacity, lat/lon grid, state/province and lake toggles, scale bar, shareable URL (`#e=gefs_ifs_geps&p=swath&f=132&v=lat,lon,zoom`; `e` is `gefs`, `ifs`, `geps`, or a blend such as `gefs_ifs`). |
| **Data** | NOAA GEFS on AWS Open Data (`s3://noaa-gefs-pds`), ECMWF open data on AWS (`s3://ecmwf-forecasts`) the Canadian GEPS on ECCC's MSC Datamart (`dd.weather.gc.ca`) and NOAA AIGEFS on AWS (`s3://noaa-nws-graphcastgfs-pds`); all public, no credentials. |

```
.
├── config.toml                  # every tunable: domain, hours, rate limits, lows, palettes, colour classes
├── requirements.txt / -dev.txt
├── gefs_pipeline/               # the backend (python -m gefs_pipeline ...)
│   ├── fetcher.py               #   polite HTTP client: rate limit, backoff+jitter, Range requests, .idx parsing
│   ├── gefs.py                  #   GEFS bucket layout, 0.25°→0.5° fallback
│   ├── sources.py               #   ensemble families (GEFS, ECMWF IFS ENS, CMC GEPS, NOAA AIGEFS): members, schedule, fetch, cycle readiness
│   ├── decode.py                #   in-memory GRIB2 decode (ecCodes) + domain subset
│   ├── products.py              #   mean / min, per-member low detection
│   ├── gridio.py                #   compact gzip'd binary grids shipped to the browser (+ gzip of the basemap)
│   ├── palette.py               #   ColorBrewer ramps, OKLab interpolation
│   ├── pipeline.py / cli.py     #   orchestration + command line
│   └── basemap.py               #   Natural Earth → clipped, simplified basemap layers
├── site/                        # the viewer (plain HTML/CSS/JS + vendored Leaflet, no build step)
│   ├── engine.js                #   browser-side compute: inflate/decode grids, blends, swath, lows, colouring
│   └── data/basemap/*.json      #   committed basemap vectors (gzip'd into the built site)
├── tests/                       # pytest (mock S3 server with Range + 503s, GRIB round-trip, binary grid format, palettes, and the browser engine run under Node against the Python reference)
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

# full production build: GEFS 31 + ECMWF 51 + GEPS 21 + AIGEFS 31 members, f000-f240 = 10 days (≈ 8 000 requests, ≈ 3.5 GB downloaded, ~25 min; the published site is only ~15 MB, see *Browser-side processing*)
python -m gefs_pipeline build --out dist

python -m http.server 8000 --directory dist             # open http://localhost:8000
```

Useful flags: `--cycle 2026111806` (pin a run), `--max-fh`, `--members N`, `--rps 4` (slow down), `--workers`, `--no-cache`, `-v`.
`python -m gefs_pipeline resolve-cycle` just prints the newest complete cycle.

## 2. What the pipeline does

1. **Pick the cycle.** Starting at the current time, step back through the cycles in `[run] cycles` (default **00Z and 12Z** – the ECMWF runs that reach day 15) until one is *complete for every family* (GEFS: last forecast hour exists for all 31 members; ECMWF: last step of the ensemble and control streams is published; GEPS: the first and last all-member files exist; AIGEFS is *optional*, see below), so a half-published run is never shown and both systems always share the same initialisation time. Add 06/18Z to `cycles` for a GEFS-led site; ECMWF then ends at F144 for those cycles (the viewer shows exactly the hours each system has).
2. **Pick the grid per forecast hour – highest resolution first.**
   `pgrb2sp25` (**0.25°**) is probed first; if the object does not exist, `pgrb2ap5` (0.5°) is used.
   Verified against the bucket: **0.25° exists for f000–f240, exactly the 10-day window this project is configured for (`[forecast] stop = 240`)**, so every frame is 0.25°. The 0.5° product is only a safety net if a 0.25° object is missing; it can also serve longer runs (raise `stop` up to 384: f246+ exist only at 0.5° and are cubic-spline-upsampled for contouring). The viewer labels every frame with its grid.
3. **Download only the MSLP bytes (HTTP Range).**
   For each member × hour the client reads the tiny `.idx` inventory, finds the `PRMSL:mean sea level` record and requests exactly `bytes=start-end`. That is **~0.5 MB instead of the ~13.5 MB file (≈ 25× less)**. Every ranged reply is validated (`206`, `Content-Range`, length, `GRIB…7777` framing) and cached on disk atomically so re-runs are free.
4. **Decode in memory** (`eccodes`, no temp files), roll longitudes 0…360 → −180…180, flip to south→north, convert Pa → hPa, cut to the configured domain (+ padding).
5. **Per-system grids** – for each system and hour: ensemble mean (`nanmean`) with every member's low centres, and the member-wise minimum (`nanmin`) with the member that supplies it at each cell. These are written as **gzip'd binary grids** (`data/<run>/<system>/<mean|min>/fNNN.bin.gz`, ~40 KB each) plus `manifest.json` (schema 3: run info, systems and member lists, class edges, ColorBrewer colours, frames). Nothing else is pre-built.

### Browser-side processing (and the 1 GB Pages limit)
The pipeline only ships those per-system grids; **the browser does the rest** (`site/engine.js`): it inflates and decodes the grids, forms **any blend** of two or more systems (regridding GEPS 0.5° to the first system's grid), accumulates the **swath**, finds the low centres of every derived minimum field (a port of `detect_lows`, verified identical by the tests), smooths the field (Catmull-Rom) and colours it pixel by pixel on a canvas, and builds the tooltips.
* **Format** (`gefs_pipeline/gridio.py`): Int16 tenths of hPa, each value predicted from its neighbours (left + up − up-left), residuals zig-zag coded and split into byte planes, then gzip. Lossless to 0.1 hPa; a ~113 k-cell field becomes ~30–40 KB. The basemap is shipped as `*.json.gz` as well; only `manifest.json` stays plain (small, and the workflow reads it).
* **Why `.gz` files rather than relying on Pages' own compression:** the Pages limit (1 GB) applies to the *tar of the site* that is deployed (GitHub's Pages-artifact docs recommend keeping that tar under 1 GB), so JSON counts at its full size, and Pages cannot send `Content-Encoding: gzip` for pre-compressed files. Storing the data already gzip'd makes the stored bytes the compressed bytes, and the browser inflates them itself with `DecompressionStream` (Chrome/Edge 80+, Firefox 113+, Safari 16.4+). A server that has already decoded the file is handled too.
* **Result:** the published site is ~15 MB per cycle (it was ~700 MB with pre-contoured GeoJSON for every blend), every blend combination and the swath cost nothing, and there is no site-size budget to manage. Downloads are small too: a view loads ~40–80 KB per system per hour; the swath fetches the *min* grids of all earlier hours once (~40 KB × hours × systems).
* Because the data are plain numbers in the browser, adding a system or product later needs only the encoder to write another grid and a few lines in `app.js`.

### ECMWF IFS ENS data
Open data from `s3://ecmwf-forecasts` (`<date>/<cycle>z/ifs/0p25/`): the **`enfo`** stream holds the **50 perturbed members**, the control forecast is the deterministic **`oper`** run (the ENS control is identical to HRES since IFS 50r1; older cycles fall back to `enfo` `cf`). Each step has a JSON-lines `.index`; the client reads it once per step and then downloads only the `msl` message of each member by byte range (≈ 0.5 MB each instead of whole multi-GB files). 00/12Z runs have 3-hourly steps to 144 h then 6-hourly to 360 h (the 10-day window uses F000–F240); 06/18Z runs stop at F144. ECMWF GRIB2 uses CCSDS packing (needs ecCodes ≥ 2.2x with AEC support – the pip wheel has it) and its longitudes start at 180°E; the decoder builds longitudes from the grid increments and rolls them to −180…180.

### Canadian GEPS data
Environment and Climate Change Canada's Global Ensemble Prediction System, from the MSC Datamart: `https://dd.weather.gc.ca/<YYYYMMDD>/WXO-DD/ensemble/geps/grib2/raw/<HH>/<hhh>/CMC_geps-raw_PRMSL_MSL_0_latlon0p5x0p5_<YYYYMMDDHH>_P<hhh>_allmbrs.grib2`. 21 members (control + 20), **0.5°** (the only resolution published for the raw members), 00/12Z runs to 384 h (3-hourly to 192 h, then 6-hourly). Unlike the other two, **one file per forecast hour holds every member** (~2.2 MB), so each step is downloaded once and split into members (control first, then by the GRIB `perturbationNumber`; if the keys are missing the file order is used). The Datamart keeps only a few recent days, so very old cycles cannot be rebuilt. GEPS files are posted ~5 h after cycle time. The 0.5° field is contoured after cubic-spline upsampling and, inside a blend, bilinearly regridded onto the finer family's grid.

*Disable a system* by removing it from `[run] families` (e.g. `["gefs", "ifs"]`); the viewer adapts to whatever the manifest contains. If a system fails for an hour (e.g. fewer than 80 % of its members arrive) that hour is built without it, and the viewer simply omits it there.

### NOAA AIGEFS data
NOAA's AI-based ensemble (a GraphCast model fine-tuned on GDAS, run from GEFS initial conditions), on AWS Open Data: `https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com/EAGLE_ensemble/aigefs.<YYYYMMDD>/<HH>/mem<NNN>/model/atmos/grib2/aigefs.t<HH>z.sfc.f<FFF>.grib2` plus a `.idx` file. 31 members (`mem000`…`mem030`; shown as `ai00`…`ai30`), 0.25°, 6-hourly to 384 h, all four cycles (only 00/12Z are used, like the others). Each member file holds many surface fields; the pipeline reads the `.idx` and fetches only the `PRMSL:mean sea level` message (~1.3 MB) with an HTTP byte range, exactly as for GEFS. The data are CC0.
* **Optional system.** AIGEFS is published irregularly (from about 7.5 hours to more than a day after cycle time), so it does not gate the cycle (`[run] optional = ["aigefs"]`). A build includes it only when every member is complete for the cycle. If it appears later, the next scheduled attempt sees that the live site lacks it and rebuilds the same cycle with it.
* **Not independent of GEFS.** AIGEFS starts from GEFS initial conditions, so a blend containing both is not a blend of independent ensembles; the Help dialog says so. `mem000` is treated as member 0 (NOAA does not label it as a control in the file metadata).

### Blends (any combination of the systems)
In the viewer choose *Blend* and tick the systems to include; every combination of two or more is available and is computed in the browser.
* **Blended ensemble mean** – by default the **equal-weight average of the system means** (`[blend] weighting = "equal"`), so each system counts once regardless of member count. `"members"` instead averages all members pooled (a 51-member ensemble then outweighs a 21-member one).
* **Blended member minimum** – the lowest MSLP over **all members of the chosen systems** at each grid point; each low centre of that envelope is credited to the member that supplies the minimum.
* **Blend lows** – every low centre of every member of the chosen systems, drawn in a colour per system (red GEFS, yellow ECMWF, teal GEPS, violet AIGEFS; toggle: *View ▸ Colour blend lows by system*).
* **Cursor read-out** – the blended value together with each system's own value and, for two systems, their difference (for three or more: the spread max − min).
* A blend frame exists for an hour only when every system in it has that hour (on a 06/18Z cycle, to F144). Systems are combined on the first listed system's grid; when grids differ (GEPS 0.5° vs 0.25°) the coarser one is interpolated, and the status bar says so.

### Low-pressure centres
For every member, MSLP is lightly Gaussian-smoothed (0.5°) *only to locate* minima; a point is a low when it is the minimum within a 2° radius, below 1005 hPa (as in the reference GEFS image), at least 1.5° from a deeper low, and not on the data border. The *reported* pressure is the raw grid minimum. All of this is in `[lows]` in `config.toml`.

**Member-minimum product:** the same detector runs on the member-wise minimum field (`[lows_min]`, wider 3° radius because the envelope is broad). Each L is attributed to the member that supplies the minimum at that grid cell.

**Swath product:** the same detector runs on the accumulated minimum; each L also carries the forecast hour at which that minimum was reached.

**Cursor read-out:** the browser reads the full-resolution grids directly (bilinear), so a popup follows the pointer anywhere on the map for every product, showing the pressure, a colour swatch, each system's own value in a blend, lat/lon and any low-centre markers under the cursor (tap on touch screens). For the swath it also gives the hour the minimum was reached.

### Rate-limit / server-friendliness
* One global request limiter (`requests_per_second = 8`) shared by all threads; only `workers = 6` keep-alive connections.
* Exponential backoff **with jitter** (1, 2, 4 … ≤ 60 s) on `429`, `5xx` (S3 *SlowDown* = 503) and network errors; honours `Retry-After`; 404 is never retried; hard stop after `max_retries`.
* Range requests + `.idx` (≈ 25× less data), on-disk cache, one-frame look-ahead (next frame downloads while the current one is contoured), explicit `User-Agent` (auto-set to your repo in Actions).
* Members that fail after retries are dropped; a frame is published only if ≥ 80 % of members arrived (`min_member_fraction`).
* The scheduled workflow skips the whole build when the live site already serves the newest cycle.

### Interface

A flat, operational-workstation layout: title bar, **menu bar** (File · View · Ensemble · Products · Help), **toolbar** (ensemble-system switch GEFS / ECMWF IFS ENS / CMC GEPS / NOAA AIGEFS / Blend, product switch, data opacity, reset view, theme), map with a CAVE-style three-line header legend (run · product · forecast hour/valid time), a flat colour bar, a time bar, and a **status bar** showing the cursor's lat/lon, pressure and nearest low. A cursor-following pop-up gives the same read-out on the map; in a blend, tick-boxes on the map choose the systems.

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
* Runs automatically at 08:35, 09:35, 10:35, 11:35 and 20:35, 21:35, 22:35, 23:35 UTC (ECMWF open data for the 00Z/12Z runs completes ≈ 7.5–9 h after cycle time; GEFS and GEPS are done earlier; AIGEFS is the most irregular). A run whose cycle is already live *with every system that is ready now* is skipped; a late AIGEFS therefore triggers a rebuild of that cycle. Edit the `cron` line to change. A build downloads ≈ 3.5 GB, which fits comfortably in the hosted runner's time and disk limits.
* **Run it by hand:** Actions → *Build & deploy* → *Run workflow* (options: pin a `cycle`, limit `max_fh`, `force`).
* Change `config.toml` (domain, hours, palettes…) and push – the site rebuilds.
* GitHub disables scheduled workflows after 60 days without repository activity (public repos) – push any commit or re-enable them in the Actions tab.
* Custom domain? Add a repository *variable* `SITE_URL` (Settings → Secrets and variables → Actions → Variables) so the “already deployed?” check looks at the right address.
* GitHub Pages on a *private* repository needs a paid GitHub plan; public repos are free.
* Pages keeps **only the latest cycle** (each deploy replaces the site). Archive older cycles if you need them.

## 5. Credits & licences
Forecast data: NOAA/NCEP GEFS via the [NOAA Open Data Dissemination](https://registry.opendata.aws/noaa-gefs/) programme (public domain). **NOAA AIGEFS** data: NOAA/NCEP via the NOAA Open Data Dissemination programme (CC0). **CMC GEPS** data: *Data Source: Environment and Climate Change Canada*, used under the [ECCC Data Server End-use Licence](https://eccc-msc.github.io/open-data/licence/readme_en/) (commercial use allowed; attribution required; no endorsement implied; shown in the map and Help dialog). **ECMWF IFS ENS** open data © European Centre for Medium-Range Weather Forecasts ([ECMWF](https://www.ecmwf.int)), published under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) – attribution is shown in the map and Help dialog; keep it if you re-host. The data here have been **modified** (decoded, subset, contoured, averaged, regridded and blended); neither ECCC nor ECMWF endorses this site, and ECMWF does not accept liability for errors or omissions in the data or this processing. See ECMWF's [open data terms](https://www.ecmwf.int/en/forecasts/datasets/open-data). Basemap: [Natural Earth](https://www.naturalearthdata.com/) (public domain). Colour schemes: [ColorBrewer](https://colorbrewer2.org) © Cynthia Brewer, Mark Harrower & Penn State (Apache-2.0). Map engine: [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause, vendored in `site/vendor/leaflet/`). Add a `LICENSE` of your choice for your own code.

*This is an unofficial display of model guidance, not an official NOAA product; do not use it for life-safety decisions.*
