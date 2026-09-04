"""Pure science core for the historical CMG scan.

Everything here operates on plain numpy arrays and mappings: nothing opens a
file, touches the network, or imports pyhdf, so the whole scientific path --
scaling, QC masking, plausibility, candidate selection -- is testable with
synthetic arrays on a machine with no libhdf4.

Reference: MODIS Land Surface Temperature and Emissivity Daily L3 Global
0.05 Deg CMG, MOD11C1 (Terra) and MYD11C1 (Aqua), collection 6.1.

The policy constants below are deliberately duplicated from
``ingest/kiln_ingest/science.py`` rather than imported. The two tools are
separate deployables, and a copy that drifts visibly in a diff is safer than an
import that couples a 24-year batch sweep to a job that runs every morning.
Any change to the QC bar or the plausibility band belongs in both.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

import numpy as np

from .grid import cell_center_lat, cell_center_lon, check_cmg_shape, row_center_lats

# --- Documented product constants ---------------------------------------------------

# MOD11C1/MYD11C1 store LST as uint16 with a fixed 0.02 K scale factor. We read
# the real value out of the SDS attributes and check it against this rather than
# multiplying blindly: a collection change that altered the scaling would
# otherwise silently produce temperatures off by orders of magnitude.
EXPECTED_LST_SCALE_FACTOR = 0.02
DEFAULT_LST_FILL = 0
KELVIN_ZERO_C = 273.15

# Plausibility band applied after scaling. Real land-surface temperatures on
# Earth live far inside this; anything outside means the granule is corrupt or
# the scaling assumption broke, and we would rather drop the cell than record a
# nonsense candidate.
PHYSICAL_MIN_C = -150.0
PHYSICAL_MAX_C = 200.0

# Human-readable record of the QC policy applied.
QC_NOTE = "mandatory QA 00/01; LST error flag <= 2K"


class UnexpectedGranuleError(ValueError):
    """A granule's metadata does not match the documented MOD11C1/MYD11C1 layout."""


# --- Scaling ------------------------------------------------------------------------


@dataclass(frozen=True)
class LstScaling:
    scale_factor: float
    add_offset: float
    fill_value: int
    valid_range: tuple[int, int] | None = None


def resolve_lst_scaling(attrs: Mapping[str, object]) -> LstScaling:
    """Pull scaling metadata off the LST SDS attributes, refusing surprises.

    A missing or unexpected ``scale_factor`` is fatal: it is the single value
    that turns stored integers into kelvin, and guessing it wrong is worse than
    not running at all.

    ``valid_range`` is carried through when the SDS declares one (MOD11C1 states
    7500..65535, i.e. 150 K up). It is a per-file statement of what the producer
    considers real, so it is a tighter and better-sourced gate than our own
    physical band, and we apply both.
    """
    if "scale_factor" not in attrs:
        raise UnexpectedGranuleError("LST SDS has no scale_factor attribute")

    scale_factor = float(attrs["scale_factor"])  # type: ignore[arg-type]
    if not math.isclose(scale_factor, EXPECTED_LST_SCALE_FACTOR, rel_tol=1e-6):
        raise UnexpectedGranuleError(
            f"LST scale_factor is {scale_factor}, expected "
            f"{EXPECTED_LST_SCALE_FACTOR}; refusing to guess the units"
        )

    add_offset = float(attrs.get("add_offset", 0.0))  # type: ignore[arg-type]
    fill_value = int(attrs.get("_FillValue", DEFAULT_LST_FILL))  # type: ignore[arg-type]

    valid_range: tuple[int, int] | None = None
    declared = attrs.get("valid_range")
    if declared is not None:
        values = list(declared)  # type: ignore[call-overload]
        if len(values) != 2:
            raise UnexpectedGranuleError(
                f"LST valid_range has {len(values)} entries, expected 2"
            )
        low, high = int(values[0]), int(values[1])
        if low > high:
            raise UnexpectedGranuleError(f"LST valid_range {low}..{high} is inverted")
        valid_range = (low, high)

    return LstScaling(
        scale_factor=scale_factor,
        add_offset=add_offset,
        fill_value=fill_value,
        valid_range=valid_range,
    )


