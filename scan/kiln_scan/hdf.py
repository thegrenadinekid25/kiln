"""HDF4-EOS reading for MOD11C1 / MYD11C1 daily CMG granules.

This is the only module that needs pyhdf, and it imports it lazily inside the
read function. That keeps the science core in :mod:`kiln_scan.science` and the
grid math in :mod:`kiln_scan.grid` importable -- and their tests runnable -- on
machines without libhdf4 installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .grid import CMG_COLS, CMG_ROWS
from .science import UnexpectedGranuleError

LST_DAY_SDS = "LST_Day_CMG"
QC_DAY_SDS = "QC_Day"

# Present in collection 6.1 and carrying the local overpass time in hours UTC
# (uint8, scale 0.2, fill 255). Optional on purpose: if a file ever lacks it, or
# a cell's entry is fill, the observation keeps its date and gets no time. An
# invented overpass time would be indistinguishable from a measured one.
DAY_VIEW_TIME_SDS = "Day_view_time"

VIEW_TIME_SCALE = 0.2
VIEW_TIME_FILL = 255


@dataclass(frozen=True)
class DayArrays:
    """The arrays one CMG granule contributes, straight off the file."""

    raw_lst: np.ndarray
    lst_attrs: Mapping[str, Any] = field(repr=False)
    qc: np.ndarray
    #: Raw uint8 overpass-time counts, or None when the SDS is absent.
    raw_view_time: np.ndarray | None = None


def _import_pyhdf() -> Any:
    try:
        from pyhdf.SD import SD, SDC  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pyhdf is required to read granules. Install the libhdf4 "
            "development headers (brew install hdf4, or apt-get install "
            "libhdf4-dev) and then 'pip install -r requirements.txt'."
        ) from exc
    return SD, SDC


def read_day(path: Path) -> DayArrays:
    """Read the daytime LST, its QC byte and the overpass time from one granule."""
    SD, SDC = _import_pyhdf()

    handle = SD(str(path), SDC.READ)
    try:
        available = set(handle.datasets())
        missing = {LST_DAY_SDS, QC_DAY_SDS} - available
        if missing:
            raise UnexpectedGranuleError(
                f"{Path(path).name} is missing expected SDSs: {sorted(missing)}"
            )

        lst_sds = handle.select(LST_DAY_SDS)
        try:
            raw_lst = np.asarray(lst_sds.get())
            lst_attrs = dict(lst_sds.attributes())
            if "_FillValue" not in lst_attrs:
                # pyhdf surfaces the HDF fill value through getfillvalue()
                # rather than attributes(). Absent both, science.py falls back
                # to 0, which is the documented MOD11C1/MYD11C1 fill.
                try:
                    lst_attrs["_FillValue"] = lst_sds.getfillvalue()
                except Exception:  # noqa: BLE001 - no fill value recorded on this SDS
                    pass
        finally:
            lst_sds.endaccess()

        qc_sds = handle.select(QC_DAY_SDS)
        try:
            qc = np.asarray(qc_sds.get())
        finally:
            qc_sds.endaccess()

        raw_view_time: np.ndarray | None = None
        if DAY_VIEW_TIME_SDS in available:
            view_sds = handle.select(DAY_VIEW_TIME_SDS)
            try:
                raw_view_time = np.asarray(view_sds.get())
            finally:
                view_sds.endaccess()
    finally:
        handle.end()

    if raw_lst.shape != (CMG_ROWS, CMG_COLS):
        raise UnexpectedGranuleError(
            f"{Path(path).name} LST is {raw_lst.shape}, expected {(CMG_ROWS, CMG_COLS)}"
        )
    if qc.shape != raw_lst.shape:
        raise UnexpectedGranuleError(
            f"{Path(path).name} QC shape {qc.shape} does not match LST {raw_lst.shape}"
        )

    return DayArrays(
        raw_lst=raw_lst,
        lst_attrs=lst_attrs,
        qc=qc,
        raw_view_time=raw_view_time,
    )


def view_time_hours(raw_view_time: np.ndarray | None, row: int, col: int) -> float | None:
    """Overpass time in hours UTC for one cell, or None when it is not recorded.

    Returning None rather than a default is the point: a candidate row with no
    time says the file did not carry one, which is a true statement. A zero
    would say midnight.
    """
    if raw_view_time is None:
        return None
    raw = int(np.asarray(raw_view_time)[row, col])
    if raw == VIEW_TIME_FILL:
        return None
    return raw * VIEW_TIME_SCALE
