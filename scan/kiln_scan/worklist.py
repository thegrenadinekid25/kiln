"""Turning a finished Pass 1 grid into a list of 1 km refinement jobs.

Pass 1 leaves an all-time maximum for every 0.05-degree cell on Earth and the
date that set it. Pass 2 re-measures the interesting ones at 1 km, which is
expensive per day: it means fetching MODIS L2 swath granules. So the point of
this module is to turn a scattered set of hot cells into as few (date, region)
jobs as the geography allows, without ever quietly widening a job's date.

The date is the load-bearing part. A cell's record date is the day its maximum
was observed, and that is the only day worth refining for that cell -- refining
it on a neighbouring date would measure a different thing and attribute it to
the record. Cells are therefore grouped by date first and clustered
geographically only within a date, which is why the same date can legitimately
produce several far-apart boxes and why two adjacent cells with different record
dates stay in separate jobs.

Everything here is pure: arrays and dataclasses in, dataclasses out. No file,
no network, no subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np

from .grid import (
    CENTI_PER_DEGREE,
    CMG_CELL_DEGREES,
    UNSET_DATE,
    AlltimeGrid,
    cell_center_lat,
    cell_center_lon,
    int_to_date,
)

# Two cells this far apart or closer end up in the same bounding box. Chosen
# against what a MODIS L2 granule actually covers: a swath granule is about
# 2330 km across and 5 minutes long, so a box a few degrees wide is fetched by
# the same one or two granules that a single cell would have needed. Merging
# below this is free; merging far beyond it starts pulling in granules that
# cover nothing anyone asked about.
DEFAULT_MERGE_DEGREES = 3.0

# Every final box grows by this much on all four sides. The CMG cell is 5.6 km
# and its recorded maximum is an average over that footprint, so the 1 km peak
# it stands for can sit anywhere inside the cell or just outside it. Half a
# degree is roughly 55 km of margin -- enough that a box cannot clip the feature
# it was drawn for.
DEFAULT_PAD_DEGREES = 0.5

# Rough number of L2 granules one bounding box pulls, per product, used only to
# print an expectation before a backfill starts. Counted per box rather than per
# job because it is the box that intersects granules: a job covering the Sahara
# and Australia on the same day fetches both regions' overpasses. A small box
# usually catches the one or two 5-minute granules that crossed it; a box
# several degrees across can catch four or five.
#
# It is an order-of-magnitude figure, not a forecast. A job here can hold a box
# nine degrees on a side, and that one will pull more than the high end.
GRANULES_PER_BOX_LOW = 1
GRANULES_PER_BOX_HIGH = 4


@dataclass(frozen=True)
class Bbox:
    """A west/south/east/north box in degrees, never crossing the antimeridian."""

    west: float
    south: float
    east: float
    north: float

    def as_list(self) -> list[float]:
        return [
            round(self.west, 3),
            round(self.south, 3),
            round(self.east, 3),
            round(self.north, 3),
        ]

    def as_arg(self) -> str:
        """The ``W,S,E,N`` string the ingest CLI's ``--bbox`` expects."""
        west, south, east, north = self.as_list()
        return f"{west:g},{south:g},{east:g},{north:g}"

    @property
    def width(self) -> float:
        return self.east - self.west

    @property
    def height(self) -> float:
        return self.north - self.south

    def union(self, other: "Bbox") -> "Bbox":
        return Bbox(
            west=min(self.west, other.west),
            south=min(self.south, other.south),
            east=max(self.east, other.east),
            north=max(self.north, other.north),
        )

    def gap_to(self, other: "Bbox") -> tuple[float, float]:
        """Empty space between two boxes as ``(lat_gap, lon_gap)``; 0 if they touch.

        Longitude is compared linearly, so two boxes either side of the
        antimeridian read as 360 degrees apart rather than adjacent. That is
        deliberate: see :func:`cluster_boxes`.
        """
        lat_gap = max(0.0, max(self.south, other.south) - min(self.north, other.north))
        lon_gap = max(0.0, max(self.west, other.west) - min(self.east, other.east))
        return lat_gap, lon_gap

    def is_within(self, other: "Bbox", degrees: float) -> bool:
        lat_gap, lon_gap = self.gap_to(other)
        return lat_gap <= degrees and lon_gap <= degrees


def cell_box(lat: float, lon: float) -> Bbox:
    """The footprint of one CMG cell, from its centre.

    The cell is 0.05 degrees on a side and its recorded value is an average over
    all of it, so a job drawn from the cell should cover the cell, not the point
    at its middle.
    """
    half = CMG_CELL_DEGREES / 2.0
    return Bbox(west=lon - half, south=lat - half, east=lon + half, north=lat + half)


