"""Latitude plausibility screen: the backstop for fires MOD14 did not detect.

The keep/reject cases here are the real ones the band has to get right. Getting
it wrong in one direction leaves a Siberian flame front on the map as a record;
getting it wrong in the other deletes the Lut Desert.
"""

from __future__ import annotations

import numpy as np
import pytest

from kiln_ingest.science import (
    HIGH_LATITUDE_DEGREES,
    HIGH_LATITUDE_MAX_C,
    HIGH_LATITUDE_OUTLIER_NOTE,
    QC_NOTE,
    granule_tile_maxima,
    plausibility_keep_mask,
    prepare_granule,
)

GOOD_ATTRS = {"scale_factor": 0.02, "add_offset": 0.0, "_FillValue": 0}
KELVIN_ZERO_C = 273.15


def counts(celsius: float) -> int:
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


def keeps(celsius: float, lat: float) -> bool:
    return bool(plausibility_keep_mask(np.array([celsius]), np.array([lat]))[0])


# --- the four cases that define the band --------------------------------------------


def test_a_siberian_seventy_eight_degrees_is_rejected():
    # The reading that motivated the screen: 78.75 C at 64.96 N, a fire below
    # MOD14's detection threshold.
    assert not keeps(78.75, 64.96)


def test_a_warm_arctic_summer_day_is_kept():
    assert keeps(45.0, 65.0)


def test_turpan_is_kept():
    # The Turpan Depression at 42.9 N legitimately exceeds 65 C.
    assert keeps(70.0, 42.9)


def test_the_verified_global_record_is_kept():
    # 80.8 C at about 31 N, Lut Desert (Zhao et al. 2021).
    assert keeps(80.8, 31.0)


# --- the band's edges ---------------------------------------------------------------


def test_the_screen_is_symmetric_about_the_equator():
    assert not keeps(78.0, -64.96)
    assert keeps(45.0, -64.96)


def test_both_conditions_are_required():
    # Hot but equatorward, and poleward but cool: each alone survives.
    assert keeps(HIGH_LATITUDE_MAX_C + 20.0, HIGH_LATITUDE_DEGREES - 0.1)
    assert keeps(HIGH_LATITUDE_MAX_C - 0.1, HIGH_LATITUDE_DEGREES + 20.0)


def test_the_boundaries_themselves_are_kept():
    # Strict inequalities on both axes: the band excludes only what is past
    # both edges, never what sits on one.
    assert keeps(HIGH_LATITUDE_MAX_C, HIGH_LATITUDE_DEGREES + 10.0)
    assert keeps(HIGH_LATITUDE_MAX_C + 10.0, HIGH_LATITUDE_DEGREES)
    assert not keeps(HIGH_LATITUDE_MAX_C + 0.1, HIGH_LATITUDE_DEGREES + 0.1)


def test_the_mask_keeps_the_shape_of_its_input():
    celsius = np.array([[78.0, 45.0], [70.0, 78.0]])
    lat = np.array([[65.0, 65.0], [42.9, 10.0]])
    assert plausibility_keep_mask(celsius, lat).tolist() == [
        [False, True],
        [True, True],
    ]


# --- the whole-granule path ---------------------------------------------------------


def siberian_granule():
    """A 2x2 granule in Siberia, one pixel of which reads like an undetected fire.

    Three pixels land in tile (64, 100) and the fourth in (64, 101), so the tile
    that loses a pixel still has a maximum to report and the neighbouring tile
    is there to show it was left alone.
    """
    raw = np.array(
        [[counts(78.75), counts(43.0)], [counts(41.0), counts(42.0)]], dtype=np.uint16
    )
    qc = np.zeros((2, 2), dtype=np.uint8)
    lat_sub = np.array([[64.2, 64.2], [64.6, 64.6]])
    lon_sub = np.array([[100.2, 100.6], [100.2, 101.5]])
    return raw, qc, lat_sub, lon_sub


