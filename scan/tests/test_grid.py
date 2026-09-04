"""CMG geometry and the running all-time accumulator."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kiln_scan.grid import (
    CMG_COLS,
    CMG_ROWS,
    CMG_SHAPE,
    UNSET_CENTI,
    UNSET_DATE,
    AlltimeGrid,
    GridShapeError,
    celsius_to_centi,
    cell_center_lat,
    cell_center_lon,
    check_cmg_shape,
    col_center_lons,
    date_to_int,
    int_to_date,
    row_center_lats,
)


# --- Cell coordinate math -----------------------------------------------------------


def test_first_cell_is_the_northwest_corner():
    # Row 0 / column 0 spans 90.00-89.95 N and 180.00-179.95 W; its centre is
    # half a cell in from each edge.
    assert cell_center_lat(0) == pytest.approx(89.975)
    assert cell_center_lon(0) == pytest.approx(-179.975)


def test_last_cell_is_the_southeast_corner():
    assert cell_center_lat(CMG_ROWS - 1) == pytest.approx(-89.975)
    assert cell_center_lon(CMG_COLS - 1) == pytest.approx(179.975)


def test_grid_center_straddles_the_equator_and_prime_meridian():
    # With an even row count there is no cell centred exactly on the equator;
    # the two middle rows sit half a cell either side of it.
    assert cell_center_lat(CMG_ROWS // 2 - 1) == pytest.approx(0.025)
    assert cell_center_lat(CMG_ROWS // 2) == pytest.approx(-0.025)
    assert cell_center_lon(CMG_COLS // 2 - 1) == pytest.approx(-0.025)
    assert cell_center_lon(CMG_COLS // 2) == pytest.approx(0.025)


def test_cell_centers_are_exact_to_three_decimals():
    # Not cosmetic: an unrounded 29.574999999999992 would reach every candidate
    # CSV row and every summary, and two runs could disagree in the last digit.
    assert float(cell_center_lat(1208)) == 29.575
    assert float(cell_center_lon(4784)) == 59.225
    lats = cell_center_lat(np.arange(CMG_ROWS))
    assert np.array_equal(lats, np.round(lats, 3))


def test_every_cell_center_lies_inside_the_globe():
    lats = cell_center_lat(np.arange(CMG_ROWS))
    lons = cell_center_lon(np.arange(CMG_COLS))
    assert lats.max() < 90.0 and lats.min() > -90.0
    assert lons.max() < 180.0 and lons.min() > -180.0


def test_centers_step_by_exactly_one_cell():
    lats = cell_center_lat(np.arange(CMG_ROWS))
    lons = cell_center_lon(np.arange(CMG_COLS))
    assert np.allclose(np.diff(lats), -0.05)
    assert np.allclose(np.diff(lons), 0.05)


def test_lut_desert_cell_round_trips_to_its_known_row_and_column():
    # The 2019-07-15 Terra maximum landed at 29.575 N, 59.225 E. Deriving the
    # indices back from those coordinates is the check that the scan's reported
    # position is the cell it actually read.
    row = int(round((89.975 - 29.575) / 0.05))
    col = int(round((59.225 + 179.975) / 0.05))
    assert cell_center_lat(row) == pytest.approx(29.575)
    assert cell_center_lon(col) == pytest.approx(59.225)


def test_row_and_column_vectors_broadcast_to_the_full_grid():
    lats = row_center_lats()
    lons = col_center_lons()
    assert lats.shape == (CMG_ROWS, 1)
    assert lons.shape == (CMG_COLS,)
    assert np.broadcast(lats, lons).shape == CMG_SHAPE


# --- Shape guard --------------------------------------------------------------------


def test_check_cmg_shape_rejects_a_differently_sized_grid():
    with pytest.raises(GridShapeError, match=r"expected \(3600, 7200\)"):
        check_cmg_shape(np.zeros((1800, 3600)), "LST")


def test_check_cmg_shape_rejects_a_transposed_grid():
    with pytest.raises(GridShapeError):
        check_cmg_shape(np.zeros((CMG_COLS, CMG_ROWS)), "LST")


# --- Centi-Celsius packing ----------------------------------------------------------


def test_celsius_to_centi_rounds_to_hundredths():
    packed = celsius_to_centi(np.array([0.0, 70.19, -40.005, 20.004]))
    assert packed.dtype == np.int16
    assert packed.tolist() == [0, 7019, -4001, 2000]


def test_celsius_to_centi_never_produces_the_unset_sentinel():
    packed = celsius_to_centi(np.array([-1000.0]))
    assert int(packed[0]) == UNSET_CENTI + 1


def test_date_int_round_trip():
    day = date(2019, 7, 15)
    assert date_to_int(day) == 20190715
    assert int_to_date(20190715) == day
    assert int_to_date(UNSET_DATE) is None


# --- All-time fold ------------------------------------------------------------------


def _tiny_grid() -> AlltimeGrid:
    """An accumulator with the CMG shape but only a corner exercised."""
    return AlltimeGrid.empty()


def _field(values: dict[tuple[int, int], float]) -> tuple[np.ndarray, np.ndarray]:
    celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
    keep = np.zeros(CMG_SHAPE, dtype=bool)
    for (row, col), value in values.items():
        celsius[row, col] = value
        keep[row, col] = True
    return celsius, keep


def test_empty_grid_has_no_observations():
    grid = _tiny_grid()
    assert not grid.observed().any()
    assert grid.global_max() is None
    assert grid.count_at_or_above(0.0) == 0


def test_fold_records_the_value_and_the_day_that_set_it():
    grid = _tiny_grid()
    celsius, keep = _field({(10, 20): 61.5})
    improved = grid.fold_day(celsius, keep, date(2003, 8, 1))

    assert improved == 1
    assert int(grid.max_centi[10, 20]) == 6150
    assert int(grid.date_int[10, 20]) == 20030801
    assert int(grid.max_centi[10, 21]) == UNSET_CENTI


def test_fold_keeps_the_hotter_day_and_moves_the_date_with_it():
    grid = _tiny_grid()
    grid.fold_day(*_field({(10, 20): 61.5}), date(2003, 8, 1))
    grid.fold_day(*_field({(10, 20): 64.0}), date(2010, 6, 2))

    assert int(grid.max_centi[10, 20]) == 6400
    assert int(grid.date_int[10, 20]) == 20100602


def test_fold_ignores_a_cooler_later_day():
    grid = _tiny_grid()
    grid.fold_day(*_field({(10, 20): 64.0}), date(2010, 6, 2))
    improved = grid.fold_day(*_field({(10, 20): 61.5}), date(2011, 6, 2))

    assert improved == 0
    assert int(grid.max_centi[10, 20]) == 6400
    assert int(grid.date_int[10, 20]) == 20100602


def test_a_tie_keeps_the_earlier_day():
    grid = _tiny_grid()
    grid.fold_day(*_field({(10, 20): 64.0}), date(2010, 6, 2))
    grid.fold_day(*_field({(10, 20): 64.0}), date(2015, 6, 2))
    assert int(grid.date_int[10, 20]) == 20100602


def test_refolding_the_same_day_changes_nothing():
    # This is what makes an interrupted scan safe to resume: a day that was
    # folded but never marked done gets folded again, and must be a no-op.
    grid = _tiny_grid()
    celsius, keep = _field({(10, 20): 64.0, (11, 21): 58.0})
    grid.fold_day(celsius, keep, date(2010, 6, 2))
    before_max = grid.max_centi.copy()
    before_date = grid.date_int.copy()

    improved = grid.fold_day(celsius, keep, date(2010, 6, 2))

    assert improved == 0
    assert np.array_equal(grid.max_centi, before_max)
    assert np.array_equal(grid.date_int, before_date)


def test_fold_ignores_hot_values_the_mask_rejects():
    grid = _tiny_grid()
    celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
    celsius[5, 5] = 300.0
    keep = np.zeros(CMG_SHAPE, dtype=bool)

    assert grid.fold_day(celsius, keep, date(2010, 6, 2)) == 0
    assert not grid.observed().any()


def test_fold_rejects_a_field_of_the_wrong_shape():
    grid = _tiny_grid()
    with pytest.raises(GridShapeError):
        grid.fold_day(np.zeros((10, 10)), np.ones((10, 10), dtype=bool), date(2010, 1, 1))


def test_global_max_reports_the_hottest_cell_with_its_place_and_day():
    grid = _tiny_grid()
    grid.fold_day(*_field({(0, 0): 30.0}), date(2001, 1, 1))
    grid.fold_day(*_field({(1208, 4784): 70.19}), date(2019, 7, 15))

    celsius, lat, lon, day = grid.global_max()
    assert celsius == pytest.approx(70.19)
    assert lat == pytest.approx(cell_center_lat(1208))
    assert lon == pytest.approx(cell_center_lon(4784))
    assert day == date(2019, 7, 15)


def test_count_at_or_above_uses_an_inclusive_bar():
    grid = _tiny_grid()
    grid.fold_day(
        *_field({(0, 0): 54.99, (0, 1): 55.0, (0, 2): 55.01}), date(2005, 1, 1)
    )
    assert grid.count_at_or_above(55.0) == 2


def test_count_at_or_above_ignores_never_observed_cells():
    # Unset cells hold int16's floor. A bar below that would count all 25.9
    # million of them if the observed mask were not applied.
    grid = _tiny_grid()
    grid.fold_day(*_field({(0, 0): 10.0}), date(2005, 1, 1))
    assert grid.count_at_or_above(-400.0) == 1


# --- Persistence --------------------------------------------------------------------


def test_grid_round_trips_through_disk(tmp_path):
    grid = _tiny_grid()
    grid.fold_day(*_field({(100, 200): 66.6}), date(2012, 7, 4))
    path = tmp_path / "alltime_cmg_MOD11C1.npz"
    grid.save(path)

    restored = AlltimeGrid.load(path)
    assert np.array_equal(restored.max_centi, grid.max_centi)
    assert np.array_equal(restored.date_int, grid.date_int)
    assert restored.global_max()[3] == date(2012, 7, 4)


def test_loading_a_missing_grid_starts_an_empty_one(tmp_path):
    grid = AlltimeGrid.load(tmp_path / "nothing-here.npz")
    assert not grid.observed().any()


def test_save_leaves_no_partial_file_behind(tmp_path):
    grid = _tiny_grid()
    path = tmp_path / "alltime_cmg_MOD11C1.npz"
    grid.save(path)
    assert path.exists()
    assert not (tmp_path / "alltime_cmg_MOD11C1.npz.part").exists()


def test_export_npy_writes_both_arrays(tmp_path):
    grid = _tiny_grid()
    grid.fold_day(*_field({(3, 4): 45.0}), date(2008, 5, 6))
    max_path = tmp_path / "max.npy"
    date_path = tmp_path / "dates.npy"
    grid.export_npy(max_path, date_path)

    assert np.load(max_path)[3, 4] == 4500
    assert np.load(date_path)[3, 4] == 20080506