def decode_lst_celsius(
    raw_lst: np.ndarray, scaling: LstScaling
) -> tuple[np.ndarray, np.ndarray]:
    """Convert raw stored LST counts to Celsius plus a validity mask.

    Returns ``(celsius, valid)`` where ``valid`` is False for fill cells, for
    counts outside any declared ``valid_range``, and for anything outside the
    physical plausibility band.
    """
    raw = np.asarray(raw_lst)
    kelvin = raw.astype(np.float64) * scaling.scale_factor + scaling.add_offset
    celsius = kelvin - KELVIN_ZERO_C

    valid = raw != scaling.fill_value
    if scaling.valid_range is not None:
        low, high = scaling.valid_range
        valid &= (raw >= low) & (raw <= high)
    valid &= np.isfinite(celsius)
    valid &= (celsius >= PHYSICAL_MIN_C) & (celsius <= PHYSICAL_MAX_C)
    return celsius, valid


# --- Quality control ----------------------------------------------------------------


def qc_keep_mask(qc: np.ndarray, max_error_class: int = 1) -> np.ndarray:
    """Cells whose QC byte clears the Kiln quality bar.

    MOD11C1/MYD11C1 QC_Day carries the same mandatory-QA layout as the L2
    products:

    * bits 0-1 mandatory QA: 00 produced good quality, 01 produced other
      quality, 10 not produced (cloud), 11 not produced (other).
    * bits 6-7 LST error flag: 00 average error <= 1K, 01 <= 2K, 10 <= 3K,
      11 > 3K.

    We keep a cell when mandatory QA says the LST was actually produced (00 or
    01) and the error flag is at or below ``max_error_class`` -- 1 by default,
    i.e. average error <= 2K.

    Note that QC_Day declares ``_FillValue`` 0, which is also the byte for
    "good quality, error <= 1K". The fill value is therefore useless as a mask
    here; whether a cell has data is decided by the LST fill, and this function
    only ever narrows that.
    """
    qc_bytes = np.asarray(qc).astype(np.uint8)
    mandatory = qc_bytes & 0b11
    error_class = (qc_bytes >> 6) & 0b11
    return (mandatory <= 1) & (error_class <= max_error_class)


# --- Latitude plausibility screen ---------------------------------------------------

# The same physical rule the daily pipeline applies, for the same reason: an
# undetected subpixel fire or a scan artifact can put an impossible temperature
# at a high latitude, and one of those put a 78.75 C reading at 64.96 N in
# Siberia on Kiln's map.
#
# The band is deliberately a single conservative combination rather than a
# latitude-dependent curve:
#
# * The verified global maximum land-surface temperature is 80.8 C, at about
#   31 N (Lut Desert and Sonoran Desert, Zhao et al. 2021).
# * The Turpan Depression at 42.9 N legitimately exceeds 65 C.
# * Poleward of 50 degrees, no verified LST above 60 C has ever been observed.
#
# So the screen fires only on both extremes at once. A tighter band at lower
# latitudes would clip real records, which is a far worse error than leaving a
# rare high-latitude artifact in: this map's whole claim is that its numbers are
# real measurements.
#
# The CMG has no fire product analogue to MOD14 -- there is no daily 0.05-degree
# fire mask in this lineage -- so on this path the screen is the only backstop,
# and Pass 2 is where a candidate gets checked against 1 km data.
HIGH_LATITUDE_DEGREES = 50.0
HIGH_LATITUDE_MAX_C = 60.0


