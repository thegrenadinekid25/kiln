"""HDF4 reading tests, run against a granule this test writes itself.

Skipped where pyhdf is unavailable (it needs libhdf4). CI installs
libhdf4-dev, so these run there; the science-core tests in test_science.py
cover the same maths without any HDF dependency.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("pyhdf", reason="pyhdf requires libhdf4")

from pyhdf.SD import SD, SDC  # noqa: E402

from kiln_ingest.granule import granule_maxima, read_granule  # noqa: E402
from kiln_ingest.science import UnexpectedGranuleError  # noqa: E402

KELVIN_ZERO_C = 273.15


def counts(celsius: float) -> int:
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


def write_granule(path, *, include_qc: bool = True, scale_factor: float = 0.02):
    """Write a miniature MOD11_L2-shaped granule: 10x10 LST/QC over a 2x2 geogrid."""
    raw = np.full((10, 10), counts(35.0), dtype=np.uint16)
    raw[0, 0] = counts(61.5)  # hottest good pixel
    raw[1, 1] = counts(95.0)  # hotter, but QC says cloud
    raw[2, 2] = 0  # fill
    qc = np.zeros((10, 10), dtype=np.uint8)
    qc[1, 1] = 0b00000010  # not produced, cloud

    lat_sub = np.array([[10.25, 10.25], [11.25, 11.25]], dtype=np.float32)
    lon_sub = np.array([[20.25, 21.25], [20.25, 21.25]], dtype=np.float32)

    sd = SD(str(path), SDC.WRITE | SDC.CREATE)
    try:
        lst = sd.create("LST", SDC.UINT16, raw.shape)
        lst[:] = raw
        setattr(lst, "scale_factor", scale_factor)
        setattr(lst, "add_offset", 0.0)
        lst.setfillvalue(0)
        lst.endaccess()

        if include_qc:
            qc_sds = sd.create("QC", SDC.UINT8, qc.shape)
            qc_sds[:] = qc
            qc_sds.endaccess()

        for name, values in (("Latitude", lat_sub), ("Longitude", lon_sub)):
            sds = sd.create(name, SDC.FLOAT32, values.shape)
            sds[:] = values
            sds.endaccess()
    finally:
        sd.end()
    return path


def test_read_granule_returns_the_four_arrays(tmp_path):
    path = write_granule(tmp_path / "MOD11_L2.test.hdf")

    arrays = read_granule(path)

    assert arrays.raw_lst.shape == (10, 10)
    assert arrays.qc.shape == (10, 10)
    assert arrays.lat_sub.shape == (2, 2)
    assert arrays.lon_sub.shape == (2, 2)
    assert arrays.lst_attrs["scale_factor"] == pytest.approx(0.02)
    assert int(arrays.lst_attrs["_FillValue"]) == 0


def test_granule_maxima_reads_disk_and_applies_qc(tmp_path):
    path = write_granule(tmp_path / "MOD11_L2.test.hdf")

    tiles = granule_maxima(path, "MOD11_L2.A2026242.1125.061.NRT.hdf", "2026-08-30T11:25:00Z")

    hottest = tiles[(10, 20)]
    assert hottest.max_c == pytest.approx(61.5, abs=0.02)
    assert hottest.max_lat == pytest.approx(10.25, abs=1e-4)
    assert hottest.granule_id == "MOD11_L2.A2026242.1125.061.NRT.hdf"
    assert hottest.observed_at == "2026-08-30T11:25:00Z"
    assert len(tiles) == 4


def test_read_granule_reports_a_missing_sds(tmp_path):
    path = write_granule(tmp_path / "no_qc.hdf", include_qc=False)

    with pytest.raises(UnexpectedGranuleError, match="missing expected SDSs"):
        read_granule(path)


def test_granule_maxima_refuses_an_unexpected_scale_factor(tmp_path):
    path = write_granule(tmp_path / "bad_scale.hdf", scale_factor=0.1)

    with pytest.raises(UnexpectedGranuleError, match="scale_factor"):
        granule_maxima(path, "G", "2026-08-30T11:25:00Z")
