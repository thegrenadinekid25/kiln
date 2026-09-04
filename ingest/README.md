# kiln/ingest

The land-surface-temperature ingestion pipeline. This is the only thing that
writes to the `kiln` schema; the public site reads it with the anon key under
RLS.

Once a day it discovers NASA LANCE near-real-time MODIS granules for a target
date, masks out pixels the satellite also saw burning, and produces two things
from the same masked pixels:

- **1-degree tile maxima** upserted into `kiln.lst_readings`, the summary layer.
- **A native-resolution raster tile pyramid** published to the `kiln-tiles`
  Storage bucket, which is what the map actually draws at 1 km resolution.

The same day is then folded into an **all-time archive** -- the hottest reading
the pipeline has ever recorded per place -- which is the map's second view.

## Why Python here

The rest of the portfolio is Bun and TypeScript. This directory is a deliberate
exception: the granules are HDF4-EOS swath files, and `numpy` + `pyhdf` is the
only mature way to read them. Plain `pip` and `requirements.txt`, no Poetry.

## Data source

| | |
| --- | --- |
| LST products | `MOD11_L2` (Terra), `MYD11_L2` (Aqua), collection 6.1 |
| Fire products | `MOD14` (Terra), `MYD14` (Aqua), the same overpasses |
| Discovery | [CMR](https://cmr.earthdata.nasa.gov/search/granules.json); `day_night_flag=day` for LST, unfiltered for fire |
| Daily provider | `LANCEMODIS` -- near-real-time, published within about three hours, recent days only |
| Archive provider | `LPCLOUD` -- science-quality, collection `061`, the whole mission (see [Historical backfills](#historical-backfills)) |
| Files | `nrt3` / `nrt4.modaps.eosdis.nasa.gov` (daily) or `data.lpdaac.earthdatacloud.nasa.gov` (archive), bearer-token authenticated |
| Volume | roughly 1.5-2.5 GB of LST per satellite per day plus a few hundred MB of fire granules, streamed and deleted one at a time |

Note the asymmetric naming: the LST products carry an `_L2` suffix in CMR and
the fire products do not.

`MOD11_L2` and `MYD11_L2` embed subsampled `Latitude` and `Longitude` SDSs, so
no separate MOD03 geolocation product is needed.

MOD11 and MOD14 granules are paired by the `AYYYYDDD.HHMM` overpass stamp in
their filenames. CMR is queried once per fire product per day and the results
reduced to a stamp-to-URL map. The two feeds name granules differently after the
stamp, but the stamp itself sits in the same place in both, so one pairing rule
serves a daily run and a backfill:

```
NRT      MOD11_L2.A2026242.1125.061.NRT.hdf
archive  MOD11_L2.A2019196.0635.061.2020356013308.hdf
                  ^^^^^^^^^^^^^ the overpass stamp
```

## Historical backfills

`--archive` switches discovery from LANCE to the science-quality archive, for
both the LST and fire products. It is required for any date outside LANCE's
few-day window: the same query against `LANCEMODIS` for 2019-07-15 returns zero
granules.

The provider was chosen empirically, not from documentation. Querying CMR for
`MOD11_L2` on 2019-07-15 with no provider filter returns **`LPCLOUD`**;
`LPDAAC_ECS`, `LAADS` and `LANCEMODIS` all return nothing. `LPCLOUD` serves
`MOD14` and `MYD14` under the same short names the NRT feed uses, so one flag
covers all four products. Data links are direct HTTPS `.hdf`:

```
https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/
  MOD11_L2.061/MOD11_L2.A2019196.0635.061.2020356013308/
  MOD11_L2.A2019196.0635.061.2020356013308.hdf
```

Collection `061` is pinned rather than left open. It is the full-mission
reprocessing -- spot-checked as present for 2000-03-05, 2002-08-15, 2010-06-20
and 2026-01-10 -- and pinning means a future collection appearing mid-backfill
cannot silently start returning two granules per overpass.

### Narrowing to regions

`--bbox W,S,E,N` restricts discovery to granules intersecting a box, and
repeats are ORed. For 2019-07-15 this is the difference between 109 daytime
granules globally and 1 over the Lut Desert.

```bash
# One region
python -m kiln_ingest --archive --date 2019-07-15 --bbox 55,28,62,33 --dry-run

# Several, ORed together
python -m kiln_ingest --archive --date 2019-07-15 \
  --bbox 55,28,62,33 --bbox=-118,32,-114,36 --dry-run
```

**A box starting with a negative longitude needs the `=` form.** `--bbox
-118,32,-114,36` fails with "expected one argument", because argparse reads the
leading minus as a flag; `--bbox=-118,32,-114,36` works. West greater than east
is left alone deliberately -- CMR reads that as a box crossing the antimeridian.

Repeated `bounding_box` parameters are ANDed by CMR unless told otherwise, which
for disjoint regions matches nothing at all. The query adds
`options[bounding_box][or]=true` whenever more than one box is given; verified
against CMR, two disjoint boxes return 0 granules without it and both with it.

### Token handling across redirects

The archive bounces where LANCE does not, and `requests` would drop the
Authorization header on its own when the host changes. Redirects are therefore
followed by hand, re-deciding the token at every hop:

| Hop | Token attached |
| --- | --- |
| `data.lpdaac.earthdatacloud.nasa.gov` | yes |
| 303 to `*.cloudfront.net` (pre-signed) | **no** |
| 302 to `urs.earthdata.nasa.gov` | yes |

`bearer_allowed()` says yes only for HTTPS on `nasa.gov` or a subdomain of it,
with the suffix anchored on a leading dot so `evil-nasa.gov` cannot match. This
is not only a security rule, it is the correct functional behaviour: the
CloudFront hop carries its own signature in the query string and was verified to
return 206 with the header absent.

## Science handling

These are the decisions that determine whether a published number is true.

- **Scaling.** `scale_factor` and `add_offset` are read from the `LST` SDS
  attributes, not hardcoded. A `scale_factor` that is not 0.02 raises
  `UnexpectedGranuleError` and fails the granule rather than silently
  publishing temperatures off by an order of magnitude.
- **Fill.** Pixels equal to `_FillValue` (0 for MOD11/MYD11) are dropped, as is
  anything that decodes outside a -150 C to 200 C plausibility band.
- **Quality control.** A pixel survives only when the QC byte's mandatory QA
  bits (0-1) are `00` or `01` -- LST actually produced, good or acceptable
  quality -- *and* the LST error flag bits (6-7) are `00` or `01`, meaning
  average error at or below 2 K. Cloud-obscured and high-error pixels never
  reach the database.
- **Units.** Kelvin to Celsius, once, in `decode_lst_celsius`.
- **Geolocation.** The `Latitude`/`Longitude` SDSs are subsampled 5x relative to
  the 1 km LST grid. Each LST pixel is mapped to its *nearest* stored
  geolocation point rather than interpolated. The error is bounded by about
  half the subsampling stride -- a couple of kilometres -- far below the ~111 km
  granularity of a 1-degree tile.
- **Active fires.** A burning pixel is a real land-surface temperature and a
  useless one: a flame front would top the map every day of fire season and say
  nothing about how hot the ground is. `MOD14`/`MYD14` list the fire pixels
  detected on the same overpass as `FP_latitude`/`FP_longitude` vectors. Those
  detections are binned to 0.02 degrees (about 2.2 km), and every LST pixel
  falling in a fire bin *or any of its eight neighbours* is dropped -- a guard
  ring 2-6 km wide, because the 1 km footprint beside a fire is contaminated by
  it. Excluded pixels reach neither the tile maxima nor the raster.
- **Latitude plausibility.** The fire mask only catches what MOD14 detected. A
  subpixel flame front below its threshold put a 78.75 C reading at 64.96 N in
  Siberia on the map. The backstop is one deliberately conservative band:
  pixels **poleward of 50 degrees** *and* **above 60.0 C** are dropped. The
  reasoning is that no verified land-surface temperature above 60 C has ever
  been observed that far from the equator -- the insolation and land cover that
  produce it do not occur there -- so anything reading higher is a fire or an
  artifact. It is one band and not a curve on purpose: the Turpan Depression at
  42.9 N legitimately exceeds 65 C, and the verified global maximum is 80.8 C at
  about 31 N. Clipping a real record would be a far worse error than leaving a
  rare artifact in, because this map's whole claim is that its numbers are real
  measurements. Do not tighten it at lower latitudes.
- **Cross-satellite corroboration.** Terra and Aqua cross the same ground about
  90 minutes apart, so a record-tier reading one satellite saw and the other
  contradicts is not two temperatures, it is one temperature and one artifact.
  See [Cross-satellite corroboration](#cross-satellite-corroboration).
- **Selection.** Tiles at or above 40.0 C are stored, plus the global top 10
  regardless of threshold, so the map is never empty in a cool northern winter.

### What the qc_note says

Every `lst_readings` row carries a `qc_note`. The suffixes are cumulative: a
tile that lost pixels to both screens carries both, in this order.

| `qc_note` | What happened |
| --- | --- |
| `mandatory QA 00/01; LST error flag <= 2K` | The mask was applied and nothing in this tile was burning or implausible |
| `...; fire-masked` | The mask was applied and threw away at least one otherwise-valid pixel in this tile, so the published maximum is the hottest *unburnt* pixel |
| `...; fire mask unavailable` | The matching fire granule could not be fetched or read, so this temperature has not been checked against fire detections |
| `...; high-latitude outlier excluded` | The latitude plausibility screen dropped at least one pixel in this tile, so the maximum was taken from what remained |
| `...; rejected by cross-satellite corroboration` | The other satellite contradicted this record-tier reading. It is a real observation and stays published here; it is barred from the permanent archive |
| `...; single-satellite, uncorroborated` | A record-tier reading no second satellite saw that day. Kept, but nothing backs it up |

A missing fire granule never fails the LST granule. Publishing an unchecked
temperature that says it is unchecked is better than publishing nothing, and far
better than implying a check that did not happen.

A tile whose *only* qualifying pixels were excluded is not written at all. An
absent tile is the honest answer: there is nothing left to report, and a cooler
neighbour's number would be an invention.

## Cross-satellite corroboration

Terra and Aqua cross the same ground about 90 minutes apart. Near local noon the
ground does not change much over that interval, so two very different readings
of the same tile on the same day are not two temperatures -- they are one
temperature and one artifact.

**The case that motivated this.** 2014-05-20, tile (12, 29) in Sudan: Terra read
**85.73 C** at 09:20 UTC and Aqua read **57.77 C** at 10:45 UTC. Ground cannot
shed 28 K in 85 minutes heading toward midday. The Terra value cleared both the
QC bitmask and the MOD14 fire mask -- a sub-detection flare or a retrieval
artifact -- and without this screen it would have become a permanent all-time
record, because the archive merges by maximum and a maximum never comes back.

For each 1-degree tile, the day's hottest reading is the candidate:

| Situation | Outcome |
| --- | --- |
| Below 78.0 C | Untouched. Below record tier a disagreement is ordinary diurnal and terrain variation |
| The other satellite is within 12.0 K | Untouched. Two instruments agree |
| The other satellite is further away than that | **The higher reading is rejected**; the cooler one stands in its place |
| No reading from the other satellite | Kept, and marked `single-satellite, uncorroborated` |

The last row is a deliberate choice. Excluding what cannot be checked would
quietly bias the archive toward whichever days happened to be cloud-free over
both overpasses -- an archive of mediocrity dressed as rigour. Keeping the
reading with the caveat attached is the honest answer.

If the surviving reading is itself record-tier, it is marked uncorroborated too:
its only possible witness just failed.

### Where it sits, and what it governs

Between the per-product loop and both once-per-day stages -- the only place it
can sit, since it needs both satellites to have been seen and everything after
it writes to surfaces that are hard or impossible to undo.

- **The all-time archive** merges the screened tiles, so a rejected reading
  never fossilizes.
- **The raster pyramid** has the rejected tile's pixels above the surviving
  satellite's maximum *dropped*, not clamped: lowering a pixel to the ceiling
  would publish a temperature no instrument recorded. Pixels are assigned to
  1-degree tiles with the same `tile_indices` the maxima used, so the raster and
  the table cannot disagree about which tile a pixel belongs to.
- **`lst_readings` keeps the rejected reading.** It is a real observation and
  the daily table is a record of observations. The daily rows are written product
  by product, before the other satellite has been seen, so a small corrective
  pass re-upserts the handful of affected rows with the corrected note once the
  verdict is in. Record-tier tiles are rare, and the upsert conflict target
  makes the pass idempotent.

A `--product` run has no second satellite in-process and marks every record-tier
tile uncorroborated, which is exactly true. The backfill runner always runs both
products per date, so real jobs get the full screen.

## Raster tiles

The 1-degree readings are a summary. The raster pyramid is the measurement at
something close to its native resolution, and it is what the live map draws.

- **Extent.** Every pixel at or above 40.0 C from every granule of both
  satellites, after QC and fire masking.
- **Grid.** Standard web-mercator XYZ, 256 px tiles, zooms 0-7. Zoom 7 is the
  base: 2^7 tiles of 256 px is 32768 px around the equator, about 1.2 km per
  pixel, which is as near the 1 km MODIS grid as a power-of-two pyramid gets
  without inventing detail. Latitudes beyond the mercator limit (85.05113) are
  clamped to the edge rather than dropped.
- **Values.** Hundredths of a degree Celsius as `int16`, with `-32768` meaning
  "not observed". Every write is a maximum, so granules and satellites can be
  folded in any order and a later cooler pass never overwrites a hotter one.
- **Coarser zooms.** Built from zoom 7 by 2x2 max-pooling, not averaging: a
  hotspot survives out to the world view instead of being diluted into its
  surroundings.
- **Colour.** A paletted PNG per tile, transparent where unobserved, using the
  five-step Kiln heat ramp. The thresholds and hex values in
  `kiln_ingest/tile_png.py` mirror `--heat-1`..`--heat-5` in `web/src/tokens.css`
  and the `step` expression in `web/src/components/LiveLayer/LiveLayer.tsx`.
  **Those three places change together**; `tests/test_tile_png.py` restates the
  colours literally so a one-sided change fails loudly.
- **Empty tiles.** Never uploaded. An absent tile is how the map says "no data
  here"; a fully transparent PNG would say the same thing more slowly.

### Bucket layout

`kiln-tiles`, public-read, service-key-write:

```
manifest.json                 the latest view's manifest
2026-08-30/7/64/63.png        {date}/{z}/{x}/{y}.png
2026-08-30/0/0/0.png
2026-08-29/...                the previous day, kept as a rollback window

manifest-alltime.json         the all-time view's manifest
alltime/7/64/63.png           the all-time pyramid
alltime-state/64/63.npy       base-zoom all-time state, exact centi-Celsius
```

The dated prefixes are transient. **`alltime/` and `alltime-state/` are
permanent** and must never be pruned.

`manifest.json` is a contract with `web/` -- exactly these keys:

```json
{
  "date": "2026-08-30",
  "generated_at": "2026-08-31T09:05:00+00:00",
  "min_zoom": 0,
  "max_zoom": 7,
  "tile_url_template": "{date}/{z}/{x}/{y}.png",
  "tile_count": 717
}
```

The frontend expands `tile_url_template` with the manifest's own `date` to build
tile URLs. Keys may be added; none may be renamed or dropped without changing
the frontend in the same commit.

The manifest is uploaded **last**, after every tile of that date is up, so a
reader can never find a manifest pointing at a half-built pyramid.

### Failure and pruning

Tiles upload on an eight-worker pool, two attempts each. Individual failures are
logged and counted: a handful of missing tiles leaves a map with a few visible
holes, which is survivable. Past 5% the stage fails, the manifest is not
published, and the process exits nonzero so the workflow goes red -- but the
`lst_readings` and `ingest_runs` rows written earlier in the run stand. The
raster stage is walled off precisely so its failure cannot cost the day its
readings; re-running the date rebuilds the tiles.

After a successful manifest upload, date prefixes older than the two most recent
are deleted. Two days is today plus a rollback window, not a history: the
frontend only ever asks for the manifest's date. Pruning is best-effort and its
failure is logged rather than fatal.

Pruning selects prefixes that parse as ISO dates, which is why `alltime/`,
`alltime-state/` and both manifests are structurally safe from it. That is not
an accident to be tidied away later: the state arrays are the only record of
every day the pipeline has processed, and deleting one silently lowers an
all-time maximum. `test_pruning_can_never_touch_the_all_time_archive` holds the
line at every retention setting, including zero.

## The all-time archive

The second view answers a different question: not "how hot was it yesterday" but
"how hot has this ground ever got, in all the days we have watched". It is a
running elementwise maximum over every day the pipeline has processed.

### The ordering that matters

Everything merged into the archive comes out of the day's base-zoom raster
store, which is painted only from pixels that already survived the fire mask and
the plausibility screen. **That order is the single most important correctness
property in this pipeline.** A merge is a maximum and a maximum is permanent: a
fire pixel admitted once becomes an all-time record that no later day can undo,
and cleaning it out means hand-editing a state array in a bucket.

`test_neither_a_fire_nor_an_outlier_can_enter_the_archive` drives a granule
carrying one pixel only the fire mask can catch and one only the plausibility
screen can catch, and fails if either screen is removed.

### State

`alltime-state/{x}/{y}.npy` holds one `int16` centi-Celsius array per base-zoom
tile -- exact temperatures, not palette ranks, because this is the archive and
everything else is derived from it. Only the tiles today touched are downloaded,
and only the tiles that actually improved are written back, so a run costs what
the day's new heat costs rather than what the archive weighs.

State is loaded with `allow_pickle=False`. A `.npy` file can carry a pickle and
pickles execute code on load; the bucket is world-readable and this process
holds the service key, so the loader must never be the thing that trusts its
input. Shape and dtype are checked too.

### Why coarser zooms merge by palette rank

Base zoom is exact. Zooms 6 to 0 are rebuilt from the tiles that changed today,
merged into the tiles already published -- and those are PNGs, holding palette
ranks rather than temperatures. Nothing is lost, because the palette is a
monotonic non-decreasing function of temperature: **the bucket of a maximum is
the maximum of the buckets**. Merging ranks gives exactly the image exact
max-pooling would have produced.

The alternative, downloading every sibling's state to rebuild a parent exactly,
would mean fetching the whole globe to rebuild zoom 0. Rank merging costs one
small GET per ancestor tile.

This depends on palette indices surviving a PNG round trip unchanged, including
sparse index sets an optimizing encoder might renumber. They do, and
`test_a_sparse_palette_survives_a_round_trip` holds it.

### `manifest-alltime.json`

```json
{
  "since": "2026-08-30",
  "through": "2026-08-31",
  "generated_at": "2026-08-31T09:05:00+00:00",
  "min_zoom": 0,
  "max_zoom": 7,
  "tile_url_template": "alltime/{z}/{x}/{y}.png",
  "tile_count": 717
}
```

`since` is the earliest date ever merged, carried forward from the previous
manifest because nothing else remembers it; backfilling an earlier date moves it
back. `through` is the date just ingested, and it is refreshed even on a day
that broke no records, so the frontend can say how current the archive is.
`tile_count` is a running total grown by the tiles each run creates -- counted
rather than listed, because enumerating a permanent bucket daily would cost more
than the run that fills it.

### The all-time table

`kiln.alltime_readings` holds one row per place, ever: the conflict target is
`(tile_lat, tile_lon)` with no date and no product, because a tile's record
belongs to whichever day and satellite set it. `record_date` says which day that
was.

A row is written when both hold: today's reading beats what the archive has for
that tile, and the improved value clears the same bar the daily table uses
(40 C, or the global top 10). A tie is not an improvement -- rewriting the row
would move `record_date` onto a day that did not set the record.

This step runs even when no raster tile changed. The pyramid only carries pixels
at or above the display threshold, so a cooler place can still set a record the
table should carry.

### Failure and re-running

The stage writes in the order display tiles, state, rows, manifest, chosen so
that re-running the date after any failure repairs it. State is written after
the tiles it describes because state is what marks a tile as done: advancing it
first would make the next run see nothing to do and leave the pyramid
permanently stale.

State uploads have **no failure tolerance**, unlike display tiles. A state object
that does not land loses that tile's improvement for good, whereas failing the
run means someone re-runs the date -- and re-running merges to exactly the same
archive, because a maximum is idempotent.

### Scale

A synthetic global hot belt (30 million pixels at or above 40 C across the
Sahara, Arabia, Iran, the Mojave, central Australia, the Kalahari and central
Asia) produces 473 active base tiles holding 59 MB, 717 PNGs totalling 0.8 MB,
and a peak RSS of 122 MB -- against roughly 7 GB on the Actions runner.
Real days are patchier than that belt and so will use somewhat more tiles; the
pipeline logs the active tile count every run and warns past 8000 (about 1 GB),
which is the point at which the runner's headroom would be worth checking.

## Setup

```bash
cd ingest
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

`pyhdf` needs libhdf4. Prebuilt wheels cover macOS arm64 and most Linux
runners; where none matches, install the headers first
(`sudo apt-get install -y libhdf4-dev`, or `brew install hdf4`).

If `pyhdf` will not install at all, `requirements-dev.txt` alone is enough to
run the suite: it deliberately does not pull in `requirements.txt`, and nothing
outside `kiln_ingest/granule.py` imports `pyhdf` (that module imports it
lazily, inside the read function).

## Secrets

Two environment variables, both stored as GitHub Actions repository secrets and
never committed:

| Variable | What it is | Where to get it |
| --- | --- | --- |
| `EARTHDATA_TOKEN` | NASA Earthdata Login bearer token, used on granule downloads | <https://urs.earthdata.nasa.gov/> then Generate Token |
| `SUPABASE_SERVICE_KEY` | `service_role` key for the shared tortoise project | Supabase dashboard, project `wdvguesfxcxxatzpirvy`, API settings |

The service key bypasses RLS and writes the `kiln-tiles` bucket. It belongs only
in GitHub Actions secrets and, for local runs, in your shell environment for the
length of the command.

### Token expiry

`EARTHDATA_TOKEN` is a JSON web token with a fixed lifetime, and when it lapses
every download returns 401 with nothing saying why. The CLI reads the token's
own `exp` claim before doing any network work -- decoding the payload only, not
verifying the signature, which is NASA's job -- and acts on it:

- **More than 14 days left:** an INFO line naming the date.
- **Within 14 days:** a WARNING naming the date and the two places the token
  lives, on every daily run until someone acts.
- **Already expired:** the run stops immediately with exit code 2, rather than
  producing several hundred authentication failures first.
- **Not a decodable JWT:** a warning, and the run proceeds. The check exists to
  explain failures, not to invent them.

Renewing means generating a token at <https://urs.earthdata.nasa.gov/>, then
updating **both** Doppler (`kiln/prd`) and the GitHub repository secret. Missing
the second is how the cron goes dark while local runs keep working.

## Usage

```bash
# Yesterday UTC, both satellites, writing rows and raster tiles
python -m kiln_ingest

# One date, one product
python -m kiln_ingest --date 2026-08-30 --product MOD11_L2

# Local smoke test: three granules, no remote writes, tiles on disk
EARTHDATA_TOKEN=... python -m kiln_ingest \
  --date 2026-08-30 --product MOD11_L2 --max-granules 3 --dry-run

# A historical date, narrowed to one region (see Historical backfills)
EARTHDATA_TOKEN=... python -m kiln_ingest \
  --archive --date 2019-07-15 --bbox 55,28,62,33 --max-granules 2 --dry-run
```

`--dry-run` skips every Supabase write, including the `ingest_runs` bookkeeping
and every Storage upload. It prints the selected 1-degree tiles and writes the
raster pyramid to `--tiles-dir` (default `out-tiles/`) in the same layout the
bucket uses, manifest included, so the output is servable as-is. The all-time
stage never runs under `--dry-run`; it prints what it would have merged and says
plainly that it cannot know which tiles would set records without reading the
stored state. It still downloads granules, so it still needs `EARTHDATA_TOKEN`.
`--max-granules` caps downloads; without it a full day is a few hundred granules
per satellite.

Note that the raster pyramid is per *day*, not per satellite: the maximum across
both passes. Running with `--product` produces a pyramid from that satellite
alone, which is right for a smoke test and wrong for a backfill -- backfill a
date by running both products in one invocation, as the workflow does.

Exit codes: `0` if at least one product produced data *and* both the raster and
all-time stages published, `1` if every product failed or either stage did, `2`
if a required environment variable is missing or `EARTHDATA_TOKEN` has expired.

## What gets written

`kiln.lst_readings`, upserted on `(reading_date, product, tile_lat, tile_lon)`
with `resolution=merge-duplicates`, so re-running a date is safe and idempotent.

`kiln.ingest_runs` gets one row per product per run: inserted as `running`
at the start, patched at the end to

- `succeeded` -- every discovered granule was downloaded and parsed
- `partial` -- some granules failed but at least one succeeded
- `failed` -- nothing succeeded, or discovery itself failed

The frontend reads the latest run to decide whether to mark the map stale.

`kiln.alltime_readings` gets a row for every tile whose all-time record improved
today, upserted on `(tile_lat, tile_lon)`.

`kiln-tiles` gets the day's raster pyramid and a fresh `manifest.json`, plus the
merged all-time state, pyramid and `manifest-alltime.json`, as described under
[Raster tiles](#raster-tiles) and [The all-time archive](#the-all-time-archive).
Storage writes are not covered by `ingest_runs`; a stage failure shows up as a
red workflow run with the rows for that date already in place.

A single corrupt or unavailable granule is logged and skipped; downloads retry
three times with exponential backoff. A missing or unreadable *fire* granule is
weaker still -- it is logged, noted on the affected tiles, and the LST granule is
processed anyway. Granules are written to a temp directory and deleted
immediately after processing, since the Actions runner disk is finite.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite needs no network, no credentials, and no real granule files.

- `test_science.py` -- the science core against synthetic numpy arrays: fill
  masking, QC bit handling, Kelvin conversion, geolocation upsampling, per-tile
  maxima, threshold and top-N selection.
- `test_fire_mask.py` -- fire binning and the guard ring against synthetic fires
  on a synthetic LST grid: adjacent pixels excluded, non-adjacent ones survived,
  and each of the three `qc_note` states produced by the case that causes it.
- `test_plausibility.py` -- the latitude band's four defining cases: a Siberian
  78 C rejected, a warm Arctic 45 C kept, Turpan at 42.9 N kept, and the 80.8 C
  global record at 31 N kept. Plus the note a tile gets when it loses a pixel.
- `test_corroboration.py` -- the Sudan case, the four outcomes and both
  constants' boundaries, the raster ceiling dropping rather than clamping, and
  the CLI seam where the screen reaches both stages.
- `test_raster.py` -- mercator projection against known coordinates, the
  high-latitude clamp, max-into-store accumulation, and 2x2 max-pooled parents.
- `test_token_expiry.py` -- the JWT `exp` claim read back, the renewal window's
  edge, an expired token stopping the run, and every malformed-token shape
  falling through to a warning rather than a failure. `now` is a parameter, so
  none of it depends on the clock.
- `test_tile_png.py` -- encodes tiles and decodes them back with Pillow,
  asserting the palette colours and transparency against the ramp written out
  literally, so a drift from `tokens.css` is caught rather than shipped.
- `test_cmr.py` -- CMR query building and feed parsing.
- `test_archive.py` -- archive versus NRT provider selection, the pinned
  collection, bounding-box mapping and the OR option, granule-name parsing
  across both feeds' naming styles, and the `--bbox` flag's validation.
- `test_download_redirects.py` -- the per-hop token policy, including the
  lookalike hosts (`evil-nasa.gov`, `nasa.gov.attacker.com`) and plaintext, and
  the redirect loop that drops the token at CloudFront and restores it on
  return to NASA.
- `test_supabase_payload.py` -- row shapes, upsert conflict target, run status
  resolution.
- `test_storage_payload.py` -- both manifests' exact shapes, tile paths, the
  prune policy including its structural exclusion of the all-time prefixes, and
  upload retry and tolerance against a fake bucket.
- `test_alltime.py` -- state merging and its idempotence under a re-run, the
  ancestor rebuild, rank merging proved equal to exact max-pooling, the
  improved-only row selection, and the manifest's `since` bookkeeping.
- `test_cli_alltime.py` -- the stage end to end against a fake bucket: the write
  ordering that makes a re-run repair a failure, zero tolerance on state
  uploads, and the sequencing test that fails if either screen is removed.
- `test_cli.py` -- orchestration against a stub HTTP session: dry-run writes
  nothing, one bad granule yields `partial`, all bad yields `failed`.
- `test_cli_raster.py` -- the two new stages wired up: fire granules paired by
  overpass, both satellites folded into one pyramid, and a raster failure
  reported through the exit code rather than raised.
- `test_granule_roundtrip.py` -- writes a miniature HDF4 granule and reads it
  back through the real reader. Skipped where `pyhdf` is unavailable.

## Scheduling

`.github/workflows/ingest.yml` runs daily at 09:00 UTC, by which point the
previous UTC day's NRT granules are complete. Terra and Aqua run in a single
step, because the raster pyramid is the maximum across both passes and they have
to accumulate into one process. Either satellite may still fail on its own
without failing the workflow -- the CLI's exit code carries that -- but losing
both, or failing to publish the tiles, turns the run red. Per-satellite detail
lives in the step log and in `kiln.ingest_runs`. `workflow_dispatch` takes an
optional `date` input for backfilling a single day.

`.github/workflows/ingest-tests.yml` runs the suite on any change under
`ingest/`.

## Layout

```
ingest/
  kiln_ingest/
    science.py       pure core: scaling, QC, geolocation, the three screens, aggregation
    raster.py        pure numpy: mercator projection, tile accumulation, pyramid
    alltime.py       pure numpy: archive state merging, incremental pyramid, row policy
    tile_png.py      paletted PNG encoding (the only Pillow import, lazy)
    cmr.py           granule discovery, query building, LST/fire pairing, archive mode
    download.py      streaming downloads with retry and the redirect token policy
    granule.py       HDF4 reading (the only pyhdf import, lazy)
    supabase_io.py   PostgREST payloads and writes
    storage_io.py    tile, state and manifest transfers, prune policy
    cli.py           argument handling, orchestration, the once-per-day stages
  tests/
```

The two lazy imports are load-bearing: `pyhdf` needs libhdf4 and Pillow is only
needed to encode, so neither sits on the science core's import path and the
suite runs without them. `raster.py` deliberately knows nothing about PNGs and
`tile_png.py` knows nothing about tiles-as-objects, which is what lets the
projection maths and the colour contract be tested separately.