def pad_box(box: Bbox, degrees: float) -> Bbox:
    """Grow a box on all sides, clamping latitude to the poles.

    Longitude is deliberately left unclamped here so the caller can see that the
    box ran past 180; :func:`split_at_antimeridian` is what resolves it.
    """
    if degrees < 0:
        raise ValueError(f"padding must be non-negative, got {degrees}")
    return Bbox(
        west=box.west - degrees,
        south=max(-90.0, box.south - degrees),
        east=box.east + degrees,
        north=min(90.0, box.north + degrees),
    )


def split_at_antimeridian(box: Bbox) -> list[Bbox]:
    """Cut a box that ran past +-180 into pieces that do not, west piece first.

    The ingest CLI takes boxes in plain west/south/east/north degrees with no
    wrapping convention, so a box with an east edge at 180.4 would either be
    rejected or, worse, read as spanning almost the whole globe backwards.
    Splitting is the only representation that keeps the meaning.
    """
    if box.width >= 360.0:
        return [Bbox(-180.0, box.south, 180.0, box.north)]

    if box.east > 180.0:
        return [
            Bbox(box.west, box.south, 180.0, box.north),
            Bbox(-180.0, box.south, box.east - 360.0, box.north),
        ]
    if box.west < -180.0:
        return [
            Bbox(box.west + 360.0, box.south, 180.0, box.north),
            Bbox(-180.0, box.south, box.east, box.north),
        ]
    return [box]


def _merge_pass(boxes: list[Bbox], merge_degrees: float) -> tuple[list[Bbox], bool]:
    """One sweep merging every pair of boxes that are close enough."""
    merged: list[Bbox] = []
    changed = False
    for box in boxes:
        for index, existing in enumerate(merged):
            if box.is_within(existing, merge_degrees):
                merged[index] = existing.union(box)
                changed = True
                break
        else:
            merged.append(box)
    return merged, changed


def cluster_boxes(
    points: Sequence[tuple[float, float]],
    merge_degrees: float = DEFAULT_MERGE_DEGREES,
    pad_degrees: float = DEFAULT_PAD_DEGREES,
) -> list[Bbox]:
    """Group ``(lat, lon)`` cell centres into padded bounding boxes.

    Greedy: each cell joins the first cluster it is within ``merge_degrees`` of,
    growing that cluster's box, or starts a new one. Because growing a cluster
    can bring it within reach of another, the merge is then repeated until no
    two clusters are close enough, which makes the result independent of how the
    greedy pass happened to order things.

    Clustering is linear in longitude, so a group of cells straddling the
    antimeridian comes out as two clusters rather than one. Two jobs there
    instead of one is a rounding error in cost, and the alternative -- wrap-aware
    interval arithmetic through the padding, splitting and union -- is a lot of
    machinery for ocean.

    Returned west-to-south sorted, so the same input always yields the same
    jobs.
    """
    if merge_degrees < 0:
        raise ValueError(f"merge distance must be non-negative, got {merge_degrees}")
    if not points:
        return []

    ordered = sorted((float(lat), float(lon)) for lat, lon in points)
    boxes = [cell_box(lat, lon) for lat, lon in ordered]

    changed = True
    while changed:
        boxes, changed = _merge_pass(boxes, merge_degrees)

    final: list[Bbox] = []
    for box in boxes:
        final.extend(split_at_antimeridian(pad_box(box, pad_degrees)))
    return sorted(final, key=lambda b: (b.west, b.south, b.east, b.north))


# --- Reading the grid ---------------------------------------------------------------


@dataclass(frozen=True)
class HotCells:
    """Cells at or above the bar, held columnar because there can be many.

    One entry per cell, not per observation: the all-time grid keeps a single
    maximum and a single date for each cell, so a cell appears exactly once.
    """

    lat: np.ndarray
    lon: np.ndarray
    date_int: np.ndarray
    max_c: np.ndarray

    def __len__(self) -> int:
        return int(self.lat.size)

    def group_by_date(self) -> Iterator[tuple[date, np.ndarray]]:
        """Yield ``(day, indices)`` for each distinct record date, earliest first."""
        for value in np.unique(self.date_int):
            day = int_to_date(int(value))
            if day is None:
                continue
            yield day, np.nonzero(self.date_int == value)[0]


def extract_hot_cells(grid: AlltimeGrid, bar_c: float) -> tuple[HotCells, int]:
    """Every cell whose all-time maximum reaches ``bar_c``, with its record date.

    Returns the cells and a count of those dropped for having no usable date. A
    cell with a maximum but no date cannot be refined -- there is no day to ask
    the 1 km archive about -- so it is reported rather than guessed at.
    """
    bar_centi = int(np.ceil(bar_c * CENTI_PER_DEGREE))
    selected = grid.observed() & (grid.max_centi >= bar_centi)
    rows, cols = np.nonzero(selected)

    date_int = grid.date_int[rows, cols]
    datable = date_int != UNSET_DATE
    undatable = int(np.count_nonzero(~datable))

    rows, cols, date_int = rows[datable], cols[datable], date_int[datable]
    return (
        HotCells(
            lat=cell_center_lat(rows),
            lon=cell_center_lon(cols),
            date_int=date_int.astype(np.int32),
            max_c=grid.max_centi[rows, cols].astype(np.float64) / CENTI_PER_DEGREE,
        ),
        undatable,
    )


