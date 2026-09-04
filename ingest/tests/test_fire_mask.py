"""Active-fire masking tests. Synthetic fires against a synthetic LST grid."""

from __future__ import annotations

import numpy as np
import pytest

from kiln_ingest.science import (
    FIRE_BIN_DEGREES,
    FIRE_MASKED_NOTE,
    FIRE_UNAVAILABLE_NOTE,
    QC_NOTE,
    fire_bin_keys,
    fire_excluded_mask,
    fire_exclusion_keys,
    granule_tile_maxima,
    prepare_granule,
)

GOOD_ATTRS = {"scale_factor": 0.02, "add_offset": 0.0, "_FillValue": 0}
KELVIN_ZERO_C = 273.15


def counts(celsius: float) -> int:
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


# --- binning ------------------------------------------------------------------------


def test_bin_keys_are_unique_per_bin():
    lat = np.array([10.005, 10.005, 10.025])
    lon = np.array([20.005, 20.025, 20.005])
    keys = fire_bin_keys(lat, lon)
    assert len(set(keys.tolist())) == 3


def test_bin_keys_group_coordinates_inside_one_bin():
    # Both ends of the same 0.02-degree cell.
    keys = fire_bin_keys(np.array([10.001, 10.019]), np.array([20.001, 20.019]))
    assert keys[0] == keys[1]


def test_bin_keys_floor_rather_than_truncate_below_the_equator():
    # trunc(-0.5) and floor(-0.5) differ; getting this wrong folds the southern
    # hemisphere's first bin onto the northern one.
    south = fire_bin_keys(np.array([-0.01]), np.array([-0.01]))
    north = fire_bin_keys(np.array([0.01]), np.array([0.01]))
    assert south[0] != north[0]


def test_no_detections_yields_no_exclusion():
    keys = fire_exclusion_keys(np.empty(0), np.empty(0))
    assert keys.size == 0
    lat = np.array([[10.0]])
    assert fire_excluded_mask(lat, lat, keys).tolist() == [[False]]


def test_off_globe_detections_are_ignored():
    # MODIS uses -999.0 for unlocated fire pixels.
    assert fire_exclusion_keys(np.array([-999.0]), np.array([-999.0])).size == 0


def test_mismatched_fire_vectors_are_rejected():
    with pytest.raises(Exception, match="does not match"):
        fire_exclusion_keys(np.array([1.0, 2.0]), np.array([1.0]))


# --- the guard ring -----------------------------------------------------------------


def test_guard_ring_excludes_the_fire_bin_and_its_eight_neighbours():
    fire_lat = np.array([10.005])
    fire_lon = np.array([20.005])
    keys = fire_exclusion_keys(fire_lat, fire_lon)

    # Nine bins, once each.
    assert keys.size == 9

    step = FIRE_BIN_DEGREES
    lat = np.array([
        10.005,           # the fire's own bin
        10.005 + step,    # one bin north
        10.005 - step,    # one bin south
        10.005 - step,    # diagonal
        10.005 + 2 * step,  # two bins north: outside the ring
        10.005,           # two bins east: outside the ring
        -33.0,            # the other side of the world
    ])
    lon = np.array([
        20.005,
        20.005,
        20.005,
        20.005 - step,
        20.005,
        20.005 + 2 * step,
        150.0,
    ])

    excluded = fire_excluded_mask(lat, lon, keys)
    assert excluded.tolist() == [True, True, True, True, False, False, False]


def test_guard_ring_works_south_and_west_of_zero():
    keys = fire_exclusion_keys(np.array([-10.005]), np.array([-20.005]))
    lat = np.array([-10.005, -10.005 - FIRE_BIN_DEGREES, -10.005 - 3 * FIRE_BIN_DEGREES])
    lon = np.array([-20.005, -20.005 - FIRE_BIN_DEGREES, -20.005])
    assert fire_excluded_mask(lat, lon, keys).tolist() == [True, True, False]


def test_several_fires_merge_into_one_key_set():
    keys = fire_exclusion_keys(np.array([10.005, 40.005]), np.array([20.005, 50.005]))
    assert keys.size == 18
    excluded = fire_excluded_mask(
        np.array([10.005, 40.005, 25.0]), np.array([20.005, 50.005, 35.0]), keys
    )
    assert excluded.tolist() == [True, True, False]


