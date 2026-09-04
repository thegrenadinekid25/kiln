"""Driving the 1 km ingest CLI over a worklist.

This is the only place the two tools touch, and they touch as processes, not as
imports: the backfill shells out to ``ingest/.venv/bin/python -m kiln_ingest``
with its own interpreter, its own dependencies and its own working directory.
Importing across the boundary would put the daily pipeline's failures inside a
batch job's process and make the batch job's dependency pins the daily
pipeline's problem.

The command builder is a pure function so the exact argv can be asserted in
tests without ever launching the real CLI.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .worklist import Bbox, Job

LOG = logging.getLogger("kiln_scan")

# The daily pipeline's checkout, its interpreter and its two products. Defaults
# rather than constants: a different checkout is a --ingest-dir away, and the
# tests never need the real ones to exist.
DEFAULT_INGEST_DIR = Path("/Volumes/tortoise/projects-local/kiln/ingest")
DEFAULT_INGEST_PYTHON = DEFAULT_INGEST_DIR / ".venv" / "bin" / "python"

INGEST_PRODUCTS = ("MOD11_L2", "MYD11_L2")

# First day each satellite produced its 1 km LST product, verified against CMR
# on 2026-08-31 by asking for the earliest granule of each. They match the CMG
# products' record starts, which they should: same satellites, same instruments.
#
# This is load-bearing, not decoration. Pass 1 covers 2000-02-24 onward, so a
# large share of the worklist predates Aqua. Asking the archive for MYD11_L2 on
# one of those dates returns nothing and the ingest CLI exits nonzero -- which
# is correct of it, and would otherwise mark every pre-2002 job as failed and
# trip the failure-rate guard across the whole early record.
INGEST_PRODUCT_STARTS: dict[str, date] = {
    "MOD11_L2": date(2000, 2, 24),
    "MYD11_L2": date(2002, 7, 4),
}


def products_for_day(
    day: date, products: Sequence[str] = INGEST_PRODUCTS
) -> list[str]:
    """The products whose satellite was actually flying on ``day``.

    A product with no start date on record is kept rather than dropped: an
    unknown product is the caller's business, and silently skipping it would
    turn a typo into a job that does nothing and reports success.
    """
    return [
        product
        for product in products
        if day >= INGEST_PRODUCT_STARTS.get(product, date.min)
    ]

DONE_LOG_NAME = "backfill_done.txt"

# A backfill of thousands of jobs will lose some days to transient archive
# failures, and stopping on the first one would waste the whole run. But a run
# where a fifth of the jobs failed is not a run with a few bad days in it; it is
# a broken configuration -- an expired token, a missing service key, a moved
# archive -- and it should exit nonzero so a caller notices.
FAILURE_RATE_LIMIT = 0.20


@dataclass(frozen=True)
class JobResult:
    """What one job's subprocess calls returned, per product."""

    day: date
    exit_codes: dict[str, int]

    @property
    def ok(self) -> bool:
        return bool(self.exit_codes) and all(
            code == 0 for code in self.exit_codes.values()
        )

    def log_line(self) -> str:
        """One done-log line: the date and every product's exit code."""
        codes = " ".join(
            f"{product}={self.exit_codes[product]}" for product in sorted(self.exit_codes)
        )
        return f"{self.day.isoformat()} {codes}"


# --- Command construction -----------------------------------------------------------


def ingest_command(
    python: Path,
    day: date,
    product: str,
    bboxes: Sequence[Bbox],
    dry_run: bool = False,
    max_granules: int | None = None,
    tiles_dir: Path | None = None,
    global_fetch: bool = False,
    s3_direct: bool = False,
) -> list[str]:
    """The exact argv for one ingest run.

    ``--bbox`` is emitted in the ``--bbox=W,S,E,N`` form, always, not as two
    argv entries. Any box in the western hemisphere starts with a minus sign,
    and argparse reads a separate argument beginning with ``-`` as a flag rather
    than a value; the ingest CLI's own help says so. Using the equals form
    unconditionally means a box's hemisphere cannot change whether the command
    parses.

    ``--archive`` is unconditional: every date a worklist can contain is
    historical, and LANCE only holds a few days, so the science-quality archive
    is the only place the granules exist.

    ``tiles_dir`` is worth setting on a dry run. The ingest CLI writes its
    raster pyramid to ``out-tiles/`` relative to its own working directory, so
    without it a backfill scribbles its scratch output into the daily
    pipeline's checkout instead of its own work directory.

    ``global_fetch`` is the full daily rewind's job shape: every day needs a
    whole-globe snapshot, not a refinement of specific hot cells, so it is the
    one caller allowed to omit every ``--bbox``. A refinement job with no boxes
    is still always a bug in whatever built the worklist -- that guard stays in
    place for ``global_fetch=False``, which is every other caller.
    """
    if not bboxes and not global_fetch:
        raise ValueError(f"job {day.isoformat()} has no bounding boxes to fetch")
    if bboxes and global_fetch:
        raise ValueError(
            f"job {day.isoformat()} is global_fetch but was also given bounding boxes"
        )
    if product is not None and product not in INGEST_PRODUCTS:
        raise ValueError(
            f"unknown ingest product {product!r}; expected one of {list(INGEST_PRODUCTS)}"
        )

    command = [
        str(python),
        "-m",
        "kiln_ingest",
        "--date",
        day.isoformat(),
    ]
    # No --product means the CLI runs BOTH satellites in one process, which is
    # what lets the cross-satellite corroboration screen actually compare them.
    # Two single-product invocations look equivalent but silently degrade every
    # record-tier reading to "single-satellite, uncorroborated" (the Sudan
    # 85.73 C ghost got back into the archive exactly this way).
    if product is not None:
        command.extend(["--product", product])
    command.append("--archive")
    command.extend(f"--bbox={box.as_arg()}" for box in bboxes)
    if max_granules is not None:
        command.extend(["--max-granules", str(max_granules)])
    if dry_run:
        command.append("--dry-run")
    if tiles_dir is not None:
        command.extend(["--tiles-dir", str(tiles_dir)])
    if s3_direct:
        command.append("--s3-direct")
    return command


