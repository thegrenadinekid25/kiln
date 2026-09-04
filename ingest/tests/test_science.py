"""Science-core tests. No network, no HDF files, no credentials.

Every array here is fabricated so the expected answer is known by construction.
"""

from __future__ import annotations

import numpy as np
import pytest

from kiln_ingest.science import (
    HOT_TILE_THRESHOLD_C,
    KELVIN_ZERO_C,
    PHYSICAL_MAX_C,
    TileMax,
    UnexpectedGranuleError,
    decode_lst_celsius,
    expand_geolocation,
    geolocation_valid,
    granule_tile_maxima,
    merge_tile_maxima,
    nearest_source_indices,
    qc_keep_mask,
    resolve_lst_scaling,
    select_reported_tiles,
    tile_indices,
    tile_maxima,
)

GOOD_ATTRS = {"scale_factor": 0.02, "add_offset": 0.0, "_FillValue": 0}

# QC bytes used throughout: bits 0-1 mandatory QA, bits 6-7 LST error class.
QC_GOOD = 0b00000000  # produced, good quality, error <= 1K
QC_ACCEPTABLE = 0b01000001  # produced, other quality, error <= 2K
QC_CLOUD = 0b00000010  # not produced due to cloud
QC_NOT_PRODUCED = 0b00000011  # not produced, other reason
QC_BIG_ERROR = 0b11000000  # produced good quality but error > 3K


def kelvin_counts(celsius: float) -> int:
    """Raw stored count that decodes to a given Celsius temperature."""
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


# --- scaling ------------------------------------------------------------------------


def test_resolve_scaling_reads_attributes():
    scaling = resolve_lst_scaling({"scale_factor": 0.02, "add_offset": 1.5, "_FillValue": 0})
    assert scaling.scale_factor == 0.02
    assert scaling.add_offset == 1.5
    assert scaling.fill_value == 0


def test_resolve_scaling_rejects_unexpected_scale_factor():
    with pytest.raises(UnexpectedGranuleError, match="scale_factor"):
        resolve_lst_scaling({"scale_factor": 0.1})


def test_resolve_scaling_rejects_missing_scale_factor():
    with pytest.raises(UnexpectedGranuleError, match="no scale_factor"):
        resolve_lst_scaling({"_FillValue": 0})


def test_decode_converts_kelvin_to_celsius():
    raw = np.array([[kelvin_counts(50.0), kelvin_counts(-10.0)]], dtype=np.uint16)
    celsius, valid = decode_lst_celsius(raw, resolve_lst_scaling(GOOD_ATTRS))
    assert celsius[0, 0] == pytest.approx(50.0, abs=0.01)
    assert celsius[0, 1] == pytest.approx(-10.0, abs=0.01)
    assert valid.all()


def test_decode_applies_add_offset():
    scaling = resolve_lst_scaling({"scale_factor": 0.02, "add_offset": 10.0, "_FillValue": 0})
    raw = np.array([[kelvin_counts(50.0)]], dtype=np.uint16)
    celsius, _ = decode_lst_celsius(raw, scaling)
    assert celsius[0, 0] == pytest.approx(60.0, abs=0.01)


def test_decode_masks_fill_values():
    raw = np.array([[0, kelvin_counts(45.0)]], dtype=np.uint16)
    _, valid = decode_lst_celsius(raw, resolve_lst_scaling(GOOD_ATTRS))
    assert valid.tolist() == [[False, True]]


def test_decode_masks_physically_impossible_values():
    raw = np.array([[65535, kelvin_counts(45.0)]], dtype=np.uint16)
    celsius, valid = decode_lst_celsius(raw, resolve_lst_scaling(GOOD_ATTRS))
    assert celsius[0, 0] > PHYSICAL_MAX_C
    assert valid.tolist() == [[False, True]]


# --- quality control ----------------------------------------------------------------


def test_qc_keeps_good_and_acceptable_only():
    qc = np.array([[QC_GOOD, QC_ACCEPTABLE, QC_CLOUD, QC_NOT_PRODUCED, QC_BIG_ERROR]],
                  dtype=np.uint8)
    assert qc_keep_mask(qc).tolist() == [[True, True, False, False, False]]


def test_qc_error_class_is_configurable():
    qc = np.array([[0b10000000]], dtype=np.uint8)  # produced, error <= 3K
    assert qc_keep_mask(qc, max_error_class=1).tolist() == [[False]]
    assert qc_keep_mask(qc, max_error_class=2).tolist() == [[True]]


# --- geolocation --------------------------------------------------------------------


def test_nearest_source_indices_spreads_across_the_coarse_axis():
    idx = nearest_source_indices(10, 2)
    assert idx.tolist() == [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]


def test_nearest_source_indices_is_identity_at_equal_length():
    assert nearest_source_indices(4, 4).tolist() == [0, 1, 2, 3]


