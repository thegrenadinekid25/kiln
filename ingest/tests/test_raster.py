"""Raster projection, accumulation and pyramid tests. Pure numpy, no images."""

from __future__ import annotations

import numpy as np
import pytest

from kiln_ingest.raster import (
    EMPTY_CENTI_C,
    MAX_ZOOM,
    MERCATOR_MAX_LAT,
    TILE_SIZE,
    accumulate_granule,
    blank_tile,
    build_pyramid,
    downsample_children,
    global_pixel_extent,
    project_to_pixels,
    tile_has_data,
    to_centi_celsius,
)

# 256 px tiles across 2^7 tiles.
Z7_EXTENT = 256 * 128


# --- projection ---------------------------------------------------------------------


def test_extent_is_the_whole_world_in_pixels():
    assert global_pixel_extent(0) == 256
    assert global_pixel_extent(MAX_ZOOM) == Z7_EXTENT


def test_null_island_lands_at_the_centre_of_the_map():
    px, py = project_to_pixels(np.array([0.0]), np.array([0.0]))
    assert (int(px[0]), int(py[0])) == (Z7_EXTENT // 2, Z7_EXTENT // 2)
    # Which is the top-left corner of tile (64, 64).
    assert (px[0] // TILE_SIZE, py[0] // TILE_SIZE) == (64, 64)
    assert (px[0] % TILE_SIZE, py[0] % TILE_SIZE) == (0, 0)


def test_the_map_corners_are_the_antimeridian_and_the_mercator_limit():
    px, py = project_to_pixels(
        np.array([MERCATOR_MAX_LAT, -MERCATOR_MAX_LAT]), np.array([-180.0, 180.0])
    )
    assert (int(px[0]), int(py[0])) == (0, 0)
    assert (int(px[1]), int(py[1])) == (Z7_EXTENT - 1, Z7_EXTENT - 1)


def test_latitudes_beyond_the_mercator_limit_are_clamped_not_dropped():
    clamped, _ = project_to_pixels(np.array([90.0, -90.0]), np.array([0.0, 0.0]))
    _, rows = project_to_pixels(np.array([90.0, -90.0]), np.array([0.0, 0.0]))
    assert int(rows[0]) == 0
    assert int(rows[1]) == Z7_EXTENT - 1
    assert clamped.tolist() == [Z7_EXTENT // 2, Z7_EXTENT // 2]


def test_zoom_halves_the_pixel_grid():
    fine_x, fine_y = project_to_pixels(np.array([37.5]), np.array([-119.0]), zoom=7)
    coarse_x, coarse_y = project_to_pixels(np.array([37.5]), np.array([-119.0]), zoom=6)
    assert int(coarse_x[0]) == int(fine_x[0]) // 2
    assert int(coarse_y[0]) == int(fine_y[0]) // 2


def test_centi_celsius_round_trips_within_a_hundredth():
    assert to_centi_celsius(np.array([40.0, 57.995, 200.0])).tolist() == [4000, 5800, 20000]


def test_centi_celsius_never_collides_with_the_empty_sentinel():
    assert to_centi_celsius(np.array([-1000.0]))[0] == EMPTY_CENTI_C + 1


# --- accumulation -------------------------------------------------------------------


def test_a_hot_pixel_lands_in_the_tile_the_projection_names():
    store: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(store, np.array([61.5]), np.array([0.0]), np.array([0.0]))

    assert list(store) == [(64, 64)]
    tile = store[(64, 64)]
    assert tile[0, 0] == 6150
    # Everything else stayed unobserved.
    assert int((tile != EMPTY_CENTI_C).sum()) == 1


def test_pixels_below_the_display_threshold_are_not_rasterised():
    store: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(store, np.array([39.9]), np.array([0.0]), np.array([0.0]))
    assert store == {}


def test_the_hottest_reading_wins_within_and_across_granules():
    store: dict[tuple[int, int], np.ndarray] = {}
    # Two 1 km pixels of one granule landing on the same tile pixel, hottest
    # second, so the within-granule collapse has to pick the max rather than
    # the last one written.
    accumulate_granule(
        store, np.array([55.0, 61.0]), np.array([0.0, 0.0]), np.array([0.0, 0.0])
    )
    assert store[(64, 64)][0, 0] == 6100

    # And hottest first, which the opposite bug would pass.
    other: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(
        other, np.array([61.0, 55.0]), np.array([0.0, 0.0]), np.array([0.0, 0.0])
    )
    assert other[(64, 64)][0, 0] == 6100

    # A cooler later granule, e.g. the second satellite's pass, cannot undo it.
    accumulate_granule(store, np.array([45.0]), np.array([0.0]), np.array([0.0]))
    assert store[(64, 64)][0, 0] == 6100

    accumulate_granule(store, np.array([70.0]), np.array([0.0]), np.array([0.0]))
    assert store[(64, 64)][0, 0] == 7000


def test_masked_and_unlocated_pixels_never_reach_the_raster():
    store: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(
        store,
        np.array([80.0, 80.0, 80.0]),
        np.array([0.0, 10.0, -999.0]),
        np.array([0.0, 10.0, -999.0]),
        valid=np.array([False, True, True]),
    )
    # Only the second pixel survives: the first was masked, the third unlocated.
    assert len(store) == 1
    assert (64, 64) not in store


def test_accumulating_nothing_leaves_the_store_alone():
    store: dict[tuple[int, int], np.ndarray] = {}
    accumulate_granule(store, np.array([]), np.array([]), np.array([]))
    assert store == {}


def test_a_two_dimensional_granule_field_is_accepted_whole():
    store: dict[tuple[int, int], np.ndarray] = {}
    celsius = np.array([[41.0, 10.0], [10.0, 10.0]])
    lat = np.array([[0.0, 0.0], [0.0, 0.0]])
    lon = np.array([[0.0, 0.02], [0.04, 0.06]])
    keep = np.ones((2, 2), dtype=bool)

    accumulate_granule(store, celsius, lat, lon, valid=keep)

    assert store[(64, 64)][0, 0] == 4100


# --- pyramid ------------------------------------------------------------------------


def test_a_parent_tile_is_the_max_of_each_two_by_two_block():
    child = blank_tile()
    child[0, 0] = 5000
    child[0, 1] = 6000
    child[1, 0] = 4500
    # child[1, 1] stays empty; the block still resolves to its hottest member.
    child[2, 2] = 4200
    child[3, 3] = 7700

    parent = downsample_children({(64, 64): child}, 32, 32)

    assert parent is not None
    assert parent[0, 0] == 6000
    assert parent[1, 1] == 7700
    assert parent[0, 1] == EMPTY_CENTI_C


def test_each_child_lands_in_its_own_quadrant_of_the_parent():
    half = TILE_SIZE // 2
    children = {}
    for offset_x, offset_y, value in ((0, 0, 4100), (1, 0, 5100), (0, 1, 6100), (1, 1, 7100)):
        tile = blank_tile()
        tile[0, 0] = value
        children[(64 + offset_x, 64 + offset_y)] = tile

    parent = downsample_children(children, 32, 32)

    assert parent is not None
    assert parent[0, 0] == 4100
    assert parent[0, half] == 5100
    assert parent[half, 0] == 6100
    assert parent[half, half] == 7100


def test_a_parent_with_no_children_is_not_a_tile():
    assert downsample_children({}, 32, 32) is None


def test_the_pyramid_runs_from_the_base_zoom_down_to_the_world_view():
    base = {(64, 64): blank_tile()}
    base[(64, 64)][0, 0] = 6600

    pyramid = build_pyramid(base)

    assert sorted(pyramid) == list(range(0, MAX_ZOOM + 1))
    assert list(pyramid[MAX_ZOOM]) == [(64, 64)]
    assert list(pyramid[6]) == [(32, 32)]
    assert list(pyramid[0]) == [(0, 0)]
    # The hotspot survives all the way out rather than being averaged away, and
    # lands where it belongs: the top-left corner of z7 tile (64, 64) is null
    # island, which is the centre of the single world tile.
    assert pyramid[0][(0, 0)][TILE_SIZE // 2, TILE_SIZE // 2] == 6600
    assert int((pyramid[0][(0, 0)] != EMPTY_CENTI_C).sum()) == 1


def test_the_pyramid_only_builds_parents_that_have_children():
    base = {(0, 0): blank_tile(), (127, 127): blank_tile()}
    base[(0, 0)][0, 0] = 4100
    base[(127, 127)][255, 255] = 4200

    pyramid = build_pyramid(base)

    assert sorted(pyramid[6]) == [(0, 0), (63, 63)]
    assert sorted(pyramid[1]) == [(0, 0), (1, 1)]
    assert sorted(pyramid[0]) == [(0, 0)]


def test_build_pyramid_rejects_an_inverted_zoom_range():
    with pytest.raises(ValueError, match="min_zoom"):
        build_pyramid({}, max_zoom=3, min_zoom=5)


def test_tile_has_data_distinguishes_an_empty_tile():
    assert not tile_has_data(blank_tile())
    tile = blank_tile()
    tile[10, 10] = 4000
    assert tile_has_data(tile)