def plausibility_keep_mask(celsius: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Cells that survive the latitude plausibility screen.

    True keeps the cell. Only cells both poleward of
    :data:`HIGH_LATITUDE_DEGREES` and hotter than :data:`HIGH_LATITUDE_MAX_C`
    are rejected; everything else passes untouched, at every latitude.

    ``lat`` may be a (3600, 1) column vector, which broadcasts against a full
    CMG field without materialising a second 3600x7200 array.
    """
    celsius = np.asarray(celsius, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    implausible = (np.abs(lat) > HIGH_LATITUDE_DEGREES) & (celsius > HIGH_LATITUDE_MAX_C)
    return ~implausible


# --- Whole-day composition ----------------------------------------------------------


@dataclass(frozen=True)
class DayField:
    """One CMG day decoded to Celsius, with everything unusable already masked.

    ``celsius`` is meaningful only where ``keep`` is True; elsewhere it holds
    whatever the arithmetic produced from fill counts and must not be read.
    """

    celsius: np.ndarray
    keep: np.ndarray
    qc_dropped: int
    implausible_dropped: int

    @property
    def kept(self) -> int:
        return int(np.count_nonzero(self.keep))


def prepare_day(
    raw_lst: np.ndarray,
    lst_attrs: Mapping[str, object],
    qc: np.ndarray,
    max_error_class: int = 1,
) -> DayField:
    """Full science path for one CMG day, from raw SDS arrays to a masked field.

    Kept free of file and network I/O so tests can drive it with fabricated
    arrays; :mod:`kiln_scan.hdf` supplies the real ones.
    """
    raw = np.asarray(raw_lst)
    qc_arr = np.asarray(qc)
    check_cmg_shape(raw, "LST_Day_CMG")
    check_cmg_shape(qc_arr, "QC_Day")

    scaling = resolve_lst_scaling(lst_attrs)
    celsius, valid = decode_lst_celsius(raw, scaling)

    quality = qc_keep_mask(qc_arr, max_error_class=max_error_class)
    after_qc = valid & quality
    qc_dropped = int(np.count_nonzero(valid & ~quality))

    plausible = plausibility_keep_mask(celsius, row_center_lats())
    keep = after_qc & plausible
    implausible_dropped = int(np.count_nonzero(after_qc & ~plausible))

    return DayField(
        celsius=celsius,
        keep=keep,
        qc_dropped=qc_dropped,
        implausible_dropped=implausible_dropped,
    )


# --- Candidate selection ------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One cell that reached the bar on one day."""

    day: date
    cell_lat: float
    cell_lon: float
    max_c: float


def select_candidates(field: DayField, day: date, bar_c: float) -> list[Candidate]:
    """Every kept cell at or above ``bar_c``, hottest first.

    Ordered hottest first so a truncated file still holds the most interesting
    rows, and so the head of any year's CSV is directly readable.
    """
    hot = field.keep & (np.asarray(field.celsius, dtype=np.float64) >= bar_c)
    rows, cols = np.nonzero(hot)
    if rows.size == 0:
        return []

    values = np.asarray(field.celsius, dtype=np.float64)[rows, cols]
    order = np.lexsort((cols, rows, -values))
    lats = cell_center_lat(rows[order])
    lons = cell_center_lon(cols[order])
    return [
        Candidate(
            day=day,
            cell_lat=float(lat),
            cell_lon=float(lon),
            max_c=float(value),
        )
        for lat, lon, value in zip(lats, lons, values[order])
    ]


def day_maximum(field: DayField) -> tuple[float, float, float] | None:
    """The day's hottest kept cell as ``(celsius, lat, lon)``, or None if empty."""
    if not field.keep.any():
        return None
    masked = np.where(field.keep, np.asarray(field.celsius, dtype=np.float64), -np.inf)
    flat = int(np.argmax(masked))
    row, col = divmod(flat, masked.shape[1])
    return (
        float(masked[row, col]),
        float(cell_center_lat(row)),
        float(cell_center_lon(col)),
    )


def top_candidates(candidates: Sequence[Candidate], top_n: int) -> list[Candidate]:
    """The ``top_n`` hottest candidates, hottest first.

    Ties break by day then position so the ranking is stable across runs.
    """
    if top_n < 0:
        raise ValueError(f"top_n must be non-negative, got {top_n}")
    ranked = sorted(
        candidates,
        key=lambda c: (-c.max_c, c.day, -c.cell_lat, c.cell_lon),
    )
    return ranked[:top_n]