def test_expand_geolocation_upsamples_to_the_lst_grid():
    lat_sub = np.array([[10.0, 11.0], [20.0, 21.0]])
    lon_sub = np.array([[30.0, 31.0], [40.0, 41.0]])
    lat, lon = expand_geolocation(lat_sub, lon_sub, (4, 4))
    assert lat.shape == (4, 4)
    assert lat[0, 0] == 10.0
    assert lat[3, 3] == 21.0
    assert lon[0, 3] == 31.0


def test_expand_geolocation_rejects_mismatched_shapes():
    with pytest.raises(UnexpectedGranuleError, match="does not match"):
        expand_geolocation(np.zeros((2, 2)), np.zeros((2, 3)), (4, 4))


def test_geolocation_valid_rejects_the_unlocated_sentinel():
    lat = np.array([[10.0, -999.0]])
    lon = np.array([[20.0, -999.0]])
    assert geolocation_valid(lat, lon).tolist() == [[True, False]]


def test_tile_indices_floor_and_clip():
    lat = np.array([12.7, -0.2, 90.0, -90.0])
    lon = np.array([-3.4, 179.9, 180.0, -180.0])
    tile_lat, tile_lon = tile_indices(lat, lon)
    assert tile_lat.tolist() == [12, -1, 89, -90]
    assert tile_lon.tolist() == [-4, 179, 179, -180]


# --- aggregation --------------------------------------------------------------------


def test_tile_maxima_picks_the_hottest_pixel_and_its_location():
    celsius = np.array([[40.0, 55.0], [50.0, 41.0]])
    valid = np.ones((2, 2), dtype=bool)
    lat = np.array([[10.2, 10.8], [10.4, 11.6]])
    lon = np.array([[20.2, 20.8], [20.4, 20.6]])

    tiles = tile_maxima(celsius, valid, lat, lon, "2026-08-30T10:00:00Z", "G1")

    assert set(tiles) == {(10, 20), (11, 20)}
    hot = tiles[(10, 20)]
    assert hot.max_c == 55.0
    assert hot.max_lat == 10.8
    assert hot.max_lon == 20.8
    assert hot.granule_id == "G1"
    assert hot.observed_at == "2026-08-30T10:00:00Z"
    assert tiles[(11, 20)].max_c == 41.0


def test_tile_maxima_ignores_invalid_pixels():
    celsius = np.array([[99.0, 42.0]])
    valid = np.array([[False, True]])
    lat = np.array([[10.5, 10.5]])
    lon = np.array([[20.5, 20.5]])
    tiles = tile_maxima(celsius, valid, lat, lon, "t", "G1")
    assert tiles[(10, 20)].max_c == 42.0


def test_tile_maxima_returns_empty_when_nothing_is_valid():
    celsius = np.zeros((2, 2))
    valid = np.zeros((2, 2), dtype=bool)
    lat = np.full((2, 2), 10.5)
    lon = np.full((2, 2), 20.5)
    assert tile_maxima(celsius, valid, lat, lon, "t", "G1") == {}


def make_tile(tile_lat: int, tile_lon: int, max_c: float, granule_id: str = "G") -> TileMax:
    return TileMax(
        tile_lat=tile_lat,
        tile_lon=tile_lon,
        max_c=max_c,
        max_lat=tile_lat + 0.5,
        max_lon=tile_lon + 0.5,
        observed_at="2026-08-30T10:00:00Z",
        granule_id=granule_id,
    )


def test_merge_keeps_the_hotter_reading_per_tile():
    acc = {(10, 20): make_tile(10, 20, 45.0, "A")}
    merge_tile_maxima(acc, {(10, 20): make_tile(10, 20, 52.0, "B")})
    merge_tile_maxima(acc, {(10, 20): make_tile(10, 20, 48.0, "C")})
    merge_tile_maxima(acc, {(11, 21): make_tile(11, 21, 30.0, "D")})

    assert acc[(10, 20)].max_c == 52.0
    assert acc[(10, 20)].granule_id == "B"
    assert acc[(11, 21)].max_c == 30.0


# --- selection ----------------------------------------------------------------------


def test_selection_keeps_every_tile_over_the_threshold():
    tiles = [make_tile(i, 0, HOT_TILE_THRESHOLD_C + i) for i in range(15)]
    tiles += [make_tile(i, 1, 10.0) for i in range(30)]

    selected = select_reported_tiles(tiles, threshold_c=HOT_TILE_THRESHOLD_C, top_n=10)

    assert len(selected) == 15
    assert all(t.max_c >= HOT_TILE_THRESHOLD_C for t in selected)
    assert selected[0].max_c == pytest.approx(HOT_TILE_THRESHOLD_C + 14)