def test_adjacent_fires_do_not_double_count_shared_bins():
    # Two fires one bin apart share six of their nine bins.
    keys = fire_exclusion_keys(
        np.array([10.005, 10.005 + FIRE_BIN_DEGREES]), np.array([20.005, 20.005])
    )
    assert keys.size == 12


# --- the whole-granule path ---------------------------------------------------------


def burning_granule():
    """A 4x4 granule spanning one 1-degree tile, with a hot pixel at each corner.

    Geolocation runs 10.00-10.06 by 20.00-20.06, so every pixel lands in tile
    (10, 20) but in a different 0.02-degree fire bin.
    """
    raw = np.full((4, 4), counts(45.0), dtype=np.uint16)
    raw[0, 0] = counts(80.0)  # the pixel a fire will be planted on
    raw[3, 3] = counts(62.0)  # the hottest unburnt pixel
    qc = np.zeros((4, 4), dtype=np.uint8)

    lat_sub = np.array([[10.005 + 0.02 * row] * 4 for row in range(4)])
    lon_sub = np.array([[20.005 + 0.02 * col for col in range(4)]] * 4)
    return raw, qc, lat_sub, lon_sub


def test_prepare_granule_drops_burning_pixels_and_names_their_tile():
    raw, qc, lat_sub, lon_sub = burning_granule()
    keys = fire_exclusion_keys(np.array([10.005]), np.array([20.005]))

    field = prepare_granule(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        fire_exclusion=keys,
    )

    # The fire's own bin plus its neighbours: a 2x2 block of the corner.
    assert field.keep[0, 0] is np.False_
    assert field.keep[1, 1] is np.False_
    assert field.keep[3, 3] is np.True_
    assert field.fire_tiles == frozenset({(10, 20)})


def test_fire_masked_pixels_never_become_the_tile_maximum():
    raw, qc, lat_sub, lon_sub = burning_granule()
    keys = fire_exclusion_keys(np.array([10.005]), np.array([20.005]))

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="2026-08-30T11:25:00Z",
        granule_id="MOD11_L2.A2026242.1125.061.NRT.hdf",
        fire_exclusion=keys,
    )

    hottest = tiles[(10, 20)]
    assert hottest.max_c == pytest.approx(62.0, abs=0.02)
    assert hottest.qc_note.endswith(FIRE_MASKED_NOTE)


def test_a_clean_granule_keeps_the_plain_note():
    raw, qc, lat_sub, lon_sub = burning_granule()
    # The mask was applied and found nothing: an empty key array, not None.
    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="t",
        granule_id="G",
        fire_exclusion=np.empty(0, dtype=np.int64),
    )
    assert tiles[(10, 20)].max_c == pytest.approx(80.0, abs=0.02)
    assert tiles[(10, 20)].qc_note == QC_NOTE


def test_only_the_tiles_that_lost_a_pixel_are_marked():
    raw, qc, lat_sub, lon_sub = burning_granule()
    # Push the right-hand column into tile (10, 21) and burn only the left one.
    lon_sub = lon_sub + np.array([[0.0, 0.0, 0.0, 1.0]])
    keys = fire_exclusion_keys(np.array([10.005]), np.array([20.005]))

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="t",
        granule_id="G",
        fire_exclusion=keys,
    )

    assert tiles[(10, 20)].qc_note.endswith(FIRE_MASKED_NOTE)
    assert tiles[(10, 21)].qc_note == QC_NOTE


def test_an_unavailable_mask_is_recorded_on_every_tile():
    raw, qc, lat_sub, lon_sub = burning_granule()

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="t",
        granule_id="G",
        fire_exclusion=None,
        qc_note=QC_NOTE + FIRE_UNAVAILABLE_NOTE,
    )

    # No mask means no pixel was dropped: the 80 C reading still wins, and the
    # note says it was never checked rather than implying it was.
    assert tiles[(10, 20)].max_c == pytest.approx(80.0, abs=0.02)
    assert tiles[(10, 20)].qc_note.endswith(FIRE_UNAVAILABLE_NOTE)
    assert FIRE_MASKED_NOTE not in tiles[(10, 20)].qc_note
