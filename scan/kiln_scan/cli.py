"""Command line for the historical CMG scan.

Two subcommands:

* ``scan`` -- sweep a date range one day at a time, appending candidates and
  folding each day into the running all-time grid. This is the default, so the
  documented invocation works without naming it::

      python -m kiln_scan --product MOD11C1 --years 2000-2026 --work-dir ./work

* ``summarize`` -- read back what a scan produced: the top-N candidates across
  every year, and a small JSON describing the all-time grid.

* ``worklist`` -- turn the all-time grid into a list of (date, region) jobs for
  Pass 2 to refine at 1 km.

* ``backfill`` -- run the 1 km ingest CLI over that worklist.

``scan`` needs EARTHDATA_TOKEN in the environment. ``backfill`` needs whatever
the ingest CLI needs -- EARTHDATA_TOKEN, and SUPABASE_SERVICE_KEY unless
``--dry-run`` -- which it inherits rather than reads. ``summarize`` and
``worklist`` read only local files and need nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .backfill import (
    DEFAULT_INGEST_DIR,
    DEFAULT_INGEST_PYTHON,
    FAILURE_RATE_LIMIT,
    INGEST_PRODUCTS,
    BackfillLog,
    JobResult,
)
from .backfill import done_log_path as backfill_log_path
from .backfill import pending_jobs, run_job
from .cmr import PRODUCTS, find_daily_granule, product_info
from .download import DownloadError, download_granule
from .full_rewind import DEFAULT_START, full_range_jobs, run_full_rewind
from .grid import AlltimeGrid
from .science import Candidate, day_maximum, prepare_day, select_candidates
from .store import (
    CandidateRow,
    CandidateWriter,
    DoneLog,
    alltime_grid_path,
    candidate_files,
    done_log_path,
    download_dir,
    load_alltime_grid,
    pending_days,
    products_with_grids,
    read_candidate_files,
)
from .worklist import (
    DEFAULT_MERGE_DEGREES,
    DEFAULT_PAD_DEGREES,
    build_jobs,
    expected_granule_range,
    extract_hot_cells,
    jobs_from_payload,
    jobs_to_payload,
    merge_hot_cells,
)

LOG = logging.getLogger("kiln_scan")

DEFAULT_BAR_C = 55.0
DEFAULT_PROGRESS_EVERY = 30
DEFAULT_TOP_N = 20

# The bar for a Pass 2 candidate is deliberately higher than the bar for a Pass
# 1 candidate row. Pass 1 is cheap per cell and errs wide; Pass 2 downloads
# swath granules per date, so its list has to be the places that could actually
# hold a record.
DEFAULT_WORKLIST_BAR_C = 60.0

SUBCOMMANDS = ("scan", "summarize", "worklist", "backfill", "rewind")


# --- Argument types -----------------------------------------------------------------


def year_range(text: str) -> tuple[int, int]:
    """Parse ``2000-2026`` or a bare ``2019`` into an inclusive year pair."""
    parts = text.split("-")
    try:
        if len(parts) == 1:
            start = end = int(parts[0])
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
        else:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a year or a YYYY-YYYY range, got {text!r}"
        ) from None
    if end < start:
        raise argparse.ArgumentTypeError(f"year range {text!r} ends before it starts")
    return start, end


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")
    return value


def iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a date as YYYY-MM-DD, got {text!r}"
        ) from None


def resolve_scan_window(
    product: str,
    years: tuple[int, int] | None,
    today: date,
    start_override: date | None = None,
    end_override: date | None = None,
) -> tuple[date, date]:
    """The day range to sweep, clamped to what the product can actually have.

    ``--start``/``--end`` win over ``--years`` when given, which is how a single
    day or an arbitrary slice gets scanned; ``--years`` is the convenient form
    for whole years.

    Clamped at both ends on purpose. Asking for 2000 of MYD11C1 is not an error
    worth stopping for -- Aqua simply was not flying yet -- and asking for 2026
    in January would otherwise spend the rest of the year requesting days that
    do not exist.
    """
    info = product_info(product)
    if years is None:
        start_year, end_year = info.record_start.year, today.year
    else:
        start_year, end_year = years

    start = start_override if start_override is not None else date(start_year, 1, 1)
    end = end_override if end_override is not None else date(end_year, 12, 31)
    return max(start, info.record_start), min(end, today)


# --- Scan -------------------------------------------------------------------------


@dataclass
class ScanTotals:
    days_processed: int = 0
    days_missing: int = 0
    days_failed: int = 0
    candidates: int = 0


def _log_progress(
    product: str, day: date, totals: ScanTotals, grid: AlltimeGrid, remaining: int
) -> None:
    peak = grid.global_max()
    if peak is None:
        peak_text = "none yet"
    else:
        celsius, lat, lon, peak_day = peak
        peak_text = f"{celsius:.2f} C at {lat:.3f}, {lon:.3f} on {peak_day.isoformat()}"
    LOG.info(
        "progress %s: at %s, %d days done (%d missing, %d failed), "
        "%d candidates, %d days left, all-time max %s",
        product,
        day.isoformat(),
        totals.days_processed,
        totals.days_missing,
        totals.days_failed,
        totals.candidates,
        remaining,
        peak_text,
    )


def process_day(
    session: Any,
    product: str,
    day: date,
    token: str,
    work_dir: Path,
    bar_c: float,
    grid: AlltimeGrid,
    writer: CandidateWriter,
    max_error_class: int,
    keep_granules: bool,
) -> tuple[bool, int]:
    """Download, read, screen and fold one day. Returns ``(had_data, candidates)``.

    ``had_data`` is False when CMR lists no granule for the day, which is a
    normal state of this record rather than a failure. Exceptions propagate; the
    caller decides that one bad day does not end the sweep.
    """
    ref = find_daily_granule(session, product, day)
    if ref is None:
        return False, 0

    destination = download_dir(work_dir) / f"{ref.granule_id}.hdf"
    try:
        download_granule(session, ref.url, destination, token)

        from .hdf import read_day  # noqa: PLC0415 - pyhdf stays out of import time

        arrays = read_day(destination)
        field = prepare_day(
            arrays.raw_lst,
            arrays.lst_attrs,
            arrays.qc,
            max_error_class=max_error_class,
        )
        candidates: list[Candidate] = select_candidates(field, day, bar_c)

        if LOG.isEnabledFor(logging.DEBUG):
            peak = day_maximum(field)
            LOG.debug(
                "%s %s: %d cells kept (%d dropped by QC, %d implausible), "
                "%d at >= %.1f C, day max %s",
                product,
                day.isoformat(),
                field.kept,
                field.qc_dropped,
                field.implausible_dropped,
                len(candidates),
                bar_c,
                "none"
                if peak is None
                else f"{peak[0]:.2f} C at {peak[1]:.3f}, {peak[2]:.3f}",
            )

        # Order matters and is the resumability contract: candidates durable,
        # then the grid durable, then the day marked done by the caller.
        writer.write_day(day, candidates)
        grid.fold_day(field.celsius, field.keep, day)
        return True, len(candidates)
    finally:
        if not keep_granules:
            destination.unlink(missing_ok=True)
            destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)


def run_scan(args: argparse.Namespace, session: Any, token: str) -> int:
    product: str = args.product
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    start, end = resolve_scan_window(
        product,
        args.years,
        datetime.now(timezone.utc).date(),
        start_override=args.start,
        end_override=args.end,
    )
    if end < start:
        LOG.error(
            "%s has no days in the requested range: the record starts %s",
            product,
            product_info(product).record_start.isoformat(),
        )
        return 1

    done = DoneLog.load(done_log_path(work_dir, product))
    todo = pending_days(start, end, done.days, limit=args.days)
    grid_path = alltime_grid_path(work_dir, product)
    grid = AlltimeGrid.load(grid_path)

    LOG.info(
        "scanning %s (%s) %s..%s: %d days to do, %d already done, bar %.1f C",
        product,
        product_info(product).satellite,
        start.isoformat(),
        end.isoformat(),
        len(todo),
        len(done),
        args.bar,
    )
    if not todo:
        LOG.info("nothing to do")
        return 0

    totals = ScanTotals()
    interrupted = False
    last_day = todo[0]

    # Days folded into the grid in memory but not yet marked done on disk. They
    # are marked only after the grid holding them has been saved, so the
    # done-log can never claim a day the grid does not contain.
    unflushed: list[date] = []

    def flush() -> None:
        if not unflushed:
            return
        grid.save(grid_path)
        for finished in unflushed:
            done.mark(finished)
        unflushed.clear()

    with CandidateWriter(work_dir, product) as writer:
        for index, day in enumerate(todo):
            last_day = day
            try:
                had_data, found = process_day(
                    session=session,
                    product=product,
                    day=day,
                    token=token,
                    work_dir=work_dir,
                    bar_c=args.bar,
                    grid=grid,
                    writer=writer,
                    max_error_class=args.max_error_class,
                    keep_granules=args.keep_granules,
                )
            except KeyboardInterrupt:
                LOG.warning("interrupted during %s; that day will be redone", day)
                interrupted = True
                break
            except DownloadError as exc:
                LOG.warning("skipping %s: %s", day.isoformat(), exc)
                totals.days_failed += 1
                continue
            except Exception as exc:  # noqa: BLE001 - one bad day must not end the sweep
                LOG.warning(
                    "skipping %s: %s: %s", day.isoformat(), type(exc).__name__, exc
                )
                totals.days_failed += 1
                continue

            if not had_data:
                LOG.info("no granule published for %s %s", product, day.isoformat())
                totals.days_missing += 1
            totals.candidates += found
            totals.days_processed += 1
            unflushed.append(day)

            if len(unflushed) >= args.flush_every:
                flush()

            if totals.days_processed % args.progress_every == 0:
                _log_progress(product, day, totals, grid, len(todo) - index - 1)

        flush()

    _log_progress(product, last_day, totals, grid, 0)
    LOG.info(
        "scan %s: %d days done, %d with no granule, %d failed, %d candidates at >= %.1f C",
        "interrupted" if interrupted else "complete",
        totals.days_processed,
        totals.days_missing,
        totals.days_failed,
        totals.candidates,
        args.bar,
    )
    return 0


# --- Summarize ----------------------------------------------------------------------


def build_summary(
    product: str | None,
    grid: AlltimeGrid | None,
    rows: Sequence[CandidateRow],
    bar_c: float,
    top_n: int,
    days_done: int,
) -> dict[str, Any]:
    """The summary JSON: what the grid holds, plus the hottest candidates."""
    ranked = sorted(
        rows, key=lambda r: (-r.max_c, r.day, -r.cell_lat, r.cell_lon)
    )[:top_n]

    summary: dict[str, Any] = {
        "scanner_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "product": product,
        "bar_c": bar_c,
        "days_done": days_done,
        "candidate_rows": len(rows),
        "candidate_days": len({row.day for row in rows}),
        "top": [
            {
                "date": row.day.isoformat(),
                "cell_lat": row.cell_lat,
                "cell_lon": row.cell_lon,
                "max_c": row.max_c,
            }
            for row in ranked
        ],
    }

    if grid is None:
        summary["alltime_grid"] = None
        return summary

    peak = grid.global_max()
    summary["alltime_grid"] = {
        "cells_ever_observed": int(grid.observed().sum()),
        "cells_at_or_above_bar": grid.count_at_or_above(bar_c),
        "global_max": None
        if peak is None
        else {
            "celsius": round(peak[0], 2),
            "cell_lat": peak[1],
            "cell_lon": peak[2],
            "date": peak[3].isoformat(),
        },
    }
    return summary


def run_summarize(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    files = candidate_files(work_dir, args.product)
    rows = read_candidate_files(files)

    grid: AlltimeGrid | None = None
    days_done = 0
    if args.product:
        grid_path = alltime_grid_path(work_dir, args.product)
        grid = AlltimeGrid.load(grid_path) if grid_path.exists() else None
        days_done = len(DoneLog.load(done_log_path(work_dir, args.product)))

    summary = build_summary(
        product=args.product,
        grid=grid,
        rows=rows,
        bar_c=args.bar,
        top_n=args.top,
        days_done=days_done,
    )

    label = args.product or "all products"
    print(f"kiln historical scan summary: {label}")
    print(f"  candidate files: {len(files)}")
    print(f"  candidate rows:  {summary['candidate_rows']} across {summary['candidate_days']} days")
    if grid is not None:
        block = summary["alltime_grid"]
        print(f"  days scanned:    {days_done}")
        print(f"  cells observed:  {block['cells_ever_observed']}")
        print(f"  cells >= {args.bar:.1f} C: {block['cells_at_or_above_bar']}")
        peak = block["global_max"]
        if peak:
            print(
                f"  all-time max:    {peak['celsius']:.2f} C at "
                f"{peak['cell_lat']:.3f}, {peak['cell_lon']:.3f} on {peak['date']}"
            )
    print(f"  top {len(summary['top'])} candidates:")
    for entry in summary["top"]:
        print(
            f"    {entry['date']}  {entry['max_c']:6.2f} C  "
            f"{entry['cell_lat']:8.3f}, {entry['cell_lon']:9.3f}"
        )

    out_path = (
        Path(args.json_out)
        if args.json_out
        else work_dir / f"summary_{args.product or 'all'}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {out_path}")

    if args.export_npy:
        if grid is None:
            LOG.error("--export-npy needs --product and an existing all-time grid")
            return 1
        max_path = work_dir / f"alltime_cmg_{args.product}.npy"
        date_path = work_dir / f"alltime_dates_cmg_{args.product}.npy"
        grid.export_npy(max_path, date_path)
        print(f"  wrote {max_path} and {date_path}")

    return 0


# --- Worklist -----------------------------------------------------------------------


def run_worklist(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    products = [args.product] if args.product else products_with_grids(work_dir)
    if not products:
        LOG.error("no all-time grid found in %s; run a scan first", work_dir)
        return 1

    parts = []
    undatable = 0
    for product in products:
        grid = load_alltime_grid(work_dir, product)
        if grid is None:
            LOG.error("no all-time grid for %s in %s", product, work_dir)
            return 1
        cells, missing_dates = extract_hot_cells(grid, args.bar)
        undatable += missing_dates
        LOG.info(
            "%s: %d cells at >= %.1f C", product, len(cells), args.bar
        )
        parts.append(cells)

    cells = merge_hot_cells(parts)
    if undatable:
        # Should be zero: the scanner writes a maximum and its date together.
        # If it is not, saying so is better than inventing a date to refine on.
        LOG.warning(
            "%d cells reached the bar with no record date and were skipped", undatable
        )

    jobs = build_jobs(cells, args.merge_degrees, args.pad_degrees)
    payload = jobs_to_payload(
        jobs, args.bar, products, args.merge_degrees, args.pad_degrees
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    total_boxes = sum(len(job.bboxes) for job in jobs)
    low, high = expected_granule_range(jobs, products=len(INGEST_PRODUCTS))
    print(f"kiln refinement worklist from {', '.join(products)} at >= {args.bar:.1f} C")
    print(f"  cells over the bar: {len(cells)}")
    print(f"  jobs (unique dates): {len(jobs)}")
    print(f"  bounding boxes:     {total_boxes}")
    print(
        f"  expected downloads: {low}-{high} granules, roughly "
        f"({total_boxes} boxes x {len(INGEST_PRODUCTS)} products x ~1-4 each); "
        "a box several degrees across will exceed that on its own"
    )
    print(f"  top {min(args.top, len(jobs))} jobs:")
    for job in jobs[: args.top]:
        boxes = ", ".join(box.as_arg() for box in job.bboxes[:3])
        if len(job.bboxes) > 3:
            boxes += f", +{len(job.bboxes) - 3} more"
        print(
            f"    {job.day.isoformat()}  {job.cmg_max_c:6.2f} C  "
            f"{job.cells:>6} cells  {len(job.bboxes):>3} box(es)  [{boxes}]"
        )
    print(f"  wrote {out_path}")
    return 0


# --- Backfill -----------------------------------------------------------------------


def run_backfill(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    jobs_path = Path(args.jobs)
    try:
        payload = json.loads(jobs_path.read_text(encoding="utf-8"))
        jobs = jobs_from_payload(payload)
    except (OSError, ValueError) as exc:
        LOG.error("could not read jobs from %s: %s", jobs_path, exc)
        return 1

    ingest_dir = Path(args.ingest_dir)
    python = Path(args.ingest_python)
    if not python.exists():
        LOG.error(
            "no ingest interpreter at %s; point --ingest-python at the "
            "pipeline's venv",
            python,
        )
        return 1

    log = BackfillLog.load(backfill_log_path(work_dir))
    todo = pending_jobs(jobs, log.succeeded, limit=args.limit)

    LOG.info(
        "backfilling %d of %d jobs (%d already done)%s",
        len(todo),
        len(jobs),
        len(log),
        " as a dry run" if args.dry_run else "",
    )
    if not todo:
        LOG.info("nothing to do")
        return 0

    failed: list[JobResult] = []
    for index, job in enumerate(todo, start=1):
        LOG.info(
            "job %d/%d: %s, %.2f C, %d cells, %d box(es)",
            index,
            len(todo),
            job.day.isoformat(),
            job.cmg_max_c,
            job.cells,
            len(job.bboxes),
        )
        result = run_job(
            job,
            python=python,
            ingest_dir=ingest_dir,
            dry_run=args.dry_run,
            max_granules=args.max_granules,
            # Keep a dry run's raster output in this backfill's own work
            # directory rather than in the daily pipeline's checkout.
            tiles_dir=(work_dir / "dry-run-tiles") if args.dry_run else None,
        )
        log.record(result)
        if not result.ok:
            failed.append(result)
            LOG.warning("job %s failed: %s", job.day.isoformat(), result.log_line())

    succeeded = len(todo) - len(failed)
    LOG.info(
        "backfill finished: %d of %d jobs succeeded, %d failed",
        succeeded,
        len(todo),
        len(failed),
    )
    for result in failed:
        LOG.info("  failed: %s", result.log_line())

    failure_rate = len(failed) / len(todo)
    if failure_rate > FAILURE_RATE_LIMIT:
        LOG.error(
            "%.0f%% of jobs failed, over the %.0f%% limit; this looks like a "
            "configuration problem rather than a few bad days",
            failure_rate * 100,
            FAILURE_RATE_LIMIT * 100,
        )
        return 1
    return 0


# --- Full rewind --------------------------------------------------------------------


def run_rewind(args: argparse.Namespace) -> int:
    ingest_dir = Path(args.ingest_dir)
    python = Path(args.ingest_python)
    if not python.exists():
        LOG.error(
            "no ingest interpreter at %s; point --ingest-python at the "
            "pipeline's venv",
            python,
        )
        return 1

    start = args.start or DEFAULT_START
    end = args.end or datetime.now(timezone.utc).date()
    jobs = full_range_jobs(start, end)
    LOG.info("full rewind: %s..%s is %d day(s)", start.isoformat(), end.isoformat(), len(jobs))

    return run_full_rewind(
        jobs,
        python=python,
        ingest_dir=ingest_dir,
        work_dir=Path(args.work_dir),
        tiles_dir=Path(args.tiles_dir),
        workers=args.workers,
        s3_direct=args.s3_direct,
        max_granules=args.max_granules,
        limit=args.limit,
    )


# --- Parser -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m kiln_scan",
        description=(
            "Pass 1 of Kiln's all-time scan: sweep every day of the MODIS CMG "
            "record for cells that were ever extremely hot."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="sweep a date range (default)")
    scan.add_argument(
        "--product",
        required=True,
        choices=sorted(PRODUCTS),
        help="MOD11C1 (Terra, from 2000-02-24) or MYD11C1 (Aqua, from 2002-07-04)",
    )
    scan.add_argument(
        "--years",
        type=year_range,
        default=None,
        help="inclusive year range, e.g. 2000-2026 (default: the product's whole record)",
    )
    scan.add_argument(
        "--start",
        type=iso_date,
        default=None,
        help="first day to scan as YYYY-MM-DD; overrides the start of --years",
    )
    scan.add_argument(
        "--end",
        type=iso_date,
        default=None,
        help="last day to scan as YYYY-MM-DD; overrides the end of --years",
    )
    scan.add_argument(
        "--days",
        type=positive_int,
        default=None,
        help="stop after this many days; for testing a slice of the record",
    )
    scan.add_argument(
        "--bar",
        type=float,
        default=DEFAULT_BAR_C,
        help=f"record a cell as a candidate at or above this Celsius (default {DEFAULT_BAR_C})",
    )
    scan.add_argument(
        "--work-dir",
        required=True,
        help="directory for the done-log, candidate CSVs, all-time grid and downloads",
    )
    scan.add_argument(
        "--max-error-class",
        type=int,
        choices=(0, 1, 2, 3),
        default=1,
        help="highest QC LST error class to keep (default 1, average error <= 2K)",
    )
    scan.add_argument(
        "--progress-every",
        type=positive_int,
        default=DEFAULT_PROGRESS_EVERY,
        help=f"log a progress line every N days (default {DEFAULT_PROGRESS_EVERY})",
    )
    scan.add_argument(
        "--flush-every",
        type=positive_int,
        default=1,
        help=(
            "save the all-time grid every N days (default 1). The grid is 155 MB, "
            "so a long unattended run can raise this to write less; the cost is "
            "that a kill then redoes up to N days rather than one. A day is never "
            "marked done before the grid holding it is on disk, at any setting."
        ),
    )
    scan.add_argument(
        "--keep-granules",
        action="store_true",
        help="do not delete each downloaded granule; for debugging one day only",
    )

    summarize = subparsers.add_parser(
        "summarize", help="report the top candidates and the all-time grid"
    )
    summarize.add_argument(
        "--product",
        choices=sorted(PRODUCTS),
        default=None,
        help="narrow to one product (required to read an all-time grid)",
    )
    summarize.add_argument("--work-dir", required=True, help="the scan's work directory")
    summarize.add_argument(
        "--top",
        type=positive_int,
        default=DEFAULT_TOP_N,
        help=f"how many candidates to report (default {DEFAULT_TOP_N})",
    )
    summarize.add_argument(
        "--bar",
        type=float,
        default=DEFAULT_BAR_C,
        help="Celsius bar for the grid's cell count (default %(default)s)",
    )
    summarize.add_argument("--json-out", default=None, help="where to write the summary JSON")
    summarize.add_argument(
        "--export-npy",
        action="store_true",
        help="also write the grid as two plain .npy files for downstream tools",
    )

    worklist = subparsers.add_parser(
        "worklist", help="turn the all-time grid into 1 km refinement jobs"
    )
    worklist.add_argument("--work-dir", required=True, help="the scan's work directory")
    worklist.add_argument(
        "--product",
        choices=sorted(PRODUCTS),
        default=None,
        help="use one product's grid (default: every product the work dir has)",
    )
    worklist.add_argument(
        "--bar",
        type=float,
        default=DEFAULT_WORKLIST_BAR_C,
        help=(
            f"refine cells whose all-time max reaches this Celsius "
            f"(default {DEFAULT_WORKLIST_BAR_C})"
        ),
    )
    worklist.add_argument(
        "--merge-degrees",
        type=float,
        default=DEFAULT_MERGE_DEGREES,
        help=(
            "cells this far apart or closer share a bounding box "
            "(default %(default)s)"
        ),
    )
    worklist.add_argument(
        "--pad-degrees",
        type=float,
        default=DEFAULT_PAD_DEGREES,
        help="grow every box by this much on each side (default %(default)s)",
    )
    worklist.add_argument(
        "--top",
        type=positive_int,
        default=10,
        help="how many jobs to print (default %(default)s)",
    )
    worklist.add_argument("--out", required=True, help="where to write the jobs JSON")

    backfill = subparsers.add_parser(
        "backfill", help="run the 1 km ingest CLI over a worklist"
    )
    backfill.add_argument("--jobs", required=True, help="a jobs JSON from `worklist`")
    backfill.add_argument(
        "--work-dir", required=True, help="where the backfill done-log lives"
    )
    backfill.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="stop after this many jobs; the worklist is hottest-first",
    )
    backfill.add_argument(
        "--dry-run",
        action="store_true",
        help="pass --dry-run to the ingest CLI: no Supabase writes",
    )
    backfill.add_argument(
        "--max-granules",
        type=positive_int,
        default=None,
        help="cap granules per product per job; passed straight to the ingest CLI",
    )
    backfill.add_argument(
        "--ingest-dir",
        default=str(DEFAULT_INGEST_DIR),
        help="the ingest checkout to run in (default %(default)s)",
    )
    backfill.add_argument(
        "--ingest-python",
        default=str(DEFAULT_INGEST_PYTHON),
        help="the ingest venv interpreter (default %(default)s)",
    )

    rewind = subparsers.add_parser(
        "rewind",
        help="full daily rewind: one whole-globe snapshot per day of the record",
    )
    rewind.add_argument(
        "--start",
        type=iso_date,
        default=None,
        help=f"first day, YYYY-MM-DD (default {DEFAULT_START.isoformat()}, the record start)",
    )
    rewind.add_argument(
        "--end",
        type=iso_date,
        default=None,
        help="last day, YYYY-MM-DD (default: today UTC)",
    )
    rewind.add_argument(
        "--work-dir", required=True, help="where the rewind's done-log lives"
    )
    rewind.add_argument(
        "--tiles-dir", required=True, help="where each day's staged readings/anomalies land"
    )
    rewind.add_argument(
        "--workers",
        type=positive_int,
        default=8,
        help="concurrent days in flight (default %(default)s)",
    )
    rewind.add_argument(
        "--s3-direct",
        action="store_true",
        help="pass --s3-direct to the ingest CLI (only helps from AWS us-west-2; "
        "safe elsewhere, see the ingest CLI's own --help)",
    )
    rewind.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help="stop after this many pending days",
    )
    rewind.add_argument(
        "--max-granules",
        type=positive_int,
        default=None,
        help="cap granules per product per day; passed straight to the ingest CLI",
    )
    rewind.add_argument(
        "--ingest-dir",
        default=str(DEFAULT_INGEST_DIR),
        help="the ingest checkout to run in (default %(default)s)",
    )
    rewind.add_argument(
        "--ingest-python",
        default=str(DEFAULT_INGEST_PYTHON),
        help="the ingest venv interpreter (default %(default)s)",
    )

    for sub in (scan, summarize, worklist, backfill, rewind):
        sub.add_argument(
            "--verbose", action="store_true", help="log at DEBUG instead of INFO"
        )

    return parser


def normalise_argv(argv: Sequence[str]) -> list[str]:
    """Insert the default ``scan`` subcommand when none was named.

    So that the documented invocation -- ``--product X --years Y --work-dir Z``
    -- works as written, while ``summarize`` stays a real subcommand with its
    own options.
    """
    argv = list(argv)
    if not argv:
        # Let argparse report the missing subcommand and print the usage line.
        return argv
    if argv[0] in SUBCOMMANDS or argv[0] in ("-h", "--help"):
        return argv
    if argv[0].startswith("-"):
        return ["scan", *argv]
    # An unrecognised word: leave it for argparse, whose error names the valid
    # subcommands, rather than burying it inside a "scan" it does not belong to.
    return argv


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(normalise_argv(raw))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "summarize":
        return run_summarize(args)
    if args.command == "worklist":
        return run_worklist(args)
    if args.command == "backfill":
        # The ingest CLI reads its own credentials out of the environment it
        # inherits. Checking for them here would duplicate its rules and go
        # stale; letting it refuse in its own words is more honest.
        return run_backfill(args)
    if args.command == "rewind":
        return run_rewind(args)

    token = os.environ.get("EARTHDATA_TOKEN", "")
    if not token:
        LOG.error(
            "EARTHDATA_TOKEN is not set. Generate one at "
            "https://urs.earthdata.nasa.gov/profile and export it."
        )
        return 1

    import requests  # noqa: PLC0415 - keeps --help usable without the dependency

    with requests.Session() as session:
        return run_scan(args, session, token)
