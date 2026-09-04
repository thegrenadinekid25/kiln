"""The full daily rewind: one whole-globe snapshot per day of the record.

This is a different job shape from :mod:`backfill`, not a variation of it.
Pass 2's ordinary backfill refines the handful of days and regions Pass 1
flagged as candidates -- a job is a hot region on a hot day. The rewind's job
is "every day needs a browsable snapshot, whether or not anything record-tier
happened," so a job here is just a date; it carries no bounding boxes at all,
which is why :func:`ingest_command` needed an explicit ``global_fetch`` escape
hatch rather than silently reinterpreting an empty box list.

Reused rather than reimplemented from :mod:`backfill`: :class:`Job` (with
``bboxes=()``, ``cells``/``cmg_max_c`` unused placeholders -- this job type has
no such numbers), :class:`BackfillLog` for the resumable done-log, and
:func:`run_job` itself, which already does the one-invocation-covers-both-
satellites thing the corroboration screen depends on.

Parallelism is threads, not processes: each job's real work happens inside a
subprocess (the ingest CLI), so the GIL sits idle for the whole job and a
thread pool gets genuine concurrency without the complications of sharing a
``BackfillLog`` across process boundaries. The log's own append is wrapped in
a lock -- see :class:`BackfillLog`'s docstring for why a single line append is
otherwise safe, but a lock costs nothing here and removes any doubt.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from .backfill import (
    DEFAULT_INGEST_DIR,
    DEFAULT_INGEST_PYTHON,
    FAILURE_RATE_LIMIT,
    INGEST_PRODUCTS,
    BackfillLog,
    JobResult,
)
from .backfill import done_log_path as backfill_log_path
from .backfill import run_job
from .cmr import product_info
from .worklist import Job

LOG = logging.getLogger("kiln_scan")

# MOD11_L2's own record start (Terra); MYD11_L2 (Aqua) starts later and
# ``run_job`` already skips it on days before its own launch via
# ``products_for_day``. Starting the rewind here rather than at each
# product's own start means Terra-only days near the front of the record
# still get a job instead of being silently excluded from the date range.
DEFAULT_START = product_info("MOD11C1").record_start


def full_range_days(start: date, end: date) -> list[date]:
    """Every calendar day in ``[start, end]``, inclusive."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def full_range_jobs(start: date, end: date) -> list[Job]:
    """One placeholder :class:`Job` per day, with no bounding boxes.

    ``cells`` and ``cmg_max_c`` are 0 / 0.0: this job type was never scored
    against a candidate bar, and nothing downstream reads those fields for a
    ``global_fetch`` job -- they exist only because ``Job`` is shared with the
    refinement backfill.
    """
    return [Job(day=day, bboxes=(), cells=0, cmg_max_c=0.0) for day in full_range_days(start, end)]


def run_full_rewind(
    jobs: Sequence[Job],
    python: Path,
    ingest_dir: Path,
    work_dir: Path,
    tiles_dir: Path,
    workers: int,
    s3_direct: bool = False,
    max_granules: int | None = None,
    products: Sequence[str] = INGEST_PRODUCTS,
    limit: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> int:
    """Run the pending jobs across a thread pool, resumable via the done-log.

    Always a dry run: the rewind's destination is local disk
    (``write_readings_locally`` in the ingest CLI), never Supabase directly --
    a separate, later step imports the staged output in bulk. There is no
    ``dry_run`` parameter because there is no other mode.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    log = BackfillLog.load(backfill_log_path(work_dir))
    todo = [job for job in jobs if job.day not in log]
    if limit is not None:
        todo = todo[:limit]

    LOG.info(
        "full rewind: %d of %d days pending (%d already done), %d worker(s)%s",
        len(todo),
        len(jobs),
        len(log),
        workers,
        ", S3-direct" if s3_direct else "",
    )
    if not todo:
        LOG.info("nothing to do")
        return 0

    lock = threading.Lock()
    failed: list[JobResult] = []
    completed = 0

    def run_one(job: Job) -> JobResult:
        return run_job(
            job,
            python=python,
            ingest_dir=ingest_dir,
            dry_run=True,
            max_granules=max_granules,
            tiles_dir=tiles_dir,
            products=products,
            global_fetch=True,
            s3_direct=s3_direct,
            runner=runner,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, job): job for job in todo}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one job's crash must not end the rewind
                LOG.warning("%s: run_job raised %s: %s", job.day.isoformat(), type(exc).__name__, exc)
                result = JobResult(day=job.day, exit_codes={"__raised__": -1})

            with lock:
                log.record(result)
                completed += 1
                if not result.ok:
                    failed.append(result)
                    LOG.warning("job %s failed: %s", job.day.isoformat(), result.log_line())
                if completed % 25 == 0 or completed == len(todo):
                    LOG.info(
                        "progress: %d/%d days attempted, %d failed",
                        completed,
                        len(todo),
                        len(failed),
                    )

    succeeded = len(todo) - len(failed)
    LOG.info(
        "full rewind finished: %d of %d days succeeded, %d failed",
        succeeded,
        len(todo),
        len(failed),
    )

    failure_rate = len(failed) / len(todo)
    if failure_rate > FAILURE_RATE_LIMIT:
        LOG.error(
            "%.0f%% of days failed, over the %.0f%% limit; this looks like a "
            "configuration problem rather than a few bad days",
            failure_rate * 100,
            FAILURE_RATE_LIMIT * 100,
        )
        return 1
    return 0
