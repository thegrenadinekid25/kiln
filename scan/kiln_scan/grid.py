"""Geometry of the MODIS climate modeling grid, and the running all-time maximum.

The CMG is a plain equirectangular grid: 7200 columns by 3600 rows of 0.05
degrees, row 0 at the north pole and column 0 at the antimeridian. Every
function here is pure arithmetic on that definition -- no file, no network -- so
the coordinate math the whole scan depends on is provable in tests.

The all-time accumulator is two same-shaped arrays: the hottest Celsius value
ever seen in each cell (int16 hundredths of a degree) and the day that set it
(int32 ``YYYYMMDD``). They are folded and persisted together because a maximum
without its date is a number nobody can check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

# --- Grid definition ----------------------------------------------------------------

CMG_ROWS = 3600
CMG_COLS = 7200
CMG_CELL_DEGREES = 0.05

# Cell centres, not edges. Row 0 spans 90.00 to 89.95 N, so its centre is
# 89.975; column 0 spans 180.00 to 179.95 W, so its centre is -179.975.
CMG_FIRST_ROW_CENTER_LAT = 90.0 - CMG_CELL_DEGREES / 2.0
CMG_FIRST_COL_CENTER_LON = -180.0 + CMG_CELL_DEGREES / 2.0

CMG_SHAPE = (CMG_ROWS, CMG_COLS)


class GridShapeError(ValueError):
    """An array does not have the CMG shape this scanner is built around."""


def check_cmg_shape(array: np.ndarray, name: str) -> None:
    """Refuse an array that is not the documented 3600x7200 CMG raster.

    A collection change that altered the grid would otherwise be read as if it
    were the old one, silently attaching every temperature to the wrong place.
    """
    shape = tuple(np.shape(array))
    if shape != CMG_SHAPE:
        raise GridShapeError(f"{name} has shape {shape}, expected {CMG_SHAPE}")


# Every CMG cell centre is an exact multiple of 0.05 offset by 0.025, so it has
# exactly three decimal places. Rounding there removes the binary-floating-point
# residue (29.574999999999992 for 29.575) that would otherwise leak into every
# CSV row and summary, without moving any coordinate.
CENTER_DECIMALS = 3


def cell_center_lat(row: np.ndarray | int) -> np.ndarray:
    """Latitude of the centre of the given CMG row(s), north positive."""
    raw = CMG_FIRST_ROW_CENTER_LAT - np.asarray(row, dtype=np.float64) * CMG_CELL_DEGREES
    return np.round(raw, CENTER_DECIMALS)


def cell_center_lon(col: np.ndarray | int) -> np.ndarray:
    """Longitude of the centre of the given CMG column(s), east positive."""
    raw = CMG_FIRST_COL_CENTER_LON + np.asarray(col, dtype=np.float64) * CMG_CELL_DEGREES
    return np.round(raw, CENTER_DECIMALS)


def row_center_lats() -> np.ndarray:
    """Centre latitude of every row, as a column vector of shape (3600, 1).

    A column vector rather than a full 3600x7200 array on purpose: latitude
    varies only down the grid, and broadcasting it costs 29 kB where
    materialising it costs 207 MB.
    """
    return cell_center_lat(np.arange(CMG_ROWS)).reshape(CMG_ROWS, 1)


def col_center_lons() -> np.ndarray:
    """Centre longitude of every column, shape (7200,)."""
    return cell_center_lon(np.arange(CMG_COLS))


# --- All-time accumulator -----------------------------------------------------------

# Hundredths of a degree Celsius. The plausibility band (-150..200 C) maps to
# -15000..20000, comfortably inside int16, and 0.01 C is finer than the 0.02 K
# quantisation of the stored product, so the packing loses nothing.
CENTI_PER_DEGREE = 100.0

# "This cell has never had a valid observation." Chosen as int16's floor so it
# loses every comparison against a real temperature without a special case.
UNSET_CENTI = np.iinfo(np.int16).min

# "No day set this cell's maximum", paired with UNSET_CENTI.
UNSET_DATE = 0

MAX_GRID_SUFFIX = ".npz"


def date_to_int(day: date) -> int:
    """``YYYYMMDD`` as an int, readable in a hex dump and sortable as a number."""
    return day.year * 10000 + day.month * 100 + day.day


def int_to_date(value: int) -> date | None:
    """Inverse of :func:`date_to_int`; ``None`` for the unset sentinel."""
    value = int(value)
    if value == UNSET_DATE:
        return None
    return date(value // 10000, (value // 100) % 100, value % 100)


def celsius_to_centi(celsius: np.ndarray) -> np.ndarray:
    """Round Celsius to int16 hundredths, clipped to what int16 can hold."""
    scaled = np.rint(np.asarray(celsius, dtype=np.float64) * CENTI_PER_DEGREE)
    clipped = np.clip(scaled, UNSET_CENTI + 1, np.iinfo(np.int16).max)
    return clipped.astype(np.int16)


@dataclass
class AlltimeGrid:
    """Running per-cell maximum over every day folded in so far.

    ``max_centi`` holds hundredths of a degree Celsius, ``date_int`` the
    ``YYYYMMDD`` of the day that set each maximum. Cells never validly observed
    hold :data:`UNSET_CENTI` and :data:`UNSET_DATE`.
    """

    max_centi: np.ndarray
    date_int: np.ndarray

    @classmethod
    def empty(cls) -> "AlltimeGrid":
        return cls(
            max_centi=np.full(CMG_SHAPE, UNSET_CENTI, dtype=np.int16),
            date_int=np.full(CMG_SHAPE, UNSET_DATE, dtype=np.int32),
        )

    def __post_init__(self) -> None:
        check_cmg_shape(self.max_centi, "all-time max grid")
        check_cmg_shape(self.date_int, "all-time date grid")
        self.max_centi = np.asarray(self.max_centi, dtype=np.int16)
        self.date_int = np.asarray(self.date_int, dtype=np.int32)

    def fold_day(self, celsius: np.ndarray, keep: np.ndarray, day: date) -> int:
        """Fold one day's masked field in, returning how many cells it improved.

        Strictly greater wins, so on a tie the earlier day keeps the
        attribution. Re-folding a day already folded is therefore a no-op, which
        is what makes an interrupted scan safe to resume.
        """
        check_cmg_shape(celsius, "daily Celsius field")
        check_cmg_shape(keep, "daily keep mask")

        candidate = celsius_to_centi(celsius)
        improved = np.asarray(keep, dtype=bool) & (candidate > self.max_centi)
        count = int(np.count_nonzero(improved))
        if count:
            self.max_centi[improved] = candidate[improved]
            self.date_int[improved] = np.int32(date_to_int(day))
        return count

    def observed(self) -> np.ndarray:
        """Mask of cells that have ever had a valid observation."""
        return self.max_centi > UNSET_CENTI

    def global_max(self) -> tuple[float, float, float, date] | None:
        """Hottest cell ever seen: ``(celsius, lat, lon, day)``, or None if empty.

        Ties go to the northernmost then westernmost cell, purely so the answer
        is deterministic.
        """
        if not self.observed().any():
            return None
        flat = int(np.argmax(self.max_centi))
        row, col = divmod(flat, CMG_COLS)
        day = int_to_date(self.date_int[row, col])
        if day is None:
            return None
        return (
            float(self.max_centi[row, col]) / CENTI_PER_DEGREE,
            float(cell_center_lat(row)),
            float(cell_center_lon(col)),
            day,
        )

    def count_at_or_above(self, bar_c: float) -> int:
        """Cells whose all-time maximum reaches ``bar_c``."""
        bar_centi = int(np.ceil(bar_c * CENTI_PER_DEGREE))
        return int(np.count_nonzero(self.observed() & (self.max_centi >= bar_centi)))

    def save(self, path: Path) -> None:
        """Persist both arrays as one file, replaced atomically.

        One file rather than two: a maximum and the date that set it are a pair,
        and a crash between two separate writes would leave a grid claiming a
        temperature on the wrong day. ``os.replace`` on a single archive cannot
        produce that state.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        with open(partial, "wb") as handle:
            np.savez(handle, max_centi=self.max_centi, date_int=self.date_int)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(path)

    @classmethod
    def load(cls, path: Path) -> "AlltimeGrid":
        """Read a saved grid back, or start an empty one if the file is absent."""
        path = Path(path)
        if not path.exists():
            return cls.empty()
        with np.load(path) as archive:
            return cls(max_centi=archive["max_centi"], date_int=archive["date_int"])

    def export_npy(self, max_path: Path, date_path: Path) -> None:
        """Write the two arrays as plain ``.npy`` for downstream consumers."""
        max_path = Path(max_path)
        date_path = Path(date_path)
        max_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(max_path, self.max_centi)
        np.save(date_path, self.date_int)
