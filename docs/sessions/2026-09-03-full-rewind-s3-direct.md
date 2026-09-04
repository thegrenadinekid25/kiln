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

## AWS account: set up this session (2026-09-04, ~04:xx UTC)

Drove Chrome for navigation while the user handled every sensitive field
(email, password, payment, phone verification) themselves -- I never touched
account creation or entered credentials. Done:

- New AWS account created, signed into the console (account id
  `363476363325`, alias `thegrenadinekid25`).
- IAM user `kiln-rewind-cli` created (no console password, programmatic use
  only), with the `AmazonEC2FullAccess` managed policy attached.
- Cost safety net, set up **before** touching any compute:
  - AWS Budget `kiln-rewind-hard-cap`: $50.00/month, all AWS services in
    scope (not just EC2). Email alerts at 50% and 100% of budget to
    thegrenadinekid25@gmail.com.
  - Billing preferences: AWS Free Tier alerts and CloudWatch billing alerts
    both enabled and delivering to the root user email.
  - **Not done**: an automated hard-stop action (AWS Budgets can attach an
    action -- e.g. an IAM deny policy or an SSM automation to stop EC2 -- to
    a threshold). This needs a dedicated IAM execution role for AWS Budgets
    to assume, which didn't exist and isn't safe to hand-roll via console
    clicks at 2am. Better done via CLI once credentials exist: write the
    trust policy and the deny/stop policy precisely, show it before
    creating. Until then, the $50 cap is a real, working email-alert net,
    just not a fully automated cutoff.

**Credentials generated** (2026-09-04, later that morning): the user
generated the access key in the AWS console themselves and stored it in
Doppler (`kiln` project, `dev_personal` config, `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`) rather than `aws configure` -- consistent with this
portfolio's existing secrets practice. Verified working via
`doppler run --project kiln --config dev_personal -- aws sts get-caller-identity`.
All AWS CLI calls this session go through `doppler run ... --`.

## Benchmark: real, confirmed (2026-09-04)

Launched one `c7i-flex.large` (2 vCPU; new AWS accounts are Free-Tier-instance-type
-restricted -- see below) in `us-west-2`, cloned the repo, ran the real
ingest CLI with `--s3-direct` against 2020-07-15:

- **Both satellites, one full day, S3-direct: 2m53.8s wall clock**
  (~226 granules) -- confirmed via DEBUG log as genuine signed
  `HeadObject`/`GetObject` calls against
  `lp-prod-protected.s3.us-west-2.amazonaws.com`, no HTTPS fallback.
- That's **~174s/day on 2 vCPU**, vs. the local baseline's ~20 min/day --
  a real **~6.9x speedup**, exactly what direct same-region S3 access
  should buy over the public HTTPS+CloudFront path.
- Full record (2000-02-24..2026-09-04, 9,690 days) at that rate:
  **19.5 days single-threaded**, ~9.8 days at 2-way, ~2.4 days at 8-way,
  ~1.2 days at 16-way parallelism.
- **Cost is a non-issue regardless of parallelism**: total instance-hours
  is fixed (~468 hours of 2-vCPU-equivalent time); only wall-clock time
  changes with worker count. Verified real Spot price for `c7i-flex.large`
  in `us-west-2`: **$0.0268/hr** -> **~$12-15 total** for the entire 26-year
  rewind, however it's sliced. Well under the $50 budget cap.

## New-account instance-type restriction (real finding, not a quota problem)

Tried to launch anything bigger than a Free-Tier-eligible type
(`c7i-flex.large`, `t3.small`, `t3.micro`, `t4g.small/micro`,
`m7i-flex.large`) and every one failed:
`InvalidParameterCombination: The specified instance type is not eligible
for Free Tier.` This is **not** a Service Quotas limit -- the account's
"Running On-Demand Standard instances" quota was already 32 vCPUs (checked
directly), and a `--dry-run` launch of `c7i.4xlarge` even reported success.
The restriction only triggers on a **real** launch and isn't visible or
liftable via Service Quotas at all; it's a separate new-account abuse-
prevention gate. Requesting a quota increase (the `AWSServiceQuotasFullAccess`
policy was attached to `kiln-rewind-cli` for this) doesn't touch it -- ruled
out as the wrong lever after confirming with a real (not dry-run) launch
attempt.

## Fleet approach: chosen and running (2026-09-04, ~15:5x UTC)

User's call, given the quota path didn't apply: split the date range across
8 small Free-Tier-eligible instances rather than wait on AWS to lift the
restriction or open a support case.