def test_an_implausible_pixel_is_dropped_and_its_tile_named():
    raw, qc, lat_sub, lon_sub = siberian_granule()

    field = prepare_granule(
        raw_lst=raw, lst_attrs=GOOD_ATTRS, qc=qc, lat_sub=lat_sub, lon_sub=lon_sub
    )

    assert field.keep.tolist() == [[False, True], [True, True]]
    assert field.outlier_tiles == frozenset({(64, 100)})
    assert field.fire_tiles == frozenset()


def test_an_implausible_pixel_never_becomes_the_tile_maximum():
    raw, qc, lat_sub, lon_sub = siberian_granule()

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="2026-08-30T11:25:00Z",
        granule_id="MOD11_L2.A2026242.1125.061.NRT.hdf",
    )

    # 78.75 C is gone; the tile reports the hottest pixel that is left.
    hottest = tiles[(64, 100)]
    assert hottest.max_c == pytest.approx(43.0, abs=0.02)
    assert hottest.qc_note.endswith(HIGH_LATITUDE_OUTLIER_NOTE)

    # The neighbouring tile lost nothing and says nothing.
    assert tiles[(64, 101)].qc_note == QC_NOTE


def test_a_tile_whose_only_pixel_was_implausible_is_not_reported_at_all():
    # No surviving pixel means no maximum to publish. An absent tile is the
    # right answer here: there is nothing left to say about it, and inventing a
    # cooler neighbour's number would be worse than silence.
    raw = np.array([[counts(78.75)]], dtype=np.uint16)
    qc = np.zeros((1, 1), dtype=np.uint8)

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=np.array([[64.96]]),
        lon_sub=np.array([[100.5]]),
        observed_at="t",
        granule_id="G",
    )

    assert tiles == {}


def test_a_granule_with_nothing_implausible_is_untouched():
    raw = np.full((2, 2), counts(48.0), dtype=np.uint16)
    qc = np.zeros((2, 2), dtype=np.uint8)
    lat_sub = np.full((2, 2), 30.5)
    lon_sub = np.array([[40.5, 41.5], [40.5, 41.5]])

    tiles = granule_tile_maxima(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        observed_at="t",
        granule_id="G",
    )

    assert all(tile.qc_note == QC_NOTE for tile in tiles.values())


def test_an_implausible_pixel_never_reaches_the_raster_either():
    from kiln_ingest import raster

    raw, qc, lat_sub, lon_sub = siberian_granule()
    field = prepare_granule(
        raw_lst=raw, lst_attrs=GOOD_ATTRS, qc=qc, lat_sub=lat_sub, lon_sub=lon_sub
    )

    store: dict[tuple[int, int], np.ndarray] = {}
    raster.accumulate_granule(
        store, field.celsius, field.lat, field.lon, valid=field.keep
    )

    # Three pixels clear the 40 C display threshold, and the 78.75 C one is not
    # among them: the screen removed it from both outputs, not just the rows.
    # Values are centi-Celsius, a couple of hundredths off from the 0.02 K
    # quantization the granule stores.
    painted = [int(tile.max()) for tile in store.values()]
    assert painted
    assert max(painted) == pytest.approx(4300, abs=2)
    from kiln_ingest.science import FIRE_MASKED_NOTE, fire_exclusion_keys

    raw, qc, lat_sub, lon_sub = siberian_granule()
    # A fire MOD14 did detect, on the 41 C pixel at 64.6 N, 100.2 E. The tile
    # now loses one pixel to each screen and still keeps its 43 C maximum.
    keys = fire_exclusion_keys(np.array([64.6]), np.array([100.2]))

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

    hottest = tiles[(64, 100)]
    assert hottest.max_c == pytest.approx(43.0, abs=0.02)
    assert FIRE_MASKED_NOTE in hottest.qc_note
    assert HIGH_LATITUDE_OUTLIER_NOTE in hottest.qc_note
    # Fire is named first: it is the specific cause, and what reaches the
    # plausibility screen is only what MOD14 did not account for.
    assert hottest.qc_note.index(FIRE_MASKED_NOTE) < hottest.qc_note.index(
        HIGH_LATITUDE_OUTLIER_NOTE
    )
