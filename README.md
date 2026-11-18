# GEFS Ensemble MSLP Viewer

A static, GitHub-Pages-hosted web map of **GEFS ensemble mean sea-level pressure (MSLP)**, rebuilt automatically every model cycle by a GitHub Action.

| | |
|---|---|
| **Product 1 – Ensemble mean + member lows** | Filled contours of the ensemble-mean MSLP, with an **“L” at every low-pressure centre (< 1005 hPa) of every ensemble member** (31 members: `gec00` + `gep01`…`gep30`). Hover a low to see which member and its central pressure. |
| **Product 2 – Member-wise minimum** | Filled contours of the *lowest* MSLP forecast by any member at each grid point (the “deepest-low envelope”). |
| **Viewer** | Pan / zoom (mouse, touch, keyboard), product switch, forecast-hour slider + animation, opacity, lat/lon grid, state/province and lake toggles, scale bar, shareable URL (`#p=min&f=132&v=lat,lon,zoom`). |
| **Data** | NOAA GEFS on AWS Open Data: `s3://noaa-gefs-pds` (public, no credentials). |

```
.
├── config.toml                  # every tunable: domain, hours, rate limits, lows, palettes, contour classes
├── requirements.txt / -dev.txt
├── gefs_pipeline/               # the backend (python -m gefs_pipeline ...)
│   ├── fetcher.py               #   polite HTTP client: rate limit, backoff+jitter, Range requests, .idx parsing
│   ├── gefs.py                  #   bucket layout, 0.25°→0.5° fallback, newest-complete-cycle detection
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
python -m gefs_pipeline build --out dist --max-fh 24 --members 6

# full production build: 31 members, f000-f384 (≈ 4 000 requests, ≈ 0.8 GB, ~6-10 min)
python -m gefs_pipeline build --out dist

python -m http.server 8000 --directory dist             # open http://localhost:8000
```

Useful flags: `--cycle 2026111806` (pin a run), `--max-fh`, `--members N`, `--rps 4` (slow down), `--workers`, `--no-cache`, `-v`.
`python -m gefs_pipeline resolve-cycle` just prints the newest complete cycle.

## 2. What the pipeline does

1. **Pick the cycle.** Starting at the current time, step back through 00/06/12/18Z until a cycle is *complete* (the last forecast hour exists for all 31 members), so a half-published run is never shown.
2. **Pick the grid per forecast hour – highest resolution first.**
   `pgrb2sp25` (**0.25°**) is probed first; if the object does not exist, `pgrb2ap5` (0.5°) is used.
   Verified against the bucket: **0.25° only exists for f000–f240**; f246–f384 exist only at 0.5° (the +270 h GEFS image in your reference set is therefore a 0.5° field). The viewer labels every frame with its grid, and 0.5° frames are cubic-spline-upsampled to 0.25° spacing *for contouring only* so polygons are smooth.
3. **Download only the MSLP bytes (HTTP Range).**
   For each member × hour the client reads the tiny `.idx` inventory, finds the `PRMSL:mean sea level` record and requests exactly `bytes=start-end`. That is **~0.5 MB instead of the ~13.5 MB file (≈ 25× less)**. Every ranged reply is validated (`206`, `Content-Range`, length, `GRIB…7777` framing) and cached on disk atomically so re-runs are free.
4. **Decode in memory** (`eccodes`, no temp files), roll longitudes 0…360 → −180…180, flip to south→north, convert Pa → hPa, cut to the configured domain (+ padding).
5. **Products** – ensemble mean (`nanmean`), member-wise minimum (`nanmin`), per-member lows (see below), then **filled contour polygons** (`contourpy`) written as compact GeoJSON, one file per product per forecast hour, plus `manifest.json` (run info, class edges, ColorBrewer colours, frames).