def test_selection_falls_back_to_the_global_top_n_on_a_cool_day():
    tiles = [make_tile(i, 0, 5.0 + i) for i in range(30)]

    selected = select_reported_tiles(tiles, threshold_c=HOT_TILE_THRESHOLD_C, top_n=10)

    assert len(selected) == 10
    assert [t.max_c for t in selected] == [34.0, 33.0, 32.0, 31.0, 30.0,
                                           29.0, 28.0, 27.0, 26.0, 25.0]


def test_selection_unions_threshold_and_top_n():
    # Three tiles clear 40 C; the top-10 rule pulls in seven cooler ones as well.
    tiles = [make_tile(i, 0, 41.0 + i) for i in range(3)]
    tiles += [make_tile(i, 1, 10.0 + i) for i in range(20)]

    selected = select_reported_tiles(tiles, threshold_c=HOT_TILE_THRESHOLD_C, top_n=10)

    assert len(selected) == 10
    assert sum(1 for t in selected if t.max_c >= HOT_TILE_THRESHOLD_C) == 3


def test_selection_handles_fewer_tiles_than_top_n():
    tiles = [make_tile(0, 0, 12.0), make_tile(1, 1, 8.0)]
    assert len(select_reported_tiles(tiles, top_n=10)) == 2


def test_selection_returns_hottest_first():
    tiles = [make_tile(0, 0, 30.0), make_tile(1, 1, 50.0), make_tile(2, 2, 41.0)]
    assert [t.max_c for t in select_reported_tiles(tiles)] == [50.0, 41.0, 30.0]


# --- whole-granule path -------------------------------------------------------------


def build_synthetic_granule():
    """A 20x20 granule with three deliberately planted pixels.

    Geolocation is a 4x4 subsampled grid spanning latitudes 10-13 and
    longitudes 20-23, so each 5x5 block of LST pixels lands in its own
    1-degree tile.
    """
    raw = np.full((20, 20), kelvin_counts(30.0), dtype=np.uint16)
    qc = np.full((20, 20), QC_GOOD, dtype=np.uint8)

    # Hottest good pixel in the granule, in the block that maps to tile (10, 20).
    raw[0, 0] = kelvin_counts(58.25)
    # Hotter still, but the QC byte says it was never really produced.
    raw[1, 1] = kelvin_counts(90.0)
    qc[1, 1] = QC_CLOUD
    # Hotter still, produced but with an error class above our tolerance.
    raw[2, 2] = kelvin_counts(80.0)
    qc[2, 2] = QC_BIG_ERROR
    # Fill pixel: would decode to -273.15 C if it were not masked.
    raw[3, 3] = 0
    # A separate warm peak in the block that maps to tile (12, 22).
    raw[12, 12] = kelvin_counts(47.5)

    lat_sub = np.array([[10.1, 10.1, 10.1, 10.1],
                        [11.1, 11.1, 11.1, 11.1],
                        [12.1, 12.1, 12.1, 12.1],
                        [13.1, 13.1, 13.1, 13.1]])
    lon_sub = np.array([[20.1, 21.1, 22.1, 23.1]] * 4)
    return raw, qc, lat_sub, lon_sub


def test_granule_tile_maxima_end_to_end():
    raw, qc, lat_sub, lon_sub = build_synthetic_granule()

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="2026-08-30T11:25:00Z",
        granule_id="MOD11_L2.A2026242.1125.061.hdf",
    )

    hottest = tiles[(10, 20)]
    # 58.25 C wins: the 90 C and 80 C pixels were excluded by QC, and the fill
    # pixel never became -273.15 C.
    assert hottest.max_c == pytest.approx(58.25, abs=0.01)
    assert hottest.max_lat == pytest.approx(10.1)
    assert hottest.max_lon == pytest.approx(20.1)
    assert hottest.granule_id == "MOD11_L2.A2026242.1125.061.hdf"
    assert "2K" in hottest.qc_note

    assert tiles[(12, 22)].max_c == pytest.approx(47.5, abs=0.01)
    # Four 5x5 blocks across a 4x4 geolocation grid: 16 tiles, all populated.
    assert len(tiles) == 16
    assert min(t.max_c for t in tiles.values()) == pytest.approx(30.0, abs=0.01)


def test_granule_tile_maxima_rejects_shape_mismatch():
    raw, qc, lat_sub, lon_sub = build_synthetic_granule()
    with pytest.raises(UnexpectedGranuleError, match="does not match QC"):
        granule_tile_maxima(
            raw_lst=raw,
            lst_attrs=GOOD_ATTRS,
            qc=qc[:10],
            lat_sub=lat_sub,
            lon_sub=lon_sub,
            observed_at="t",
            granule_id="G",
        )


def test_granule_tile_maxima_with_no_usable_pixels_returns_empty():
    raw = np.zeros((10, 10), dtype=np.uint16)  # all fill
    qc = np.full((10, 10), QC_GOOD, dtype=np.uint8)
    lat_sub = np.full((2, 2), 10.5)
    lon_sub = np.full((2, 2), 20.5)

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="t",
        granule_id="G",
    )
    assert tiles == {}
