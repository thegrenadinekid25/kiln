"""Scaling refusal, QC bits, the plausibility band and candidate selection."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from kiln_scan.grid import CMG_COLS, CMG_ROWS, CMG_SHAPE, cell_center_lat, cell_center_lon
from kiln_scan.science import (
    EXPECTED_LST_SCALE_FACTOR,
    HIGH_LATITUDE_DEGREES,
    HIGH_LATITUDE_MAX_C,
    KELVIN_ZERO_C,
    PHYSICAL_MAX_C,
    DayField,
    UnexpectedGranuleError,
    day_maximum,
    decode_lst_celsius,
    plausibility_keep_mask,
    prepare_day,
    qc_keep_mask,
    resolve_lst_scaling,
    select_candidates,
    top_candidates,
)

# The attributes a real MOD11C1 LST_Day_CMG SDS carries, read off
# MOD11C1.A2019196.061.2020356040840.hdf.
REAL_LST_ATTRS = {
    "units": "K",
    "valid_range": [7500, 65535],
    "_FillValue": 0,
    "scale_factor": 0.02,
    "add_offset": 0.0,
}


def counts_for(celsius: float) -> int:
    """The stored uint16 count that decodes to roughly this Celsius value."""
    return int(round((celsius + KELVIN_ZERO_C) / EXPECTED_LST_SCALE_FACTOR))


# --- Scaling refusal ----------------------------------------------------------------


def test_scaling_reads_the_real_attributes():
    scaling = resolve_lst_scaling(REAL_LST_ATTRS)
    assert scaling.scale_factor == pytest.approx(0.02)
    assert scaling.add_offset == 0.0
    assert scaling.fill_value == 0
    assert scaling.valid_range == (7500, 65535)


def test_missing_scale_factor_is_refused():
    with pytest.raises(UnexpectedGranuleError, match="no scale_factor"):
        resolve_lst_scaling({"_FillValue": 0})


def test_a_different_scale_factor_is_refused_rather_than_guessed():
    attrs = dict(REAL_LST_ATTRS, scale_factor=0.1)
    with pytest.raises(UnexpectedGranuleError, match="refusing to guess"):
        resolve_lst_scaling(attrs)


def test_a_scale_factor_off_by_a_rounding_error_is_accepted():
    attrs = dict(REAL_LST_ATTRS, scale_factor=0.020000001)
    assert resolve_lst_scaling(attrs).scale_factor == pytest.approx(0.02)


def test_a_malformed_valid_range_is_refused():
    with pytest.raises(UnexpectedGranuleError, match="valid_range"):
        resolve_lst_scaling(dict(REAL_LST_ATTRS, valid_range=[7500]))
    with pytest.raises(UnexpectedGranuleError, match="inverted"):
        resolve_lst_scaling(dict(REAL_LST_ATTRS, valid_range=[65535, 7500]))


def test_absent_valid_range_is_allowed():
    attrs = {k: v for k, v in REAL_LST_ATTRS.items() if k != "valid_range"}
    assert resolve_lst_scaling(attrs).valid_range is None


# --- Decoding -----------------------------------------------------------------------


def test_counts_decode_to_celsius():
    scaling = resolve_lst_scaling(REAL_LST_ATTRS)
    raw = np.array([[counts_for(70.19), counts_for(0.0)]], dtype=np.uint16)
    celsius, valid = decode_lst_celsius(raw, scaling)

    assert celsius[0, 0] == pytest.approx(70.19, abs=0.01)
    assert celsius[0, 1] == pytest.approx(0.0, abs=0.01)
    assert valid.all()


def test_fill_counts_are_invalid():
    scaling = resolve_lst_scaling(REAL_LST_ATTRS)
    raw = np.array([[0, counts_for(40.0)]], dtype=np.uint16)
    _, valid = decode_lst_celsius(raw, scaling)
    assert valid.tolist() == [[False, True]]


def test_counts_below_the_declared_valid_range_are_invalid():
    # 7499 decodes to -123.17 C, which is inside our own physical band; only the
    # file's own valid_range rejects it.
    scaling = resolve_lst_scaling(REAL_LST_ATTRS)
    raw = np.array([[7499, 7500]], dtype=np.uint16)
    celsius, valid = decode_lst_celsius(raw, scaling)
    assert celsius[0, 0] > -150.0
    assert valid.tolist() == [[False, True]]


def test_counts_outside_the_physical_band_are_invalid_even_without_a_valid_range():
    attrs = {k: v for k, v in REAL_LST_ATTRS.items() if k != "valid_range"}
    scaling = resolve_lst_scaling(attrs)
    raw = np.array([[counts_for(PHYSICAL_MAX_C + 5.0)]], dtype=np.uint16)
    _, valid = decode_lst_celsius(raw, scaling)
    assert not valid.any()


# --- QC bits ------------------------------------------------------------------------


def test_qc_keeps_good_and_other_quality_with_small_error():
    # bits 0-1 mandatory QA, bits 6-7 error class.
    qc = np.array([[0b00000000, 0b00000001, 0b01000000, 0b01000001]], dtype=np.uint8)
    assert qc_keep_mask(qc).tolist() == [[True, True, True, True]]


def test_qc_rejects_not_produced_cells():
    # 10 = not produced (cloud), 11 = not produced (other). This is the mask
    # that makes the whole scan clear-sky only.
    qc = np.array([[0b00000010, 0b00000011]], dtype=np.uint8)
    assert qc_keep_mask(qc).tolist() == [[False, False]]


def test_qc_rejects_large_error_classes():
    # error class 10 (<= 3K) and 11 (> 3K) both exceed the default bar of 1.
    qc = np.array([[0b10000000, 0b11000000]], dtype=np.uint8)
    assert qc_keep_mask(qc).tolist() == [[False, False]]


def test_qc_error_bar_is_adjustable():
    qc = np.array([[0b10000000]], dtype=np.uint8)
    assert qc_keep_mask(qc, max_error_class=2).tolist() == [[True]]


def test_qc_ignores_the_middle_bits():
    # Bits 2-5 carry emissivity and LST accuracy detail we do not screen on.
    # The 2019-07-15 Lut maximum carried QC byte 0b00001000, and it must pass.
    qc = np.array([[0b00001000, 0b00111100]], dtype=np.uint8)
    assert qc_keep_mask(qc).tolist() == [[True, True]]


# --- Plausibility band --------------------------------------------------------------


def test_plausibility_rejects_only_hot_high_latitude_cells():
    celsius = np.array([[78.75, 78.75, 55.0, 70.19]])
    lat = np.array([[64.96, 31.0, 64.96, 29.575]])
    assert plausibility_keep_mask(celsius, lat).tolist() == [[False, True, True, True]]


def test_plausibility_band_edges_are_exclusive():
    # Exactly at 50 degrees, or exactly at 60 C, is kept: the screen fires only
    # when both are strictly exceeded.
    celsius = np.array(
        [[HIGH_LATITUDE_MAX_C, HIGH_LATITUDE_MAX_C + 0.01, HIGH_LATITUDE_MAX_C + 0.01]]
    )
    lat = np.array(
        [
            [
                HIGH_LATITUDE_DEGREES + 0.01,
                HIGH_LATITUDE_DEGREES,
                HIGH_LATITUDE_DEGREES + 0.01,
            ]
        ]
    )
    assert plausibility_keep_mask(celsius, lat).tolist() == [[True, True, False]]


def test_plausibility_applies_in_the_southern_hemisphere():
    celsius = np.array([[70.0]])
    lat = np.array([[-70.0]])
    assert plausibility_keep_mask(celsius, lat).tolist() == [[False]]


def test_plausibility_broadcasts_a_latitude_column_over_a_grid():
    celsius = np.full((3, 4), 65.0)
    lat = np.array([[80.0], [30.0], [-80.0]])
    keep = plausibility_keep_mask(celsius, lat)
    assert keep.shape == (3, 4)
    assert keep[0].tolist() == [False] * 4
    assert keep[1].tolist() == [True] * 4
    assert keep[2].tolist() == [False] * 4


# --- Whole-day preparation ----------------------------------------------------------


def _cmg_day(cells: dict[tuple[int, int], tuple[float, int]]) -> tuple[np.ndarray, np.ndarray]:
    """A full-size CMG day that is fill everywhere except the named cells.

    Each cell is given as ``(row, col): (celsius, qc_byte)``.
    """
    raw = np.zeros(CMG_SHAPE, dtype=np.uint16)
    qc = np.full(CMG_SHAPE, 0b00000011, dtype=np.uint8)  # not produced
    for (row, col), (celsius, qc_byte) in cells.items():
        raw[row, col] = counts_for(celsius)
        qc[row, col] = qc_byte
    return raw, qc


def test_prepare_day_keeps_a_good_hot_cell():
    raw, qc = _cmg_day({(1208, 4784): (70.19, 0b00001000)})
    field = prepare_day(raw, REAL_LST_ATTRS, qc)

    assert field.kept == 1
    assert field.celsius[1208, 4784] == pytest.approx(70.19, abs=0.01)
    assert field.keep[1208, 4784]


def test_prepare_day_drops_a_cloudy_cell_and_counts_it():
    raw, qc = _cmg_day({(1208, 4784): (70.19, 0b00000010)})
    field = prepare_day(raw, REAL_LST_ATTRS, qc)

    assert field.kept == 0
    assert field.qc_dropped == 1


def test_prepare_day_drops_an_implausible_high_latitude_cell_and_counts_it():
    # Row 500 sits at 64.975 N, past the 50-degree line.
    raw, qc = _cmg_day({(500, 100): (78.75, 0b00000000)})
    field = prepare_day(raw, REAL_LST_ATTRS, qc)

    assert cell_center_lat(500) > HIGH_LATITUDE_DEGREES
    assert field.kept == 0
    assert field.implausible_dropped == 1


def test_prepare_day_keeps_the_same_temperature_at_a_low_latitude():
    # Row 1208 sits at 29.575 N. The screen is about where, not just how hot.
    raw, qc = _cmg_day({(1208, 100): (78.75, 0b00000000)})
    field = prepare_day(raw, REAL_LST_ATTRS, qc)
    assert field.kept == 1


def test_prepare_day_refuses_a_grid_of_the_wrong_size():
    raw = np.zeros((100, 100), dtype=np.uint16)
    qc = np.zeros((100, 100), dtype=np.uint8)
    with pytest.raises(Exception, match="expected"):
        prepare_day(raw, REAL_LST_ATTRS, qc)


def test_prepare_day_refuses_mismatched_lst_and_qc():
    raw = np.zeros(CMG_SHAPE, dtype=np.uint16)
    qc = np.zeros((CMG_ROWS, CMG_COLS - 1), dtype=np.uint8)
    with pytest.raises(Exception, match="expected"):
        prepare_day(raw, REAL_LST_ATTRS, qc)


# --- Candidate selection ------------------------------------------------------------


def _field_from(values: dict[tuple[int, int], float]) -> DayField:
    celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
    keep = np.zeros(CMG_SHAPE, dtype=bool)
    for (row, col), value in values.items():
        celsius[row, col] = value
        keep[row, col] = True
    return DayField(celsius=celsius, keep=keep, qc_dropped=0, implausible_dropped=0)


def test_candidates_use_an_inclusive_bar():
    field = _field_from({(0, 0): 54.99, (0, 1): 55.0, (0, 2): 55.01})
    values = [round(c.max_c, 2) for c in select_candidates(field, date(2019, 7, 15), 55.0)]
    assert values == [55.01, 55.0]


def test_candidates_carry_the_cell_center_coordinates():
    field = _field_from({(1208, 4784): 70.19})
    (candidate,) = select_candidates(field, date(2019, 7, 15), 55.0)

    assert candidate.day == date(2019, 7, 15)
    assert candidate.cell_lat == pytest.approx(cell_center_lat(1208))
    assert candidate.cell_lon == pytest.approx(cell_center_lon(4784))
    assert candidate.max_c == pytest.approx(70.19)


def test_candidates_come_back_hottest_first():
    field = _field_from({(0, 0): 56.0, (0, 1): 60.0, (0, 2): 58.0})
    values = [c.max_c for c in select_candidates(field, date(2019, 7, 15), 55.0)]
    assert values == sorted(values, reverse=True)


def test_candidates_ignore_hot_cells_the_mask_rejected():
    field = _field_from({(0, 0): 56.0})
    field.keep[0, 0] = False
    field.celsius[0, 1] = 99.0  # hot but never kept
    assert select_candidates(field, date(2019, 7, 15), 55.0) == []


def test_a_day_with_nothing_hot_yields_no_candidates():
    field = _field_from({(0, 0): 20.0})
    assert select_candidates(field, date(2019, 7, 15), 55.0) == []


def test_day_maximum_finds_the_hottest_kept_cell():
    field = _field_from({(0, 0): 30.0, (1208, 4784): 70.19})
    celsius, lat, lon = day_maximum(field)

    assert celsius == pytest.approx(70.19)
    assert lat == pytest.approx(29.575)
    assert lon == pytest.approx(59.225)


def test_day_maximum_of_an_entirely_masked_day_is_none():
    field = DayField(
        celsius=np.full(CMG_SHAPE, 99.0),
        keep=np.zeros(CMG_SHAPE, dtype=bool),
        qc_dropped=0,
        implausible_dropped=0,
    )
    assert day_maximum(field) is None


def test_top_candidates_ranks_across_days():
    field = _field_from({(0, 0): 56.0, (0, 1): 61.0})
    first = select_candidates(field, date(2003, 8, 1), 55.0)
    second = select_candidates(_field_from({(0, 2): 58.0}), date(2019, 7, 15), 55.0)

    top = top_candidates(first + second, 2)
    assert [round(c.max_c, 2) for c in top] == [61.0, 58.0]
    assert top[0].day == date(2003, 8, 1)


def test_top_candidates_asking_for_more_than_exist_returns_all():
    field = _field_from({(0, 0): 56.0})
    candidates = select_candidates(field, date(2019, 7, 15), 55.0)
    assert len(top_candidates(candidates, 100)) == 1


def test_top_candidates_refuses_a_negative_count():
    with pytest.raises(ValueError):
        top_candidates([], -1)