# --- Done-log -----------------------------------------------------------------------


def done_log_path(work_dir: Path) -> Path:
    return Path(work_dir) / DONE_LOG_NAME


def parse_backfill_log(lines: Iterable[str]) -> set[date]:
    """Dates whose every product exited 0, from a done-log's lines.

    A line recording a failure stays in the log as a record of the attempt but
    does not mark the date done, so a rerun retries exactly the jobs that did
    not succeed. Unparseable lines are ignored: the log is bookkeeping, and a
    partial final line from a killed run must not make the whole file
    unreadable.
    """
    succeeded: set[date] = set()
    for line in lines:
        parts = line.strip().split()
        if not parts or parts[0].startswith("#"):
            continue
        try:
            day = date.fromisoformat(parts[0])
        except ValueError:
            continue
        codes = []
        for token in parts[1:]:
            _, _, value = token.partition("=")
            try:
                codes.append(int(value))
            except ValueError:
                codes = []
                break
        if codes and all(code == 0 for code in codes):
            succeeded.add(day)
    return succeeded


class BackfillLog:
    """Append-only record of attempted jobs, fsynced after each one."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._succeeded: set[date] = set()

    @classmethod
    def load(cls, path: Path) -> "BackfillLog":
        log = cls(path)
        if log.path.exists():
            log._succeeded = parse_backfill_log(
                log.path.read_text(encoding="utf-8").splitlines()
            )
        return log

    def __contains__(self, day: date) -> bool:
        return day in self._succeeded

    def __len__(self) -> int:
        return len(self._succeeded)

    @property
    def succeeded(self) -> set[date]:
        """Dates already completed with every product exiting 0."""
        return set(self._succeeded)

    def record(self, result: JobResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(result.log_line() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if result.ok:
            self._succeeded.add(result.day)


def pending_jobs(
    jobs: Sequence[Job], done: set[date], limit: int | None = None
) -> list[Job]:
    """Jobs still to run, in worklist order, capped at ``limit`` if given."""
    out: list[Job] = []
    for job in jobs:
        if job.day in done:
            continue
        out.append(job)
        if limit is not None and len(out) >= limit:
            break
    return out


# --- Running ------------------------------------------------------------------------


def run_job(
    job: Job,
    python: Path,
    ingest_dir: Path,
    dry_run: bool = False,
    max_granules: int | None = None,
    tiles_dir: Path | None = None,
    products: Sequence[str] = INGEST_PRODUCTS,
    runner: Callable[..., Any] = subprocess.run,
    global_fetch: bool = False,
    s3_direct: bool = False,
) -> JobResult:
    """Run the ingest CLI once per product for one job, sequentially.

    Sequential rather than parallel on purpose: both products download from the
    same LP DAAC host with the same token, and two concurrent granule streams
    per job would multiply the archive load without shortening the wall clock
    much, since the bottleneck is the network.

    Products whose satellite was not yet flying on the job's date are skipped
    rather than run and failed, so a job from 2001 is complete once Terra alone
    has been refined.

    Output is inherited rather than captured, so a long backfill shows the
    ingest CLI's own progress as it happens instead of going silent for hours.
    An exception from the subprocess layer is recorded as a nonzero code rather
    than raised: one unrunnable product must not end the backfill.
    """
    runnable = products_for_day(job.day, products)
    for skipped in [p for p in products if p not in runnable]:
        LOG.info(
            "%s: skipping %s, not launched until %s",
            job.day.isoformat(),
            skipped,
            INGEST_PRODUCT_STARTS[skipped].isoformat(),
        )

    exit_codes: dict[str, int] = {}
    # One invocation covering every flying satellite, not one per product:
    # the corroboration screen needs both accumulators in the same process.
    invocations = (
        [(None, tuple(runnable))]
        if len(runnable) > 1
        else [(runnable[0], (runnable[0],)) ] if runnable else []
    )
    for product, covered in invocations:
        command = ingest_command(
            python=python,
            day=job.day,
            product=product,
            bboxes=job.bboxes,
            dry_run=dry_run,
            max_granules=max_granules,
            tiles_dir=tiles_dir,
            global_fetch=global_fetch,
            s3_direct=s3_direct,
        )
        LOG.debug("running: %s", " ".join(command))
        try:
            completed = runner(command, cwd=str(ingest_dir), check=False)
            for name in covered:
                exit_codes[name] = int(completed.returncode)
        except Exception as exc:  # noqa: BLE001 - a failure to launch is a failed job
            LOG.warning(
                "%s %s: could not run the ingest CLI: %s: %s",
                job.day.isoformat(),
                "+".join(covered),
                type(exc).__name__,
                exc,
            )
            for name in covered:
                exit_codes[name] = -1
    return JobResult(day=job.day, exit_codes=exit_codes)