**Infra built:**
- S3 staging bucket `kiln-rewind-staging-363476363325` (`us-west-2`, public
  access blocked).
- IAM role + instance profile `kiln-rewind-fleet-node`: trusts
  `ec2.amazonaws.com`, scoped to `s3:PutObject/GetObject/ListBucket` on only
  that bucket. Fleet nodes authenticate via instance-metadata credentials
  from this role -- **no AWS secret ever touches a fleet instance**, only the
  NASA Earthdata token (scp'd, same as the benchmark).
- A narrowly-scoped inline policy (`kiln-rewind-fleet-setup`) was added to
  `kiln-rewind-cli` to create the above -- resource-scoped to exactly this
  bucket and exactly this role/instance-profile name, not broad S3/IAM
  access.
- 8x `c7i-flex.large`, tag `Name=kiln-rewind-fleet`, `--instance-initiated
  -shutdown-behavior terminate`, user-data installs Python 3.12 + clones the
  repo + sets up `ingest/.venv`, plus a 4-day self-terminate safety net
  (`shutdown -h now` after 345600s) in case a node gets orphaned.
- Date range split into 8 contiguous ~1,211-day slices (script:
  `scratchpad/deploy_node.sh`, mapping in `scratchpad/fleet-slices.txt`).
  Each node runs `python -m kiln_scan rewind --start <s> --end <e>
  --workers 2 --s3-direct --ingest-dir ~/kiln/ingest --ingest-python
  ~/kiln/ingest/.venv/bin/python`, plus a background loop syncing
  `~/rewind-tiles` -> `s3://.../tiles/` and `~/rewind-work` (the per-node
  done-log) -> `s3://.../work/<hostname>/` every 120s.
- All 8 confirmed running with `pgrep`/log-tail spot checks; S3 sync
  confirmed landing real data.

**Expected completion**: ~1.2 days from launch (~2026-09-05 evening UTC),
16-way effective parallelism (8 nodes x 2 workers).

**Instance IDs, IPs, SSH key, security group, bucket name**: all recorded in
`scratchpad/fleet-*.txt` (session-local, not committed -- ephemeral infra
identifiers, not project state).

## Batch import to Supabase (not yet built)

Once the new `kiln-archive` Supabase project exists (still blocked on the
platform outage as of this writing -- retry `troth` / the Management API
project-creation call before this step), a bulk importer needs to be written
that reads the staged JSON out of `s3://kiln-rewind-staging-363476363325/tiles/`
and writes it into Supabase in batches. Not started this session.

## Fleet teardown (do this once the rewind finishes or is abandoned)

```bash
doppler run --project kiln --config dev_personal -- aws ec2 terminate-instances \
  --region us-west-2 --instance-ids $(cat scratchpad/fleet-instance-ids.txt)
doppler run --project kiln --config dev_personal -- aws ec2 delete-security-group \
  --region us-west-2 --group-id $(cat scratchpad/kiln-bench-sg-id.txt)
doppler run --project kiln --config dev_personal -- aws ec2 delete-key-pair \
  --region us-west-2 --key-name kiln-bench
# Keep the S3 bucket until the batch-import step reads it; then:
doppler run --project kiln --config dev_personal -- aws s3 rb \
  s3://kiln-rewind-staging-363476363325 --force
# The IAM role/instance-profile/policies are harmless to leave, but to remove:
doppler run --project kiln --config dev_personal -- aws iam remove-role-from-instance-profile \
  --instance-profile-name kiln-rewind-fleet-node --role-name kiln-rewind-fleet-node
doppler run --project kiln --config dev_personal -- aws iam delete-instance-profile \
  --instance-profile-name kiln-rewind-fleet-node
doppler run --project kiln --config dev_personal -- aws iam delete-role-policy \
  --role-name kiln-rewind-fleet-node --policy-name kiln-rewind-fleet-s3-access
doppler run --project kiln --config dev_personal -- aws iam delete-role \
  --role-name kiln-rewind-fleet-node
```

## Also still open from before this session

- Heat index / humidity overlay: research-only, no code (MERRA-2 reanalysis
  flagged as the most plausible source, needs its own pass).

## Picked up while blocked on AWS credentials

- Fixed troth `c4e532a9` (manifest-alltime's "through" date regressing on a
  historical backfill): added `alltime.alltime_through()`, mirroring the
  already-correct `alltime_since()` carry-forward pattern but taking the max
  instead of the min. Tested, committed (`5a09de4`), dismissed in Troth.