### Low-pressure centres
For every member, MSLP is lightly Gaussian-smoothed (0.5°) *only to locate* minima; a point is a low when it is the minimum within a 2° radius, below 1005 hPa (as in the reference GEFS image), at least 1.5° from a deeper low, and not on the data border. The *reported* pressure is the raw grid minimum. All of this is in `[lows]` in `config.toml`.

### Rate-limit / server-friendliness
* One global request limiter (`requests_per_second = 8`) shared by all threads; only `workers = 6` keep-alive connections.
* Exponential backoff **with jitter** (1, 2, 4 … ≤ 60 s) on `429`, `5xx` (S3 *SlowDown* = 503) and network errors; honours `Retry-After`; 404 is never retried; hard stop after `max_retries`.
* Range requests + `.idx` (≈ 25× less data), on-disk cache, one-frame look-ahead (next frame downloads while the current one is contoured), explicit `User-Agent` (auto-set to your repo in Actions).
* Members that fail after retries are dropped; a frame is published only if ≥ 80 % of members arrived (`min_member_fraction`).
* The scheduled workflow skips the whole build when the live site already serves the newest cycle.

## 3. Cartography & colour

* **Basemap**: Natural Earth 1:10m (public domain), clipped to the data domain and simplified at build time. Stacking order, bottom → top: land fill · **data** · lakes · coastline · state/province lines · international boundaries.
* **Line hierarchy** (standard practice – weight and contrast follow administrative rank): international boundaries are the heaviest and darkest, with a light casing so they stay legible over both dark-red and dark-blue fills; disputed/indefinite boundaries are dashed; treaty/median maritime lines are fine grey dots; first-order divisions (states, provinces) are thinner mid-grey with a faint casing; coastlines are finest. **All widths scale ≈ 20 % per zoom level** so the map stays balanced from continental to local views. Lakes are drawn as water cut-outs above the data. Graticule density adapts to zoom (10° → 1°).
* **Colour** (all ColorBrewer, interpolated in OKLab to one colour per 4 hPa class; official hex values are unit-tested against `palettable`):
  * *Mean + lows* – sequential **YlOrRd** (as in your GEFS reference: warm = higher pressure). Low markers use ColorBrewer’s darkest blue with a white halo.
  * *Member minimum* – diverging **RdBu**, reversed (blue = low, red = high) with the neutral mid-colour pinned to the 1012–1016 hPa class, so deep lows stand out as saturated blues.
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
* Runs automatically at 05:25, 11:25, 17:25, 23:25 UTC (≈ 5.5 h after each cycle). Edit the `cron` line to change.
* **Run it by hand:** Actions → *Build & deploy* → *Run workflow* (options: pin a `cycle`, limit `max_fh`, `force`).
* Change `config.toml` (domain, hours, palettes…) and push – the site rebuilds.
* GitHub disables scheduled workflows after 60 days without repository activity (public repos) – push any commit or re-enable them in the Actions tab.
* Custom domain? Add a repository *variable* `SITE_URL` (Settings → Secrets and variables → Actions → Variables) so the “already deployed?” check looks at the right address.
* GitHub Pages on a *private* repository needs a paid GitHub plan; public repos are free.
* Pages keeps **only the latest cycle** (each deploy replaces the site). Archive older cycles if you need them.

## 5. Credits & licences
Forecast data: NOAA/NCEP GEFS via the [NOAA Open Data Dissemination](https://registry.opendata.aws/noaa-gefs/) programme (public domain). Basemap: [Natural Earth](https://www.naturalearthdata.com/) (public domain). Colour schemes: [ColorBrewer](https://colorbrewer2.org) © Cynthia Brewer, Mark Harrower & Penn State (Apache-2.0). Map engine: [Leaflet](https://leafletjs.com) 1.9.4 (BSD-2-Clause, vendored in `site/vendor/leaflet/`). Add a `LICENSE` of your choice for your own code.

*This is an unofficial display of model guidance, not an official NOAA product; do not use it for life-safety decisions.*
