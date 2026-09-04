# kiln/scan — historical MODIS LST scanner (Pass 1)

A standalone batch tool that sweeps **every day of the MODIS land-surface-
temperature record** looking for places that have ever been extremely hot, and
records both the days they were hot on and a running all-time maximum for every
cell on Earth.

It is deliberately separate from `ingest/`, the daily near-real-time pipeline.
They read different products from different NASA hosts on different schedules,
and they share no code — the science constants and the downloader are copied
rather than imported, so a change to one cannot break the other silently. Any
change to the QC bar or the plausibility band belongs in both; the copies say so
in their own comments.

## The two-pass design

Scanning 24 years of MODIS at 1 km is not feasible on one machine: the L2 swath
archive is hundreds of terabytes. So the all-time layer is built in two passes.

**Pass 1 — this tool.** Sweep every day at 0.05 degrees (~5.6 km at the equator)
using the daily global CMG products, MOD11C1 (Terra) and MYD11C1 (Aqua). One
file per day, 40–70 MB, one global grid of 7200 × 3600 cells. Coarse, but it
covers the whole record and the whole planet, and it is enough to say *which
(place, day) pairs are worth a closer look*. Output: a per-year CSV of every
cell that reached the bar on a given day, plus a running all-time maximum grid
with the date that set each cell.

**Pass 2 — driven from here, executed by `ingest/`.** Take the candidates Pass 1
found and re-measure just those places on just those days against the 1 km
MOD11_L2 / MYD11_L2 swaths. A 5.6 km cell averages a lot of ground; a 1 km look
is what turns a candidate into a record Kiln can publish. The `worklist` and
`backfill` subcommands below do the planning and the driving; the measuring is
the daily pipeline's `--archive --bbox` mode, invoked as a subprocess.

`scan`, `summarize` and `worklist` write only local files. `backfill` without
`--dry-run` upserts into the production all-time archive, because the ingest CLI
it drives does.

## What each day goes through

1. Ask CMR for the day's granule. A day with none is normal (2000-02-29 has no
   MOD11C1 file) and is recorded as done rather than retried forever.
2. Download the HDF4-EOS file with an Earthdata bearer token.
3. Read `LST_Day_CMG` (uint16) and `QC_Day` (uint8).
4. **Scaling.** Read `scale_factor` off the SDS and refuse the file if it is not
   0.02. Guessing the units is worse than not running.
5. **Validity.** Drop fill cells and anything outside the file's own declared
   `valid_range`, then outside a physical band of -150 to 200 C.
6. **QC mask.** Keep only cells where mandatory QA is 00 or 01 (LST actually
   produced) and the LST error flag is 00 or 01 (average error <= 2 K).
7. **Plausibility screen.** Drop cells that are both poleward of 50 degrees and
   hotter than 60 C — the same physical rule the daily pipeline applies, for the
   same reason: an undetected subpixel fire once put a 78.75 C reading in
   Siberia on Kiln's map.
8. Append every surviving cell at or above the bar to that year's CSV.
9. Fold the day into the all-time grid: elementwise maximum, with the date grid
   updated wherever the maximum moved.
10. Delete the downloaded file, then mark the day done.

## Install

`pyhdf` needs the libhdf4 development headers. On macOS, `brew install hdf4`;
on Debian/Ubuntu, `apt-get install libhdf4-dev`.

```
cd scan
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The test suite never imports pyhdf — `kiln_scan.hdf` imports it lazily inside its
read function — so a machine without libhdf4 can still run everything except a
real scan:

```
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Run

