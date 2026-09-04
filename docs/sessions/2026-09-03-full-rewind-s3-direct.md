# Full daily rewind: S3-direct download + launch checklist

Status as of 2026-09-04, ~03:50 UTC. Picks up from the git-history-rewrite /
public-launch session (2026-08-31 doc). Troth request: `cf024cf9`.

## Where things stand

- Repo is public (`gh repo edit ... --visibility public`, confirmed live).
- Supabase project creation is still blocked by a platform outage (checked
  twice, most recently ~02:xx UTC 2026-09-04; status page said "incremental
  improvements" but the Management API still returned "Partial System
  Outage").
- Decided against GitHub Actions for the historical compute: ephemeral
  runners have no clean path back to local/persistent storage.
- Decided against running the full rewind on this laptop: a timed real
  single-day pilot (2020-07-15, both satellites, global, no bbox) measured
  ~5.3s/granule regardless of file size (fixed per-request latency, not
  bandwidth), ~10 min/product/day, ~20 min/day total, single-threaded. That's
  **~134 days of single-threaded work** for the full ~9,684-day record, or
  4-8 days even with 16-32 local parallel workers -- not "quickly."
- User chose: rent AWS compute instead (the plan originally priced before
  the Supabase outage forced the local-staging pivot).

## The real lever: direct S3 access (verified, not assumed)

LP DAAC's archive granules (`data.lpdaac.earthdatacloud.nasa.gov`, what the
pipeline already pulls from in `--archive` mode) live in S3 in `us-west-2`.
NASA's docs and a live test both confirm:

- Credential exchange (`GET https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials`
  with the Earthdata bearer token) **works from anywhere** -- tested from this
  laptop, got real temporary AWS credentials back.
- The actual S3 read is **region-gated to `us-west-2`** -- tested `head_object`
  against `s3://lp-prod-protected/...` from this laptop with those exact
  credentials: `403 Forbidden`. From inside `us-west-2` it will work.
- Buckets confirmed from a real CMR response: `lp-prod-protected` (protected
  granules) and `lp-prod-public` (browse imagery).

This matters because the pilot's bottleneck was per-request latency (a 1-tile
and a 700-tile granule took about the same time over HTTPS), not bandwidth --
exactly what direct same-region S3 access fixes, and generic cheap VPS
providers (Hetzner, Contabo) get no benefit from at all since they're not in
AWS.

## What was built this session (committed, tested, not yet run for real)

- `ingest/kiln_ingest/cmr.py`: `GranuleRef.s3_url` (direct-S3 link, extracted
  from CMR's `s3#` rel, bucket-allowlisted to `lp-prod-protected`/
  `lp-prod-public`); `time_key_map` now returns `dict[str, GranuleRef]`
  instead of `dict[str, str]` so fire-mask granules keep their S3 link too.
- `ingest/kiln_ingest/download.py`: `S3Fetcher` (credential exchange +
  boto3 client, auto-refreshes ~5 min before the ~1hr expiry) and
  `download_granule_auto()`, which tries S3 once and **self-disables for the
  rest of the process** on any failure, falling back to the existing HTTPS
  path. Safe to pass `--s3-direct` unconditionally, including on a machine
  that isn't in AWS at all -- it just costs one failed attempt, then behaves
  exactly like today.
- `ingest/kiln_ingest/cli.py`: new `--s3-direct` flag, threaded through
  `run_product` -> `build_reducer`/`process_granules` -> the download calls.
- `ingest/requirements.txt`: added `boto3==1.43.88`.
- `scan/kiln_scan/backfill.py`: `ingest_command`/`run_job` gained
  `global_fetch` (emit no `--bbox` at all -- the rewind's job shape, a whole
  day, not a refined region; the existing "empty bboxes is a bug" guard for
  the ordinary refinement backfill is unchanged and still enforced) and
  `s3_direct` passthrough.
