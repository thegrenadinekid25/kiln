# Full daily rewind: S3-direct download + launch checklist

Troth request: `cf024cf9`. Picks up from the git-history-rewrite /
public-launch session (2026-08-31 doc).

---

# ⛔ RESUME HERE (paused 2026-09-08, blocked on AWS account)

**One-line state**: the 26-year rewind ran to ~95%+ (likely 100%) on an
8-node AWS fleet, all output is in an S3 bucket, and then the AWS account
ran out of free credits — API access was cut off and the fleet was stopped
mid-flight. Nothing is lost; it is locked behind the account. **Get the
account reinstated, then follow "First moves when access returns" below.**

## What is where

| Thing | Value |
|---|---|
| AWS account | `363476363325` (alias `thegrenadinekid25`), region `us-west-2` |
| Staged output (THE ASSET) | `s3://kiln-rewind-staging-363476363325/tiles/` |
| Per-node done-logs | `s3://kiln-rewind-staging-363476363325/work/<hostname>/backfill_done.txt` |
| AWS creds | Doppler `kiln` / `dev_personal` -> `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| Earthdata token | Doppler `kiln` / `prd` -> `EARTHDATA_TOKEN` (valid to 2026-10-30) |
| SSH key + fleet scripts | `/Volumes/tortoise/projects-local/.kiln-infra/` (outside git, key is `chmod 600`) |
| IAM user | `kiln-rewind-cli` (EC2FullAccess, ServiceQuotasFullAccess, inline `kiln-rewind-fleet-setup`) |
| Fleet role/profile | `kiln-rewind-fleet-node` (trusts ec2, inline `kiln-rewind-fleet-s3-access` scoped to the bucket) |
| Security group | `sg-05aa925a3548cf631` (SSH 22; several home IPs allowlisted) |
| Key pair | `kiln-bench` |
| Budget guard | `kiln-rewind-hard-cap`, $50/mo, email alerts at 50% / 100% |
| Instance IDs | `i-01a70c14cdfd8505e i-02eb5308b9ee9e77d i-06f365e70d28f4455 i-0b3939040bb34a38b i-04eed948b5b065c1e i-037218dcd6931d7f1 i-0e5329e1f385a81e1 i-09fa13adb4d2dac69` |

Every AWS call this session was made as
`doppler run --project kiln --config dev_personal -- aws ...`.

## How far it actually got

Last verified reading, **2026-09-08 16:39 UTC**: **9,197 / 9,690 days
succeeded (94.9%)**, counted properly (see the `wc -l` gotcha below). Two of
eight nodes had already finished their slices and exited cleanly. The other
six were running at ~230-290 days/hour with ~493 days left between them,
which extrapolates to **completion around 18:30 UTC** — and the credentials
were dead by 18:38 UTC. So the job plausibly finished, but **that is an
extrapolation, not a verified fact.** Verify against the done-logs in S3.

| Slice | Node IP | Slice days | Succeeded @16:39 | Est. remaining |
|---|---|---|---|---|
| 2000-02-24..2003-06-19 | 54.212.149.150 | 1212 | 1172 | 0 — **finished**, 40 permanent failures |
| 2003-06-20..2006-10-13 | 52.88.193.98 | 1212 | 1147 | ~65 |
| 2006-10-14..2010-02-05 | 54.202.227.185 | 1211 | 1054 | ~157 |
| 2010-02-06..2013-05-31 | 50.112.162.184 | 1211 | 1098 | ~113 |
| 2013-06-01..2016-09-23 | 35.93.3.247 | 1211 | 1210 | 0 — **finished** |
| 2016-09-24..2020-01-17 | 34.211.131.93 | 1211 | 1138 | ~73 |
| 2020-01-18..2023-05-12 | 18.246.70.1 | 1211 | 1196 | ~15 |
| 2023-05-13..2026-09-04 | 44.248.129.33 | 1211 | 1182 | ~29 |

The 40 permanent failures on the first slice are **real NASA archive gaps**
in Terra's first year (`CMR returned no daytime MOD11_L2 granules for
<date>`), not bugs. Expect a scattering of these across the record; they can
never succeed no matter how many times they are retried.

## First moves when access returns

**1. Confirm credentials, then immediately de-risk the asset.** The single
most important action is getting the S3 output onto the SSD so the work no
longer depends on the AWS account at all:

```bash
doppler run --project kiln --config dev_personal -- aws sts get-caller-identity
mkdir -p /Volumes/tortoise/kiln-rewind-staging
doppler run --project kiln --config dev_personal -- aws s3 sync \
  s3://kiln-rewind-staging-363476363325/ /Volumes/tortoise/kiln-rewind-staging/ \
  --region us-west-2
du -sh /Volumes/tortoise/kiln-rewind-staging
```

**2. Compute what is actually still pending** from the synced done-logs —
do not trust the estimate table above, it is only a fallback:

```bash
cd /Volumes/tortoise/projects-local/kiln/scan
.venv/bin/python - <<'PY'
from datetime import date, timedelta
from pathlib import Path
from kiln_scan.backfill import parse_backfill_log

root = Path('/Volumes/tortoise/kiln-rewind-staging/work')
done = set()
for log in root.glob('*/backfill_done.txt'):
    done |= parse_backfill_log(log.read_text().splitlines())

start, end = date(2000, 2, 24), date(2026, 9, 4)
alld = {start + timedelta(days=i) for i in range((end - start).days + 1)}
missing = sorted(alld - done)
print(f'done={len(done)} missing={len(missing)}')
for d in missing[:40]:
    print(' ', d.isoformat())