Scanning needs an Earthdata login token in `EARTHDATA_TOKEN` (generate one at
<https://urs.earthdata.nasa.gov/profile>).

```
export EARTHDATA_TOKEN=...

# The whole Terra record.
python -m kiln_scan --product MOD11C1 --years 2000-2026 --work-dir ./work

# A slice, for a first look.
python -m kiln_scan --product MYD11C1 --years 2019 --days 30 --work-dir ./work

# One specific day.
python -m kiln_scan --product MOD11C1 --start 2019-07-15 --end 2019-07-15 \
    --work-dir ./work

# Read back what a scan produced.
python -m kiln_scan summarize --product MOD11C1 --work-dir ./work --top 20
```

`scan` is the default subcommand, so the first form needs no verb.

Useful flags: `--bar` sets the candidate threshold in Celsius (default 55);
`--max-error-class` loosens or tightens the QC error bar; `--flush-every` trades
disk writes against how many days a kill costs; `--keep-granules` leaves the
downloaded file on disk for debugging a single day.

`summarize --export-npy` additionally writes the all-time grid as two plain
`.npy` files (`alltime_cmg_<product>.npy`, `alltime_dates_cmg_<product>.npy`)
for Pass 2 or anything else that would rather not open an archive.

`summarize` without `--product` reads both products' CSVs together and reports
one ranked list. Where Terra and Aqua both flagged the same cell on the same
day it keeps the hotter reading, because the list exists to name the
(place, day) pairs Pass 2 should revisit and revisiting one twice buys nothing.
The per-product CSVs keep both values.

Add `--verbose` to a scan for a per-day line: cells kept, cells dropped by QC,
cells dropped as implausible, and that day's own hottest cell.

## Pass 2: worklist and backfill

Once a sweep has run (it does not have to have finished — the grid is valid at
every point), `worklist` turns it into refinement jobs and `backfill` runs them.

```
# Plan: every cell whose all-time max reached 60 C, grouped into (date, region) jobs.
python -m kiln_scan worklist --work-dir ./work --bar 60 --out jobs.json

# Execute, hottest job first. Dry run first; it downloads but writes nothing.
export EARTHDATA_TOKEN=... SUPABASE_SERVICE_KEY=...
python -m kiln_scan backfill --jobs jobs.json --work-dir ./work --limit 5 --dry-run
python -m kiln_scan backfill --jobs jobs.json --work-dir ./work
```

**How jobs are formed.** A cell's record date is the day its maximum was
observed, and that is the only day worth refining for it: measuring a
neighbouring date would measure a different thing and attribute it to the
record. So cells are grouped by date first, and clustered geographically only
within a date. Within a date, cells within `--merge-degrees` (default 3) of each
other share a bounding box, transitively, and each final box is grown by
`--pad-degrees` (default 0.5) because the 1 km peak a 5.6 km cell stands for can
sit anywhere inside it. One date routinely yields several far-apart boxes — the
Sahara and the Sonoran Desert can peak on the same day, and one box around both
would fetch the Atlantic.

Jobs come out hottest first, so a `--limit`ed run spends its budget on the days
most likely to hold a record.

**Longitude is linear.** Clustering, padding and the union all work in plain
degrees, so a cluster straddling the antimeridian comes out as two jobs rather
than one, and any box that padding pushed past ±180 is split in two. No box a
worklist emits ever crosses the antimeridian; `backfill` re-checks that when it
reads the file and refuses the run rather than sending a malformed box to the
archive.

**What `backfill` runs.** For each job, the ingest CLI once per product,
sequentially, in the ingest checkout:

```
<ingest venv python> -m kiln_ingest --date <date> --product <product> --archive \
    --bbox=W,S,E,N [--bbox=...] [--dry-run]
```

`--archive` always: every worklist date is historical and LANCE only holds a few
days. `--bbox=` in the equals form always: any western-hemisphere box starts
with a minus sign, which argparse would read as a flag if it were a separate
argument.

**Satellite availability is enforced.** Aqua's first MYD11_L2 granule is
2002-07-04 and Terra's first MOD11_L2 is 2000-02-24, both verified against CMR.
A job dated before a satellite was flying skips that product rather than asking
the archive for data that cannot exist. Without this, every pre-2002 job — a
large share of the record — would be recorded as failed.

**Resumability.** `work/backfill_done.txt` records one line per attempt,
`<date> <product>=<exit code> ...`, fsynced as it goes. A date counts as done
only when every product it ran exited 0, so a failed job is retried on the next
run while a succeeded one is skipped. Failures are logged and the run continues;
the run exits nonzero only if more than 20% of jobs failed, which is the
signature of a broken configuration (expired token, missing service key) rather
than a few bad days.

The ingest CLI's own output streams through rather than being captured, so a
long backfill shows progress instead of going silent for hours. Redirect it if
you want to grep it later.

**The fire mask Pass 1 lacks.** The CMG lineage has no daily fire product, so
Pass 1's only defence against a burning pixel is the coarse plausibility band.
The 1 km pipeline pairs every granule with MOD14/MYD14 and drops fire pixels
plus a guard ring. That asymmetry is the main reason a Pass 1 candidate is a
lead and not a record: a hot cell in fire country is exactly what the backfill
exists to check.

### What lands in the work directory

```
work/
  done_MOD11C1.txt              one ISO date per completed day
  alltime_cmg_MOD11C1.npz       max_centi int16 + date_int int32, both 3600x7200
  summary_MOD11C1.json          written by `summarize`
  backfill_done.txt             written by `backfill`: date + per-product exit codes
  candidates/
    candidates_MOD11C1_2019.csv date,cell_lat,cell_lon,max_c
  granules/                     empty between days; one file at a time
```

## Resumability

A 24-year sweep will be interrupted. The contract is that **an interrupted run
loses at most the day it was working on**, and that rerunning the same command
picks up where it stopped.

What guarantees it, per day, in this order: the candidate rows are written and
fsynced, the all-time grid is saved (written to a temporary file and moved into
place atomically, both arrays in one file so a maximum can never be paired with
the wrong date), and only then is the date appended to the done-log. Nothing is
ever marked done before every artifact holding it is durable. The fold is an
elementwise maximum, so re-folding a day is a no-op; the only thing a crash can
duplicate is one day's CSV rows, and the summary reader deduplicates on
(day, cell).

`--flush-every N` saves the grid every N days instead of every day. It writes
less — the grid is 155 MB — at the cost of a kill redoing up to N days. The
done-log still never runs ahead of the grid at any setting.

A day whose download or parse fails is logged and skipped, and is *not* marked
done, so the next run tries it again. One unreachable day does not end the sweep.

## Disk and bandwidth

Measured on the real 2019-07-15 granules:

| | MOD11C1 (Terra) | MYD11C1 (Aqua) |
|---|---|---|
| Granule size | 46.7 MB | 48.5 MB |
| Days in record (to 2026-08) | ~9,680 | ~8,820 |
| Total download | ~450 GB | ~430 GB |
| Wall time per day (this machine) | ~5 s | ~6 s |

**Downloads**: roughly 880 GB across the full record for both products, but only
one file is on disk at a time — each is deleted as soon as it has been folded
in. Peak disk from downloads is one granule.

**The all-time grid** is 155 MB per product and is rewritten every day by
default, so a full sweep writes about 1.5 TB per product to the work disk. On an
external SSD that is worth thinking about; `--flush-every 20` cuts it by twenty.

**The candidate CSVs are the thing to size before you start.** At the default
bar of 55 C, one Terra day in July produced 99,010 rows (3.2 MB) and one Aqua
day produced 256,766 (8.2 MB) — Aqua's early-afternoon overpass catches ground
much closer to its daily peak than Terra's mid-morning one. Mid-July is near the
annual peak, so extrapolating it across the record (~30 GB Terra, ~72 GB Aqua)
is an upper bound rather than an estimate; the real figure is lower, and still
tens of gigabytes. Either way it is far more than Pass 2 can usefully consume.

Cell counts from that single Terra day show how fast the bar thins it out:

| Bar | Cells that day |
|---|---|
| 50 C | 290,196 |
| 55 C | 99,010 |
| 60 C | 15,594 |
| 65 C | 592 |
| 70 C | 2 |

The default is 55 because that is the brief's definition of "extremely hot" and
a low bar can be filtered afterwards while a high one cannot be un-filtered. But
for a first full sweep, **`--bar 65` is the recommendation**: it keeps the
candidate set in the low gigabytes, and the all-time grid records every cell's
true maximum regardless of the bar, so nothing about the record itself is lost
by raising it.

## What this measures, and what it does not

**Land-surface temperature, not air temperature.** These are two different
physical quantities. Ground in the Lut Desert reaches temperatures no
thermometer in a Stevenson screen ever will. Kiln shows satellite LST and says
so; station air-temperature records are deliberately out of scope.

**Clear-sky only, and that is a real bias.** MODIS measures the ground with a
thermal infrared instrument, which cannot see through cloud. The QC mask drops
every cell flagged "not produced (cloud)". So an all-time maximum built this way
is the maximum *over the days that happened to be clear at the overpass*. In the
deserts this matters least — they are clear most days — but a place that is hot
and frequently cloudy is systematically under-represented here, and a cell's
all-time maximum is a floor on its true maximum, never a ceiling. Anything built
on this output has to say so.

**One instant per day, not a daily maximum.** Each cell's value is the
temperature at that satellite's overpass, not the hottest moment of the day.
Terra crosses around 10:30 local time, Aqua around 13:30. Neither is
peak ground heating, though Aqua is much closer, which is why the two products
disagree on where the day's hottest place was — and why both are scanned.

**Coarse.** A 0.05-degree cell is about 5.6 km at the equator and averages
everything inside it. A candidate from this pass is a lead, not a record.

**The plausibility screen is a floor, not proof.** It only removes the
physically impossible (above 60 C poleward of 50 degrees). A candidate inside
the band can still be a fire, and the CMG lineage has no daily fire mask to
check it against. That check belongs to Pass 2.

## Verification

Live smoke, run 2026-08-31 against the real granules for 2019-07-15:

- MOD11C1 (Terra): 6,135,445 cells kept after masking, global maximum
  **70.19 C at 29.575 N, 59.225 E** — the Lut Desert, Iran.
- MYD11C1 (Aqua): 6,025,919 cells kept, global maximum **71.77 C at 33.625 N,
  55.875 E** — the Dasht-e Kavir, about 400 km north-west, and hotter, as the
  later overpass should be.

Both land in the region that holds the published global LST maximum (80.8 C,
Zhao et al. 2021), at values consistent with a 5.6 km average of it.
