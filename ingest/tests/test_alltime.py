"""All-time accumulation. Pure functions, no network.

The property under test throughout is that the archive only ever rises, and
only from readings the screens already cleared. A merge is a maximum and a
maximum is permanent: a wrong value admitted once cannot be corrected by any
later day.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kiln_ingest.alltime import (
    CorruptStateError,
    alltime_since,
    alltime_through,
    alltime_tile_total,
    build_alltime_levels,
    dump_state,
    level_has_data,
    load_state,
    merge_day,
    merge_parent_ranks,
    merge_state,
    parent_keys,
    pool_ranks,
    select_alltime_upserts,
)
from kiln_ingest.raster import EMPTY_CENTI_C, MAX_ZOOM, TILE_SIZE, blank_tile
from kiln_ingest.science import TileMax
from kiln_ingest.tile_png import palette_indices

TARGET = date(2026, 8, 31)


def tile_with(**pixels: int) -> np.ndarray:
    """A blank state tile with named "row_col" pixels set."""
    tile = blank_tile()
    for name, value in pixels.items():
        row, col = (int(part) for part in name.split("_"))
        tile[row, col] = value
    return tile


def reading(tile_lat: int, tile_lon: int, max_c: float, granule="MOD11_L2.A1.hdf") -> TileMax:
    return TileMax(
        tile_lat=tile_lat,
        tile_lon=tile_lon,
        max_c=max_c,
        max_lat=tile_lat + 0.5,
        max_lon=tile_lon + 0.5,
        observed_at="2026-08-31T11:25:00Z",
        granule_id=granule,
    )


# --- state serialisation ------------------------------------------------------------


def test_state_round_trips_through_bytes():
    state = tile_with(**{"0_0": 8000, "255_255": 4000})
    restored = load_state(dump_state(state))
    assert restored.dtype == np.int16
    assert np.array_equal(restored, state)


def test_a_truncated_state_object_is_rejected():
    with pytest.raises(CorruptStateError):
        load_state(dump_state(blank_tile())[:40])


def test_a_state_object_of_the_wrong_shape_is_rejected():
    import io

    buffer = io.BytesIO()
    np.save(buffer, np.zeros((8, 8), dtype=np.int16), allow_pickle=False)
    with pytest.raises(CorruptStateError, match="shape"):
        load_state(buffer.getvalue())


def test_a_state_object_of_the_wrong_dtype_is_rejected():
    import io

    buffer = io.BytesIO()
    np.save(buffer, np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32), allow_pickle=False)
    with pytest.raises(CorruptStateError, match="dtype"):
        load_state(buffer.getvalue())


def test_a_pickled_payload_is_refused_rather_than_executed():
    import io
    import pickle

    buffer = io.BytesIO()
    np.save(buffer, np.array([{"not": "an array"}], dtype=object), allow_pickle=True)
    with pytest.raises(CorruptStateError):
        load_state(buffer.getvalue())
    assert pickle  # the point is that loading never reaches the unpickler


# --- merging ------------------------------------------------------------------------


def test_a_first_sighting_becomes_the_state():
    today = tile_with(**{"0_0": 5000})
    merged = merge_state(today, None)
    assert np.array_equal(merged, today)
    # A copy, so later writes to the day's store cannot reach the archive.
    merged[0, 0] = 1
    assert today[0, 0] == 5000


def test_the_merge_takes_the_hotter_of_the_two_per_pixel():
    stored = tile_with(**{"0_0": 6000, "1_1": 4500})
    today = tile_with(**{"0_0": 5000, "2_2": 7000})

    merged = merge_state(today, stored)

    assert merged[0, 0] == 6000  # the archive was hotter
    assert merged[1, 1] == 4500  # today did not see it at all
    assert merged[2, 2] == 7000  # today set a new record
    assert merged[3, 3] == EMPTY_CENTI_C


def test_an_unobserved_pixel_never_beats_a_real_reading():
    stored = tile_with(**{"0_0": 4200})
    today = blank_tile()
    assert merge_state(today, stored)[0, 0] == 4200


def test_only_tiles_that_actually_improved_are_returned():
    stored_hotter = tile_with(**{"0_0": 7000})
    stored_cooler = tile_with(**{"0_0": 4000})

    changed = merge_day(
        today={
            (1, 1): tile_with(**{"0_0": 5000}),  # cooler than the archive
            (2, 2): tile_with(**{"0_0": 5000}),  # hotter than the archive
            (3, 3): tile_with(**{"0_0": 5000}),  # never seen before
        },
        stored={(1, 1): stored_hotter, (2, 2): stored_cooler, (3, 3): None},
    )

    assert sorted(changed) == [(2, 2), (3, 3)]
    assert changed[(2, 2)][0, 0] == 5000
    assert changed[(3, 3)][0, 0] == 5000


def test_a_day_that_beats_nothing_changes_nothing():
    stored = {(1, 1): tile_with(**{"0_0": 9000})}
    assert merge_day({(1, 1): tile_with(**{"0_0": 5000})}, stored) == {}


def test_merging_the_same_day_twice_is_idempotent():
    # Re-running a date after a failure must land on the same archive.
    today = {(1, 1): tile_with(**{"0_0": 5000})}
    first = merge_day(today, {(1, 1): None})
    assert merge_day(today, first) == {}


# --- the pyramid over what changed --------------------------------------------------


def test_pooling_takes_the_hottest_rank_of_each_block():
    child = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    child[0, 0] = 2
    child[0, 1] = 5
    child[1, 0] = 1
    child[2, 2] = 3

    pooled = pool_ranks(child)

    assert pooled.shape == (TILE_SIZE // 2, TILE_SIZE // 2)
    assert pooled[0, 0] == 5
    assert pooled[1, 1] == 3
    assert pooled[0, 1] == 0


def test_parents_are_the_shared_ancestors_of_their_children():
    assert parent_keys([(64, 64), (65, 64), (2, 2)]) == [(1, 1), (32, 32)]


def test_an_unchanged_quadrant_of_a_parent_is_left_exactly_as_published():
    half = TILE_SIZE // 2
    existing = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    existing[half, half] = 4  # a record in the quadrant of child (65, 65)
    existing[0, 0] = 1  # and a cooler one where the changed child lands

    child = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    child[0, 0] = 3

    merged = merge_parent_ranks({(64, 64): child}, (32, 32), existing)

    assert merged[0, 0] == 3  # raised by today
    assert merged[half, half] == 4  # untouched quadrant preserved


def test_a_parent_never_loses_ground_to_a_cooler_child():
    existing = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    existing[0, 0] = 5

    child = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    child[0, 0] = 2

    assert merge_parent_ranks({(64, 64): child}, (32, 32), existing)[0, 0] == 5


def test_each_child_lands_in_its_own_quadrant():
    half = TILE_SIZE // 2
    children = {}
    for offset_x, offset_y, rank in ((0, 0, 1), (1, 0, 2), (0, 1, 3), (1, 1, 4)):
        child = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
        child[0, 0] = rank
        children[(64 + offset_x, 64 + offset_y)] = child

    merged = merge_parent_ranks(children, (32, 32), None)

    assert merged[0, 0] == 1
    assert merged[0, half] == 2
    assert merged[half, 0] == 3
    assert merged[half, half] == 4


def test_the_rebuild_walks_every_zoom_and_names_what_it_created():
    changed = {(64, 64): tile_with(**{"0_0": 8000})}
    fetched: list[tuple[int, tuple[int, int]]] = []

    def fetch_level(zoom, keys):
        fetched.extend((zoom, key) for key in keys)
        return {key: None for key in keys}  # nothing published yet

    levels, created = build_alltime_levels(changed, fetch_level)

    assert sorted(levels) == list(range(0, MAX_ZOOM + 1))
    assert list(levels[MAX_ZOOM]) == [(64, 64)]
    assert list(levels[6]) == [(32, 32)]
    assert list(levels[0]) == [(0, 0)]
    # Every ancestor was new, and the base tile is counted by the caller.
    assert created == set(fetched)
    assert len(created) == MAX_ZOOM

    # The hotspot reaches the world tile, at the centre where null island sits.
    assert levels[0][(0, 0)][TILE_SIZE // 2, TILE_SIZE // 2] == 5


def test_the_rebuild_only_touches_the_ancestors_of_what_changed():
    changed = {(0, 0): tile_with(**{"0_0": 5000})}
    asked: dict[int, list] = {}

    def fetch_level(zoom, keys):
        asked[zoom] = list(keys)
        return {key: None for key in keys}

    build_alltime_levels(changed, fetch_level)

    # One tile per level, never the whole globe.
    assert all(len(keys) == 1 for keys in asked.values())
    assert asked[6] == [(0, 0)]


def test_rank_merging_a_parent_equals_exact_max_pooling():
    # The justification for merging coarse zooms as palette ranks rather than
    # temperatures: the palette is monotonic, so the bucket of the maximum is
    # the maximum of the buckets.
    yesterday = tile_with(**{"0_0": 4100, "0_1": 7900, "1_0": 5900})
    today = tile_with(**{"0_0": 8000, "0_1": 4100, "1_1": 6700})

    by_rank = merge_parent_ranks(
        {(64, 64): palette_indices(today)}, (32, 32), palette_indices(yesterday)
    )
    exact = palette_indices(np.maximum(yesterday, today))

    assert by_rank[0, 0] == pool_ranks(exact)[0, 0]


def test_level_has_data_distinguishes_an_empty_rank_array():
    assert not level_has_data(np.zeros((4, 4), dtype=np.uint8))
    assert level_has_data(np.array([[0, 1]], dtype=np.uint8))


# --- the all-time table -------------------------------------------------------------


def test_only_readings_that_beat_the_record_are_upserted():
    existing = {(10, 20): 55.0, (11, 21): 60.0}
    today = {
        (10, 20): reading(10, 20, 58.0),  # beats its record
        (11, 21): reading(11, 21, 45.0),  # does not
        (12, 22): reading(12, 22, 47.0),  # first sighting
    }

    selected = select_alltime_upserts(existing, today)

    assert [(t.tile_lat, t.tile_lon) for t in selected] == [(10, 20), (12, 22)]


def test_ties_do_not_count_as_records():
    # Equal is not better; rewriting the row would move record_date onto a later
    # day that did not actually set the record.
    assert select_alltime_upserts({(10, 20): 55.0}, {(10, 20): reading(10, 20, 55.0)}) == []


def test_a_day_that_breaks_no_record_writes_nothing():
    existing = {(i, 0): 70.0 for i in range(20)}
    today = {(i, 0): reading(i, 0, 50.0) for i in range(20)}
    assert select_alltime_upserts(existing, today) == []


def test_improvements_below_the_selection_bar_are_not_stored():
    # Twenty tiles are already above 40 C, so the top-10 clause is satisfied and
    # a 12 C improvement in a cold place earns no row.
    existing = {(i, 0): 70.0 for i in range(20)}
    today = {(50, 0): reading(50, 0, 12.0)}
    assert select_alltime_upserts(existing, today) == []


def test_the_top_n_clause_carries_a_cold_archive():
    # Nothing anywhere has ever cleared 40 C: the archive still gets rows.
    today = {(i, 0): reading(i, 0, 10.0 + i) for i in range(5)}
    selected = select_alltime_upserts({}, today)
    assert len(selected) == 5
    assert selected[0].max_c == 14.0


def test_upserts_come_back_hottest_first():
    today = {(i, 0): reading(i, 0, 40.0 + i) for i in range(4)}
    assert [t.max_c for t in select_alltime_upserts({}, today)] == [43.0, 42.0, 41.0, 40.0]


# --- manifest bookkeeping -----------------------------------------------------------


def test_the_start_of_the_record_is_carried_forward():
    assert alltime_since({"since": "2026-08-30"}, TARGET) == "2026-08-30"


def test_a_first_run_starts_the_record_today():
    assert alltime_since(None, TARGET) == TARGET.isoformat()


def test_backfilling_an_earlier_day_moves_the_start_back():
    assert alltime_since({"since": "2026-08-30"}, date(2026, 8, 25)) == "2026-08-25"


@pytest.mark.parametrize("prior", [{}, {"since": None}, {"since": "not-a-date"}, {"since": 7}])
def test_an_unusable_prior_since_falls_back_to_today(prior):
    assert alltime_since(prior, TARGET) == TARGET.isoformat()


def test_the_tile_total_grows_by_what_this_run_created():
    assert alltime_tile_total({"tile_count": 700}, created=17) == 717


def test_the_leading_edge_is_carried_forward():
    assert alltime_through({"through": "2026-08-30"}, TARGET) == TARGET


def test_a_first_run_leads_with_today():
    assert alltime_through(None, TARGET) == TARGET


def test_backfilling_an_earlier_day_does_not_move_the_leading_edge_back():
    # This is the manifest-alltime bug (troth c4e532a9): a historical backfill
    # of, say, 2010-06-20 must not regress "through" behind whatever the
    # daily run already advanced it to.
    assert alltime_through({"through": "2026-08-30"}, date(2010, 6, 20)) == date(2026, 8, 30)


def test_a_later_run_still_advances_the_leading_edge():
    assert alltime_through({"through": "2026-08-25"}, TARGET) == TARGET


@pytest.mark.parametrize("prior", [{}, {"through": None}, {"through": "not-a-date"}, {"through": 7}])
def test_an_unusable_prior_through_falls_back_to_today(prior):
    assert alltime_through(prior, TARGET) == TARGET


def test_the_tile_total_starts_from_nothing_without_a_prior_manifest():
    assert alltime_tile_total(None, created=5) == 5


@pytest.mark.parametrize("prior", [{"tile_count": "many"}, {"tile_count": None}, {}])
def test_an_unusable_prior_total_starts_from_this_run(prior):
    assert alltime_tile_total(prior, created=5) == 5