PY
```

**3. Finish whatever is left.** If it is small (tens to a few hundred days),
either is fine:

- *Locally, no AWS needed* — `--s3-direct` self-disables outside `us-west-2`
  and falls back to HTTPS. Budget ~6-7 min/day with 3 workers on this
  machine (8 GB RAM is the limit on concurrency; do not exceed ~3).
- *One `us-west-2` instance* — ~2 min/day, but re-provisioning is only worth
  it for a few hundred days or more. Reuse `.kiln-infra/deploy_node.sh`.

**4. Then tear the fleet down** (commands in the teardown section below) and
**5.** build the Supabase batch importer, which was never started.

## Gotchas that cost real time this session — read before touching the fleet

- **`ssh -n` silently breaks heredoc piping.** `-n` redirects the remote
  stdin from `/dev/null`, so `ssh -n host bash -s <<'EOF'` sends the script
  nowhere and you get zero output with exit code 0. Use `< /dev/null` on the
  *outer* command instead when looping.
- **`pgrep -f <pattern>` self-matches.** Run over SSH, your own command line
  contains the pattern, so it always reports "found". Use the bracket trick:
  `ps aux | grep '[k]iln_scan rewind'`.
- **A home IP is not stable.** The SG is scoped to specific IPs; when the IP
  changes, SSH fails as a *connection timeout*, which looks exactly like a
  dead instance. Check `curl -s https://checkip.amazonaws.com` first, always.
- **`wc -l` on a done-log overcounts.** Failed attempts and their later
  retries are separate lines. For true counts use
  `kiln_scan.backfill.parse_backfill_log`, which dedupes to succeeded dates.
- **`doppler run -- aws ... $VAR` mangles unquoted word-splitting.** Pass
  multi-value args (like `--instance-ids`) explicitly, not via a variable.
- **New AWS accounts are restricted to Free-Tier instance types**, and
  `run-instances --dry-run` does *not* catch it — dry-run reports success and
  the real launch then fails `InvalidParameterCombination`. This is not a
  Service Quotas limit (that quota was already 32 vCPUs); a quota increase
  request is the wrong lever entirely.
- **Credit exhaustion looks like a security incident.** Every API call fails
  `InvalidClientTokenId` / `AuthFailure`, and instances become unreachable —
  indistinguishable from a suspension until you check billing.

---

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

## Incident: all 8 fleet nodes died from disk-full (2026-09-05, caught 2026-09-07)

**Root cause**: the sync loop on every node (`aws s3 sync ~/rewind-tiles s3://.../tiles/`
every 120s) uploaded but never deleted the local copies. `c7i-flex.large`'s
default root volume is 8 GB. Local tile output accumulated monotonically
until the disk hit 100% (confirmed: `df -h /` showed `8.0G 8.0G 20K 100%` on
every node), at which point the `kiln_scan rewind` process died on an
unhandled write failure -- `rewind.log`'s last line was cut off mid-word, a
clean signature of an abrupt kill. `journalctl` on one node showed the smoking
gun directly: `seelog internal error: write
/var/log/amazon/ssm/amazon-ssm-agent.log: no space left on device`. All 8
nodes hit this within a few hours of each other (~7-10h after launch, all
around 363-365 days processed, node 1 further at 558 since its 2000-2001
slice predates Aqua and has lighter per-day output) -- a systemic design bug
in the sync loop, not a fluke on one box.

**Caught late**: the user asked for a status check on 2026-09-07, ~2.5 days
after the crash. Nothing was proactively monitored in between -- worth
naming plainly: this should have had a scheduled check-in rather than
waiting to be asked, especially for an unattended multi-day job.

**Data loss**: none. Sync uploads don't need local disk headroom, so
everything synced before the crash (3,107 of 9,690 days, spot-checked
against the S3 bucket) is durable. Only wall-clock time was lost -- roughly
2.5 days of the fleet sitting idle, dead, before anyone noticed.

**Fix, deployed to all 8 nodes**: killed the old sync loop, freed each
node's disk (`rm -rf ~/rewind-tiles/*` -- safe, already durable in S3),
replaced the sync loop with one that deletes local day-directories after a
clean sync, gated on `mtime +5min` so a day still being actively written is
never wiped mid-write:

```bash
while true; do
  aws s3 sync ~/rewind-tiles s3://kiln-rewind-staging-363476363325/tiles/ --quiet \
    && find ~/rewind-tiles -mindepth 1 -maxdepth 1 -type d -mmin +5 -exec rm -rf {} +
  aws s3 sync ~/rewind-work s3://kiln-rewind-staging-363476363325/work/$(hostname)/ --quiet
  sleep 120
done
```

Then resumed `kiln_scan rewind` on the same `--work-dir` on every node --
the existing done-log means this is a clean resume from exactly where each
node died, not a restart. All 8 confirmed running again post-fix
(2026-09-07, ~16:38 UTC), disks down to ~30% used.

**Also hit and fixed en route**: the SSH security group was scoped to my
laptop's public IP at setup time; that IP changed between sessions and
silently blocked every SSH connection (no error, just hangs) until I noticed
and added a rule for the new IP. Worth remembering for next time this
pattern is used: a home IP is not stable across days.

**Revised progress** (2026-09-07 ~16:38 UTC): 3,107/9,690 days done (32%),
6,583 remaining. At the same ~337 days/hour fleet rate: **~19.5 hours
remaining**, landing around 2026-09-08 ~12:00 UTC.

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
