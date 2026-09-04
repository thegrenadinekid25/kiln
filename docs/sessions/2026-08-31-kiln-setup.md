# 2026-08-31 — Kiln: project setup through first deploy

## Where things stand

Everything is built and live except real satellite data, which is blocked on one
credential (below). 12 of 13 Troth tasks are done.

- Live site: https://kiln-bay.vercel.app (Vercel project `kiln`, prod)
- Repo: thegrenadinekid25/kiln (private), local at /Volumes/tortoise/projects-local/kiln
- Troth cloud project: kiln (fc5d69b3-208a-4754-8272-6f9903ce5d64)
- Data: `kiln` schema in the shared tortoise Supabase (wdvguesfxcxxatzpirvy),
  exposed to the Data API additively (Management API PATCH — never `config push`).
  record_holders seeded (5 cited rows); lst_readings/ingest_runs empty until the
  pipeline runs.

## Outstanding

1. ~~EARTHDATA_TOKEN~~ — DONE later this same day. Token in Doppler (project
   `kiln`, config prd) and as GH repo secret EARTHDATA_TOKEN; expires ~2026-10-30
   (regenerate at urs.earthdata.nasa.gov and update both stores). Two gotchas
   learned: (a) LANCE archive downloads return the login page / "HTTP Basic:
   Access denied" until the account has logged into nrt3.modaps.eosdis.nasa.gov
   once in a browser — no explicit EULA page, the login itself authorizes the
   app; (b) curl needs --location-trusted for the same-host redirect, python
   requests is fine. Science path validated against 5 real granules (dry run);
   first full run dispatched as GH Actions run 33435450028.
2. **Dallol source tier** — Troth bug 8c5f6bb9: the avg-annual record only has a
   Guinness-tier source; spec demands NASA EO/WMO/peer-reviewed. Currently shown
   with the tier flagged in its notes. Connor decides: keep or drop.
3. **Name trademark pass** — informal scan found kiln.digital (UK data-viz studio
   known for interactive maps — closest collision), Kiln AI (Chesterfield
   Laboratories, trademarked), and the Kiln crypto-staking company. Real
   clearance needs a professional search if the product gets serious.

## Infrastructure refs

- Supabase service key: Management API `GET /v1/projects/wdvguesfxcxxatzpirvy/api-keys?reveal=true`,
  authenticated with a personal access token (not committed anywhere — kept in local
  credential storage). `~/.supabase/access-token` is stale; don't use it.
- `supabase db query --linked --project-ref wdvguesfxcxxatzpirvy -f file.sql` works
  with no local link.
- Vercel env: VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY set on production.
  GH Actions secret SUPABASE_SERVICE_KEY is set.
- Cost: $0/mo (shared Supabase schema, GH Actions free minutes, Vercel free).

## Gotchas discovered

- `troth projects list` reads only the local registry; cloud-only projects need a
  Supabase query via the troth repo's auth helpers. Linking an existing cloud
  project locally: hand-write .troth/config.yaml with cloud.project_id, then
  `troth init --name <name>` detects the link and refreshes CLAUDE.md.
- web/ uses TS project references: bare `tsc --noEmit` at the root checks nothing;
  `bun run build` (tsc -b) is the real typecheck.
- Terra (MOD11_L2) and Aqua (MYD11_L2) write separate rows per tile per date;
  the frontend dedupes client-side keeping the hottest (useKilnData.ts).
- MapLibre stamps aria-label="Map marker" onto custom marker elements; set the
  descriptive label after addTo() (RecordsLayer.tsx).
- Heat ramp lives in two places on purpose: tokens.css (--heat-1..5) and the
  MapLibre step expression in LiveLayer.tsx. Change both together.

## Addendum (same day, later): 1 km raster + fire mask shipped

All 16 tasks done; every spec green. The live layer now renders a z0-7
web-mercator PNG pyramid (~1.2 km/px at max zoom) from the public kiln-tiles
Storage bucket, manifest-driven, pruned to 2 dates. Fire mask: MOD14/MYD14
paired by overpass (short names have NO _L2 suffix in CMR), 0.02-degree
binning with 8-neighbor guard ring. The 1-degree rows remain solely as click
targets + hotspot/staleness data; frontend paginates past the 1000-row
PostgREST cap (was silently truncating before). Known residual: bug cd0c4f2e —
one implausible Siberian reading (78.75C at 65N) below MOD14 detection
thresholds survives the mask.