# --- Jobs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One date's refinement work: the boxes to fetch and why."""

    day: date
    bboxes: tuple[Bbox, ...]
    cells: int
    cmg_max_c: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "bboxes": [box.as_list() for box in self.bboxes],
            "cells": self.cells,
            "cmg_max_c": round(self.cmg_max_c, 2),
        }


def build_jobs(
    cells: HotCells,
    merge_degrees: float = DEFAULT_MERGE_DEGREES,
    pad_degrees: float = DEFAULT_PAD_DEGREES,
) -> list[Job]:
    """One job per record date, hottest first.

    Hottest first so that a ``--limit``ed backfill spends its budget on the days
    most likely to hold a record, and so that a run killed halfway has still
    refined the most interesting dates.
    """
    jobs: list[Job] = []
    for day, indices in cells.group_by_date():
        points = list(zip(cells.lat[indices].tolist(), cells.lon[indices].tolist()))
        boxes = cluster_boxes(points, merge_degrees, pad_degrees)
        jobs.append(
            Job(
                day=day,
                bboxes=tuple(boxes),
                cells=len(indices),
                cmg_max_c=float(cells.max_c[indices].max()),
            )
        )
    return sorted(jobs, key=lambda job: (-job.cmg_max_c, job.day))


def jobs_to_payload(
    jobs: Sequence[Job],
    bar_c: float,
    products: Sequence[str],
    merge_degrees: float,
    pad_degrees: float,
) -> dict[str, Any]:
    """The jobs file: the list plus enough provenance to know what made it."""
    return {
        "bar_c": bar_c,
        "source_products": list(products),
        "merge_degrees": merge_degrees,
        "pad_degrees": pad_degrees,
        "jobs": [job.to_dict() for job in jobs],
    }


def jobs_from_payload(payload: Mapping[str, Any]) -> list[Job]:
    """Read jobs back, refusing anything that would send a malformed box to ingest."""
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ValueError("jobs file has no 'jobs' array")

    jobs: list[Job] = []
    for entry in raw_jobs:
        try:
            day = date.fromisoformat(str(entry["date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"job has no usable date: {entry!r}") from exc

        raw_boxes = entry.get("bboxes")
        if not isinstance(raw_boxes, list) or not raw_boxes:
            raise ValueError(f"job {day.isoformat()} has no bounding boxes")

        boxes: list[Bbox] = []
        for raw in raw_boxes:
            if not isinstance(raw, (list, tuple)) or len(raw) != 4:
                raise ValueError(f"job {day.isoformat()} has a malformed box: {raw!r}")
            box = Bbox(*(float(value) for value in raw))
            if box.south >= box.north:
                raise ValueError(
                    f"job {day.isoformat()} box {box.as_arg()} has south at or above north"
                )
            if box.west >= box.east:
                raise ValueError(
                    f"job {day.isoformat()} box {box.as_arg()} has west at or above east"
                )
            if box.west < -180.0 or box.east > 180.0:
                raise ValueError(
                    f"job {day.isoformat()} box {box.as_arg()} runs off the globe"
                )
            boxes.append(box)

        jobs.append(
            Job(
                day=day,
                bboxes=tuple(boxes),
                cells=int(entry.get("cells", 0)),
                cmg_max_c=float(entry.get("cmg_max_c", 0.0)),
            )
        )
    return jobs


def expected_granule_range(jobs: Sequence[Job], products: int = 2) -> tuple[int, int]:
    """Rough low/high granule count a backfill of these jobs would download.

    Driven by the total number of bounding boxes, since that is what decides how
    many swath granules intersect. Deliberately an order of magnitude rather
    than a forecast: a single box spanning several degrees can exceed the high
    end on its own.
    """
    boxes = sum(len(job.bboxes) for job in jobs) * products
    return boxes * GRANULES_PER_BOX_LOW, boxes * GRANULES_PER_BOX_HIGH


def merge_hot_cells(parts: Iterable[HotCells]) -> HotCells:
    """Concatenate several products' hot cells into one set.

    Terra and Aqua are kept as separate observations of the same place rather
    than collapsed: they may disagree about which day was that cell's hottest,
    and both days are then worth refining.
    """
    parts = [part for part in parts if len(part)]
    if not parts:
        empty_f = np.empty(0, dtype=np.float64)
        return HotCells(
            lat=empty_f,
            lon=empty_f.copy(),
            date_int=np.empty(0, dtype=np.int32),
            max_c=empty_f.copy(),
        )
    return HotCells(
        lat=np.concatenate([p.lat for p in parts]),
        lon=np.concatenate([p.lon for p in parts]),
        date_int=np.concatenate([p.date_int for p in parts]),
        max_c=np.concatenate([p.max_c for p in parts]),
    )
