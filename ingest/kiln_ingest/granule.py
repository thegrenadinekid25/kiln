"""HDF4-EOS reading for MOD11_L2 / MYD11_L2 and MOD14_L2 / MYD14_L2 granules.

This is the only module that needs pyhdf, and it imports it lazily inside the
read functions. That keeps the science core in :mod:`kiln_ingest.science`
importable -- and its tests runnable -- on machines without libhdf4 installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .science import (
    QC_NOTE,
    Anomaly,
    GranuleField,
    TileMax,
    UnexpectedGranuleError,
    VolcanicSource,
    granule_anomalies,
    prepare_granule,
    tile_maxima,
)

LST_SDS = "LST"
QC_SDS = "QC"
LATITUDE_SDS = "Latitude"
LONGITUDE_SDS = "Longitude"

# MOD14/MYD14 record detections as vectors, one entry per fire pixel, rather
# than as a raster.
FIRE_LATITUDE_SDS = "FP_latitude"
FIRE_LONGITUDE_SDS = "FP_longitude"


@dataclass(frozen=True)
class GranuleArrays:
    raw_lst: np.ndarray
    lst_attrs: Mapping[str, Any] = field(repr=False)
    qc: np.ndarray
    lat_sub: np.ndarray
    lon_sub: np.ndarray


def _import_pyhdf() -> Any:
    try:
        from pyhdf.SD import SD, SDC  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "pyhdf is required to read granules. Install libhdf4 development "
            "headers (apt-get install libhdf4-dev) and then "
            "'pip install -r requirements.txt'."
        ) from exc
    return SD, SDC


def read_granule(path: Path) -> GranuleArrays:
    """Read the four SDSs Kiln needs out of one HDF4-EOS granule."""
    SD, SDC = _import_pyhdf()

    handle = SD(str(path), SDC.READ)
    try:
        available = set(handle.datasets())
        missing = {LST_SDS, QC_SDS, LATITUDE_SDS, LONGITUDE_SDS} - available
        if missing:
            raise UnexpectedGranuleError(
                f"{path.name} is missing expected SDSs: {sorted(missing)}"
            )

        lst_sds = handle.select(LST_SDS)
        try:
            raw_lst = np.asarray(lst_sds.get())
            lst_attrs = dict(lst_sds.attributes())
            if "_FillValue" not in lst_attrs:
                # pyhdf surfaces the HDF fill value through getfillvalue() rather
                # than attributes(). Absent both, science.py falls back to 0,
                # which is the documented MOD11/MYD11 fill.
                try:
                    lst_attrs["_FillValue"] = lst_sds.getfillvalue()
                except Exception:  # noqa: BLE001 - no fill value recorded on this SDS
                    pass
        finally:
            lst_sds.endaccess()

        arrays = {}
        for name in (QC_SDS, LATITUDE_SDS, LONGITUDE_SDS):
            sds = handle.select(name)
            try:
                arrays[name] = np.asarray(sds.get())
            finally:
                sds.endaccess()
    finally:
        handle.end()

    return GranuleArrays(
        raw_lst=raw_lst,
        lst_attrs=lst_attrs,
        qc=arrays[QC_SDS],
        lat_sub=arrays[LATITUDE_SDS],
        lon_sub=arrays[LONGITUDE_SDS],
    )


def read_fire_granule(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Fire-pixel latitudes and longitudes from one MOD14_L2 / MYD14_L2 granule.

    A granule over an overpass with no detections is a normal, correct granule:
    it may omit the ``FP_`` SDSs entirely or carry zero-length ones. Both mean
    "nothing burning here", so both return empty arrays rather than raising --
    the caller must be able to tell that apart from a granule it could not read,
    which is what decides whether a tile is marked fire-masked or unchecked.
    """
    SD, SDC = _import_pyhdf()

    handle = SD(str(path), SDC.READ)
    try:
        available = set(handle.datasets())
        if not {FIRE_LATITUDE_SDS, FIRE_LONGITUDE_SDS} <= available:
            return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.float64)

        vectors: dict[str, np.ndarray] = {}
        for name in (FIRE_LATITUDE_SDS, FIRE_LONGITUDE_SDS):
            sds = handle.select(name)
            try:
                vectors[name] = np.atleast_1d(
                    np.asarray(sds.get(), dtype=np.float64)
                ).ravel()
            except Exception:  # noqa: BLE001 - pyhdf raises on a zero-length SDS
                vectors[name] = np.empty(0, dtype=np.float64)
            finally:
                sds.endaccess()
    finally:
        handle.end()

    fire_lat = vectors[FIRE_LATITUDE_SDS]
    fire_lon = vectors[FIRE_LONGITUDE_SDS]
    if fire_lat.size != fire_lon.size:
        raise UnexpectedGranuleError(
            f"{path.name} has {fire_lat.size} fire latitudes but {fire_lon.size} longitudes"
        )
    return fire_lat, fire_lon


@dataclass(frozen=True)
class GranuleReduction:
    """What one granule contributes to every output."""

    tiles: dict[tuple[int, int], TileMax]
    pixels: GranuleField = field(repr=False)

    # The non-weather readings this granule saw: volcanic vents and notable
    # active fires, keyed by (tile lat, tile lon, cause).
    anomalies: dict[tuple[int, int, str], Anomaly] = field(default_factory=dict)


def granule_reduction(
    path: Path,
    granule_id: str,
    observed_at: str,
    fire_exclusion: np.ndarray | None = None,
    qc_note: str = QC_NOTE,
    volcanic_sources: Sequence[VolcanicSource] | None = None,
) -> GranuleReduction:
    """Read a granule from disk and reduce it to tile maxima plus its pixel field.

    The field is what the raster pyramid paints from, and it is the same masked
    field the tile maxima came out of, so no pixel can reach one output having
    been excluded from the other -- for fire, for volcanoes, for implausibility,
    or for QC. The anomalies come out of the same field's exclusions, which is
    what keeps the weather and non-weather sections disjoint.
    """
    arrays = read_granule(path)
    granule_field = prepare_granule(
        raw_lst=arrays.raw_lst,
        lst_attrs=arrays.lst_attrs,
        qc=arrays.qc,
        lat_sub=arrays.lat_sub,
        lon_sub=arrays.lon_sub,
        fire_exclusion=fire_exclusion,
        volcanic_sources=volcanic_sources,
    )
    tiles = tile_maxima(
        granule_field.celsius,
        granule_field.keep,
        granule_field.lat,
        granule_field.lon,
        observed_at=observed_at,
        granule_id=granule_id,
        qc_note=qc_note,
        fire_tiles=granule_field.fire_tiles,
        outlier_tiles=granule_field.outlier_tiles,
        volcanic_tiles=granule_field.volcanic_tiles,
    )
    anomalies = granule_anomalies(
        granule_field,
        observed_at=observed_at,
        granule_id=granule_id,
        volcanic_sources=volcanic_sources,
    )
    return GranuleReduction(tiles=tiles, pixels=granule_field, anomalies=anomalies)


def granule_maxima(
    path: Path, granule_id: str, observed_at: str
) -> dict[tuple[int, int], TileMax]:
    """Per-tile maxima for one granule, with no fire mask and no raster output."""
    return granule_reduction(path, granule_id, observed_at).tiles