## Addendum 2: two views + historical scan program

Product now has "Most recent" / "All-time" views (satellite-only decision:
station records removed; three cited MODIS records remain as context markers).
All-time archive is live and clean (screens run BEFORE merge; the Siberian fire
pixel never entered it). USPTO scan: five LIVE "KILN" marks in classes 9/42 —
rename before launch.

Historical scan (spec historical-scan): pass 1 = scan/ CMG sweep of the whole
MOD11C1/MYD11C1 record, running locally in background (work-dir
~/projects-local/kiln-scan-work, resumable done-logs, ~1 day wall time). Pass 2
tooling ready: `kiln_scan worklist` (record-date grids -> date+bbox jobs) and
`kiln_scan backfill` (drives ingest CLI with --archive --bbox; needs
EARTHDATA_TOKEN + SUPABASE_SERVICE_KEY env). When the sweep completes: generate
worklist at bar 60, run backfill, alltime archive becomes a true 24-year scan.
Canary results on record: Lut 70.19C CMG on 2019-07-15; 74-77C at 1km same day.

## Addendum 3 (2026-09-02): the 24-year archive is complete

Backfill: 651/651 jobs, 0 failed. Archive spans 2000-04-17 to present, 2,148
all-time tiles, 103 cross-satellite corroboration rejections (the Sudan 85.73C
ghost rejected in production on job 3). Live site current on both views.

The top of the archive, with eyes open:
- 90.37C, 13.59N 40.67E, 2017-05-10 — ERTA ALE'S LAVA LAKE (corroborated,
  because a volcano is hot on every pass; open product decision in bug a6a885e8).
- 87.97C Queensland Sept 2000 — pre-Aqua, can never be corroborated; kept+marked.
- Corroborated meteorological champions, all above the published 80.8 record:
  84.67C central Australia (2014-12-15), 83.53/83.49C Queensland, 83.17C Iraq
  (2021-08-01), 82.93C Sudan (2015-05-11), 82.83C Rub al Khali (2015-05-23).
- Claim discipline: these are "hottest QC-passing, fire-masked, cross-satellite-
  corroborated 1 km readings in our sweep" — method-transparent witness, never
  "new world record" (Zhao et al. methodology differs).

## Addendum 4 (2026-09-02): resume runbook after the anomalies/names pause

State at pause: anomaly routing (4 screens), geocoding, volcano list (41 GVP-verified
vents seeded), leaderboard page, air estimates (Mildrexler 2011 barren relation),
our-readings-only records — ALL shipped and committed. Geocode backfill was running
detached (pid noted in session; ~8,103 cells at 1 req/s ~ 2.25h; safe to interrupt,
re-run is a no-op: `cd ingest && SUPABASE_SERVICE_KEY=<from mgmt API> EARTHDATA_TOKEN=<scratch>
.venv/bin/python -m kiln_ingest.geocode --backfill`).

REMAINING SEQUENCE:
1. (If interrupted) finish/resume geocode backfill — primes kiln.place_names cache.
2. FINAL ARCHIVE REBUILD (user-sanctioned wipe, done twice before):
   a. wipe: python3 <scratchpad>/wipe-alltime.py equivalent (delete storage prefixes
      alltime/, alltime-state/, manifest-alltime.json), truncate kiln.alltime_readings,
      truncate kiln.anomaly_readings, rm kiln-scan-work/backfill_done.txt
   b. dispatch daily GH workflow for yesterday (seeds clean archive + fresh manifest)
   c. after it completes: relaunch scan backfill detached:
      cd scan && SUPABASE_SERVICE_KEY=... EARTHDATA_TOKEN=... nohup .venv/bin/python -m
      kiln_scan backfill --jobs /Volumes/tortoise/projects-local/kiln-scan-work/jobs-72.json
      --work-dir /Volumes/tortoise/projects-local/kiln-scan-work >> ...log &
   d. ~14h; verify: anomaly_readings has volcanic rows (erta-ale), alltime max is
      corroborated weather, place names present, site both views + leaderboard.
3. DEMO VIDEO: script for sign-off first, narrated with an ElevenLabs voice.
   Records live site scenes (bunx playwright, record video, 1440x900), ffmpeg
   mux per-scene VO. ffmpeg installed.

Token: EARTHDATA_TOKEN expires ~2026-10-30 (Doppler kiln/prd + GH secret).