- `scan/kiln_scan/full_rewind.py` (new): the full-rewind's own job list
  (`full_range_jobs`, one placeholder `Job(bboxes=())` per calendar day,
  `DEFAULT_START` = 2000-02-24) and threaded runner (`run_full_rewind`,
  reuses `BackfillLog` for a resumable done-log, `FAILURE_RATE_LIMIT` guard,
  and `run_job`'s single-invocation-covers-both-satellites correctness fix).
  New `rewind` subcommand on `python -m kiln_scan`.
- Tests: `ingest/tests/test_download_s3.py` (new, 14 tests),
  `ingest/tests/test_cmr.py` (+3 tests for S3 link extraction/allowlist),
  `ingest/tests/test_cli_raster.py` (3 existing fire-mask tests updated for
  the `GranuleRef` mapping change), `scan/tests/test_backfill.py` (+3 tests
  for `global_fetch`/`s3_direct`), `scan/tests/test_full_rewind.py` (new, 12
  tests). Both suites fully green (ingest 490, scan all passing).

## Blocked on: AWS credentials

Checked before writing any of the launch commands below: no AWS credentials
exist anywhere in this setup. `~/.aws/` doesn't exist; the Doppler `kiln` and
`tortoise` projects hold no AWS keys (checked `dev`/`prd` configs directly).
Creating an AWS account or entering payment details is not something to do
unattended -- that needs you.

**What's needed to unblock, either one:**
1. An existing AWS account with an IAM user/access key (or `aws sso login`
   already set up), or
2. Create a fresh AWS account (needs a payment method) and generate an
   access key for an IAM user with EC2 launch permissions in `us-west-2`.

Once credentials exist (`aws configure` or env vars), everything below is
ready to run as-is.

## Launch checklist (once AWS credentials exist)

### 1. Benchmark first (~15 min, low cost)

Launch one small instance in `us-west-2`, confirm S3-direct actually works
end-to-end (not just the credential exchange, which was already verified),
and get a real per-granule timing number before sizing the real box.

```bash
aws ec2 run-instances \
  --region us-west-2 \
  --image-id <current Amazon Linux 2023 AMI for us-west-2> \
  --instance-type c7i.xlarge \
  --key-name <your key pair> \
  --count 1
# SSH in, install Python 3.12 + the ingest requirements, clone the repo,
# export EARTHDATA_TOKEN, then:
python -m kiln_ingest --date 2020-07-15 --archive --dry-run \
  --tiles-dir /tmp/bench --s3-direct --max-granules 20
# Compare granules/sec against the local baseline (~5.3s/granule, flat
# regardless of file size). Terminate the instance right after.
```

### 2. Size the real job from the benchmark's real number

Don't reuse the numbers below without the real benchmark -- they're upper
and lower bookends from what's confirmed today, not a sizing.

- **Known today (local, HTTPS, no S3):** ~20 min/day single-threaded ->
  ~134 days of single-threaded work for the full record.
- **If S3-direct cuts per-granule latency by 3-5x** (plausible for
  same-region S3 GetObject vs. a public HTTPS+CloudFront redirect chain,
  unconfirmed until the benchmark runs): ~4-27 min/day single-threaded.
- Pick worker count and instance size from there. `c7i.4xlarge` (16 vCPU,
  32 GB) priced around **$0.71/hr on-demand** in US regions (unconfirmed
  for `us-west-2` specifically at launch time -- recheck), meaningfully
  cheaper on Spot (typically 50-65% off for the C-family) and the whole
  pipeline is spot-safe: every job is idempotent and resumable through
  `BackfillLog`'s done-log, so an interruption just means that day retries
  on the next launch.

### 3. Launch the real rewind

```bash
python -m kiln_scan rewind \
  --work-dir /path/to/ssd-or-instance-storage/rewind-work \
  --tiles-dir /path/to/ssd-or-instance-storage/rewind-tiles \
  --workers <sized from the benchmark> \
  --s3-direct
```

Staged output lands as JSON per day under `--tiles-dir`
(`readings_<product>.json`, `anomalies_<product>.json` -- the exact
`build_reading_rows`/`build_anomaly_rows` shape a live Supabase upsert would
send). Resumable: re-running the same command skips every day already in the
done-log.

### 4. Batch import to Supabase (not yet built)

Once the new `kiln-archive` Supabase project exists (still blocked on the
platform outage as of this writing -- retry `troth` / the Management API
project-creation call before this step), a bulk importer needs to be written
that reads the staged JSON and writes it in batches. Not started this
session.

## Also still open from before this session

- Heat index / humidity overlay: research-only, no code (MERRA-2 reanalysis
  flagged as the most plausible source, needs its own pass).
- `manifest-alltime`'s "through" date shows last-merged date, not
  `max(reading_date)` -- low-priority cosmetic bug, troth `c4e532a9`.
