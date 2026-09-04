"""On-disk state: the done-log that makes the scan resumable, and the candidate CSVs.

A 24-year sweep is going to be interrupted -- by a lost network, a full disk, a
laptop lid. The contract here is that an interrupted run loses at most the day
it was working on, and that a rerun over the same range is safe: nothing is
double-counted in the all-time grid (the fold is a maximum, so it is
idempotent), and the summary reader deduplicates candidate rows, so a day whose
CSV was written just before the process died cannot inflate a count on rerun.

The ordering that guarantees it, per day: write the candidate rows and fsync,
save the all-time grid atomically, and only then append the date to the
done-log. Every artifact a day produces is durable before the day is called
done, so the worst a crash can do is repeat a day.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator, Sequence

import numpy as np

from .science import Candidate

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    from .grid import AlltimeGrid

CANDIDATE_HEADER = ("date", "cell_lat", "cell_lon", "max_c")


# --- Layout -------------------------------------------------------------------------


def done_log_path(work_dir: Path, product: str) -> Path:
    return Path(work_dir) / f"done_{product}.txt"


def alltime_grid_path(work_dir: Path, product: str) -> Path:
    return Path(work_dir) / f"alltime_cmg_{product}.npz"


def alltime_npy_paths(work_dir: Path, product: str) -> tuple[Path, Path]:
    """The plain-``.npy`` pair ``summarize --export-npy`` writes."""
    work_dir = Path(work_dir)
    return (
        work_dir / f"alltime_cmg_{product}.npy",
        work_dir / f"alltime_dates_cmg_{product}.npy",
    )


def load_alltime_grid(work_dir: Path, product: str) -> "AlltimeGrid | None":
    """Read a product's all-time grid, or None if the work dir has none.

    Prefers the ``.npz`` the scanner writes, since that is the authoritative
    artifact and its two arrays are guaranteed to have been saved together.
    Falls back to an exported ``.npy`` pair so a work directory populated only
    by ``summarize --export-npy`` still works, but only when both halves are
    present -- a maximum without its dates cannot be turned into jobs.
    """
    from .grid import AlltimeGrid  # noqa: PLC0415 - avoids a module-level cycle

    archive = alltime_grid_path(work_dir, product)
    if archive.exists():
        return AlltimeGrid.load(archive)

    max_path, date_path = alltime_npy_paths(work_dir, product)
    if max_path.exists() and date_path.exists():
        return AlltimeGrid(max_centi=np.load(max_path), date_int=np.load(date_path))
    return None


def products_with_grids(work_dir: Path) -> list[str]:
    """Which products this work directory holds an all-time grid for."""
    from .cmr import PRODUCTS  # noqa: PLC0415 - avoids a module-level cycle

    found = []
    for product in sorted(PRODUCTS):
        archive = alltime_grid_path(work_dir, product)
        max_path, date_path = alltime_npy_paths(work_dir, product)
        if archive.exists() or (max_path.exists() and date_path.exists()):
            found.append(product)
    return found


def candidates_dir(work_dir: Path) -> Path:
    return Path(work_dir) / "candidates"


def candidates_path(work_dir: Path, product: str, year: int) -> Path:
    return candidates_dir(work_dir) / f"candidates_{product}_{year}.csv"


def download_dir(work_dir: Path) -> Path:
    return Path(work_dir) / "granules"


# --- Done-log -----------------------------------------------------------------------


class DoneLog:
    """Append-only record of days already folded in, one ISO date per line.

    Held in memory as a set for the duration of a run and appended to disk after
    each day, flushed and fsynced. The file is the authority; the set is a cache
    of it.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._done: set[date] = set()

    @classmethod
    def load(cls, path: Path) -> "DoneLog":
        log = cls(path)
        log._done = parse_done_log(read_done_lines(path))
        return log

    def __contains__(self, day: date) -> bool:
        return day in self._done

    def __len__(self) -> int:
        return len(self._done)

    @property
    def days(self) -> frozenset[date]:
        return frozenset(self._done)

    def mark(self, day: date) -> None:
        """Record a day as complete, durably, before the caller moves on."""
        if day in self._done:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(f"{day.isoformat()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._done.add(day)


def read_done_lines(path: Path) -> list[str]:
    path = Path(path)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def parse_done_log(lines: Iterable[str]) -> set[date]:
    """Parse a done-log's lines into dates, ignoring blanks and comments.

    A line that is not a date is skipped rather than fatal. The log is an
    optimisation -- redoing a day is merely slow -- so a corrupted tail must not
    stop the scan from running at all.
    """
    days: set[date] = set()
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            days.add(date.fromisoformat(text))
        except ValueError:
            continue
    return days


# --- Day ranges ---------------------------------------------------------------------


def days_in_range(start: date, end: date) -> Iterator[date]:
    """Every calendar day from ``start`` to ``end`` inclusive."""
    if end < start:
        return
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def pending_days(
    start: date,
    end: date,
    done: Iterable[date],
    limit: int | None = None,
) -> list[date]:
    """Days in the range still to do, in order, capped at ``limit`` if given."""
    completed = set(done)
    out: list[date] = []
    for day in days_in_range(start, end):
        if day in completed:
            continue
        out.append(day)
        if limit is not None and len(out) >= limit:
            break
    return out


# --- Candidate CSVs -----------------------------------------------------------------


def format_candidate_row(candidate: Candidate) -> tuple[str, str, str, str]:
    """One CSV row. Coordinates to 3 decimals -- CMG cell centres are exact there."""
    return (
        candidate.day.isoformat(),
        f"{candidate.cell_lat:.3f}",
        f"{candidate.cell_lon:.3f}",
        f"{candidate.max_c:.2f}",
    )


class CandidateWriter:
    """Appends candidate rows to a CSV per product per year, opened on demand.

    One handle stays open per year, which for a chronological scan means one
    handle at a time. Each day's rows are flushed and fsynced before the day is
    marked done.
    """

    def __init__(self, work_dir: Path, product: str) -> None:
        self.work_dir = Path(work_dir)
        self.product = product
        self._handles: dict[int, object] = {}
        self._writers: dict[int, object] = {}

    def _writer_for(self, year: int):
        if year not in self._writers:
            path = candidates_path(self.work_dir, self.product, year)
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists() or path.stat().st_size == 0
            handle = open(path, "a", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(CANDIDATE_HEADER)
            self._handles[year] = handle
            self._writers[year] = writer
        return self._writers[year]

    def write_day(self, day: date, candidates: Sequence[Candidate]) -> int:
        """Append one day's candidates and make them durable. Returns the count."""
        if not candidates:
            return 0
        writer = self._writer_for(day.year)
        for candidate in candidates:
            writer.writerow(format_candidate_row(candidate))  # type: ignore[attr-defined]
        handle = self._handles[day.year]
        handle.flush()  # type: ignore[attr-defined]
        os.fsync(handle.fileno())  # type: ignore[attr-defined]
        return len(candidates)

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()  # type: ignore[attr-defined]
        self._handles.clear()
        self._writers.clear()

    def __enter__(self) -> "CandidateWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass(frozen=True)
class CandidateRow:
    """A candidate read back off disk."""

    day: date
    cell_lat: float
    cell_lon: float
    max_c: float

    def key(self) -> tuple[date, float, float]:
        return (self.day, self.cell_lat, self.cell_lon)


def parse_candidate_rows(rows: Iterable[Sequence[str]]) -> list[CandidateRow]:
    """Parse CSV rows into candidates, skipping the header and unreadable lines.

    Unreadable lines are skipped rather than fatal for the same reason the
    done-log tolerates them: a run killed mid-write can leave a partial final
    line, and that must not make the whole year's file unreadable.
    """
    out: list[CandidateRow] = []
    for row in rows:
        if len(row) < 4:
            continue
        if row[0] == CANDIDATE_HEADER[0]:
            continue
        try:
            out.append(
                CandidateRow(
                    day=date.fromisoformat(row[0]),
                    cell_lat=float(row[1]),
                    cell_lon=float(row[2]),
                    max_c=float(row[3]),
                )
            )
        except ValueError:
            continue
    return out


def dedupe_candidates(rows: Iterable[CandidateRow]) -> list[CandidateRow]:
    """Collapse repeats of the same (day, cell), keeping the hottest value.

    A day reprocessed after an interrupted run appends its rows a second time.
    Deduplicating on read is what keeps that from showing up as two records in
    the same place on the same day.

    Reading both products at once collapses the same way, and that is deliberate
    rather than incidental: Terra and Aqua see the same cell on the same day
    from different overpasses, and a candidate list exists to say which
    (place, day) pairs Pass 1 wants Pass 2 to revisit. Revisiting one of them
    twice buys nothing, and the hotter of the two readings is the better lead.
    The per-product CSVs keep both values for anyone who wants them.
    """
    best: dict[tuple[date, float, float], CandidateRow] = {}
    for row in rows:
        current = best.get(row.key())
        if current is None or row.max_c > current.max_c:
            best[row.key()] = row
    return list(best.values())


def read_candidate_files(paths: Iterable[Path]) -> list[CandidateRow]:
    """Read and deduplicate every candidate row across a set of CSVs."""
    collected: list[CandidateRow] = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as handle:
            collected.extend(parse_candidate_rows(csv.reader(handle)))
    return dedupe_candidates(collected)


def candidate_files(work_dir: Path, product: str | None = None) -> list[Path]:
    """Every candidate CSV in the work dir, optionally narrowed to one product."""
    directory = candidates_dir(work_dir)
    if not directory.exists():
        return []
    pattern = f"candidates_{product}_*.csv" if product else "candidates_*.csv"
    return sorted(directory.glob(pattern))
