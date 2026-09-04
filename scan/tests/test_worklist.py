"""Grid-to-jobs: cell extraction at the bar, clustering, and the jobs file shape."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from kiln_scan.grid import CMG_SHAPE, AlltimeGrid, cell_center_lat, cell_center_lon
from kiln_scan.worklist import (
    DEFAULT_MERGE_DEGREES,
    DEFAULT_PAD_DEGREES,
    Bbox,
    Job,
    build_jobs,
    cell_box,
    cluster_boxes,
    expected_granule_range,
    extract_hot_cells,
    jobs_from_payload,
    jobs_to_payload,
    merge_hot_cells,
    pad_box,
    split_at_antimeridian,
)


def fold(grid: AlltimeGrid, cells: dict[tuple[int, int], float], day: date) -> None:
    celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
    keep = np.zeros(CMG_SHAPE, dtype=bool)
    for (row, col), value in cells.items():
        celsius[row, col] = value
        keep[row, col] = True
    grid.fold_day(celsius, keep, day)


# --- Boxes --------------------------------------------------------------------------


def test_cell_box_covers_the_cell_not_just_its_centre():
    # The recorded maximum is an average over the whole 0.05-degree cell, so a
    # job drawn from it has to cover the cell.
    box = cell_box(29.575, 59.225)
    assert box.west == pytest.approx(59.2)
    assert box.east == pytest.approx(59.25)
    assert box.south == pytest.approx(29.55)
    assert box.north == pytest.approx(29.6)


def test_union_covers_both_boxes():
    merged = Bbox(0, 0, 1, 1).union(Bbox(5, -2, 6, 3))
    assert merged.as_list() == [0.0, -2.0, 6.0, 3.0]


def test_gap_between_separated_boxes():
    lat_gap, lon_gap = Bbox(0, 0, 1, 1).gap_to(Bbox(4, 10, 5, 11))
    assert lon_gap == pytest.approx(3.0)
    assert lat_gap == pytest.approx(9.0)


def test_gap_between_overlapping_boxes_is_zero():
    assert Bbox(0, 0, 5, 5).gap_to(Bbox(2, 2, 8, 8)) == (0.0, 0.0)


def test_padding_grows_every_side():
    padded = pad_box(Bbox(10, 10, 11, 11), 0.5)
    assert padded.as_list() == [9.5, 9.5, 11.5, 11.5]


def test_padding_clamps_at_the_poles():
    padded = pad_box(Bbox(0, -89.9, 1, 89.9), 0.5)
    assert padded.south == -90.0
    assert padded.north == 90.0


def test_padding_refuses_a_negative_amount():
    with pytest.raises(ValueError):
        pad_box(Bbox(0, 0, 1, 1), -1.0)


# --- Antimeridian -------------------------------------------------------------------


def test_a_box_inside_the_globe_is_left_alone():
    box = Bbox(10, 10, 11, 11)
    assert split_at_antimeridian(box) == [box]


def test_a_box_running_past_the_east_edge_is_split():
    # Padding a cell at 179.975 E pushes the east edge to 180.5. Handing that to
    # the ingest CLI would either be rejected or read as a box spanning almost
    # the whole globe backwards.
    pieces = split_at_antimeridian(Bbox(179.4, 10, 180.5, 11))
    assert [p.as_list() for p in pieces] == [
        [179.4, 10.0, 180.0, 11.0],
        [-180.0, 10.0, -179.5, 11.0],
    ]


def test_a_box_running_past_the_west_edge_is_split():
    pieces = split_at_antimeridian(Bbox(-180.5, 10, -179.4, 11))
    assert [p.as_list() for p in pieces] == [
        [179.5, 10.0, 180.0, 11.0],
        [-180.0, 10.0, -179.4, 11.0],
    ]


def test_a_box_wider_than_the_globe_becomes_one_full_width_box():
    pieces = split_at_antimeridian(Bbox(-200, 10, 200, 11))
    assert [p.as_list() for p in pieces] == [[-180.0, 10.0, 180.0, 11.0]]


def test_no_clustered_box_ever_crosses_the_antimeridian():
    # Cells right against both edges, at every latitude extreme.
    points = [(0.025, 179.975), (0.025, -179.975), (89.975, 179.975), (-89.975, -179.975)]
    for box in cluster_boxes(points):
        assert -180.0 <= box.west < box.east <= 180.0
        assert -90.0 <= box.south < box.north <= 90.0


def test_cells_either_side_of_the_antimeridian_stay_separate():
    # Clustering is linear in longitude, so these read as 360 degrees apart and
    # produce two jobs rather than one. Documented behaviour, not a bug.
    boxes = cluster_boxes([(0.025, 179.975), (0.025, -179.975)])
    assert len(boxes) >= 2


# --- Clustering ---------------------------------------------------------------------


def test_no_points_makes_no_boxes():
    assert cluster_boxes([]) == []


def test_one_point_makes_one_padded_box():
    (box,) = cluster_boxes([(29.575, 59.225)], pad_degrees=0.5)
    assert box.as_list() == [58.7, 29.05, 59.75, 30.1]


def test_nearby_cells_merge_into_one_box():
    # Two cells one degree apart, well inside the 3-degree merge distance.
    boxes = cluster_boxes([(30.0, 60.0), (30.0, 61.0)], merge_degrees=3.0)
    assert len(boxes) == 1
    assert boxes[0].west < 60.0 < 61.0 < boxes[0].east


def test_distant_cells_stay_in_separate_boxes():
    # The Lut Desert and the Sonoran Desert are not one region.
    boxes = cluster_boxes([(30.0, 60.0), (33.0, -114.0)], merge_degrees=3.0)
    assert len(boxes) == 2


def test_cells_exactly_at_the_merge_distance_merge():
    boxes = cluster_boxes([(30.0, 60.0), (30.0, 63.0)], merge_degrees=3.0)
    assert len(boxes) == 1


def test_cells_beyond_the_merge_distance_do_not():
    boxes = cluster_boxes([(30.0, 60.0), (30.0, 64.0)], merge_degrees=3.0)
    assert len(boxes) == 2


def test_a_chain_of_cells_merges_transitively():
    # A joins B and B joins C, so all three are one region even though A and C
    # are 6 degrees apart. This is what the repeated merge pass buys.
    boxes = cluster_boxes(
        [(30.0, 60.0), (30.0, 63.0), (30.0, 66.0)], merge_degrees=3.0
    )
    assert len(boxes) == 1
    assert boxes[0].west < 60.0 and boxes[0].east > 66.0


def test_clustering_is_independent_of_input_order():
    points = [(30.0, 60.0), (30.0, 66.0), (30.0, 63.0), (10.0, -70.0)]
    forward = cluster_boxes(points)
    backward = cluster_boxes(list(reversed(points)))
    assert [b.as_list() for b in forward] == [b.as_list() for b in backward]


def test_merging_needs_closeness_in_both_axes():
    # Same longitude, 20 degrees of latitude apart: not one region.
    boxes = cluster_boxes([(10.0, 60.0), (30.0, 60.0)], merge_degrees=3.0)
    assert len(boxes) == 2


def test_a_negative_merge_distance_is_refused():
    with pytest.raises(ValueError):
        cluster_boxes([(0.0, 0.0)], merge_degrees=-1.0)


def test_every_clustered_box_contains_the_cells_that_made_it():
    points = [(30.0, 60.0), (30.5, 61.0), (33.0, -114.0)]
    boxes = cluster_boxes(points)
    for lat, lon in points:
        assert any(
            box.west <= lon <= box.east and box.south <= lat <= box.north
            for box in boxes
        )


# --- Extraction from the grid -------------------------------------------------------


def test_extraction_takes_cells_at_or_above_the_bar_with_their_dates():
    grid = AlltimeGrid.empty()
    fold(grid, {(1208, 4784): 70.19}, date(2019, 7, 15))
    fold(grid, {(1000, 2000): 61.0}, date(2003, 8, 1))
    fold(grid, {(1001, 2001): 59.99}, date(2005, 6, 1))  # under the bar

    cells, undatable = extract_hot_cells(grid, 60.0)

    assert len(cells) == 2
    assert undatable == 0
    assert sorted(cells.date_int.tolist()) == [20030801, 20190715]
    assert sorted(round(v, 2) for v in cells.max_c.tolist()) == [61.0, 70.19]


def test_extraction_reports_the_cell_centre_coordinates():
    grid = AlltimeGrid.empty()
    fold(grid, {(1208, 4784): 70.19}, date(2019, 7, 15))
    cells, _ = extract_hot_cells(grid, 60.0)

    assert cells.lat[0] == pytest.approx(cell_center_lat(1208))
    assert cells.lon[0] == pytest.approx(cell_center_lon(4784))


def test_extraction_uses_an_inclusive_bar():
    grid = AlltimeGrid.empty()
    fold(grid, {(0, 0): 59.99, (0, 1): 60.0, (0, 2): 60.01}, date(2005, 1, 1))
    cells, _ = extract_hot_cells(grid, 60.0)
    assert len(cells) == 2


def test_extraction_ignores_never_observed_cells():
    grid = AlltimeGrid.empty()
    cells, undatable = extract_hot_cells(grid, -400.0)
    assert len(cells) == 0
    assert undatable == 0


def test_a_hot_cell_with_no_date_is_counted_not_guessed_at():
    # Should never happen -- the scanner writes both together -- but a cell with
    # no date has no day to refine on, so it must be reported, not invented.
    grid = AlltimeGrid.empty()
    fold(grid, {(0, 0): 70.0}, date(2019, 7, 15))
    grid.date_int[0, 0] = 0

    cells, undatable = extract_hot_cells(grid, 60.0)
    assert len(cells) == 0
    assert undatable == 1


def test_grouping_splits_cells_by_their_record_date():
    grid = AlltimeGrid.empty()
    fold(grid, {(1000, 2000): 65.0, (1000, 2001): 66.0}, date(2003, 8, 1))
    fold(grid, {(1200, 4784): 70.0}, date(2019, 7, 15))

    cells, _ = extract_hot_cells(grid, 60.0)
    groups = dict(cells.group_by_date())
    assert sorted(groups) == [date(2003, 8, 1), date(2019, 7, 15)]
    assert len(groups[date(2003, 8, 1)]) == 2


def test_merging_two_products_keeps_both_dates_for_a_shared_cell():
    # Terra and Aqua can disagree about which day a cell was hottest. Both days
    # are worth refining, so neither is dropped.
    terra = AlltimeGrid.empty()
    fold(terra, {(1208, 4784): 70.19}, date(2019, 7, 15))
    aqua = AlltimeGrid.empty()
    fold(aqua, {(1208, 4784): 71.77}, date(2003, 8, 1))

    merged = merge_hot_cells(
        [extract_hot_cells(terra, 60.0)[0], extract_hot_cells(aqua, 60.0)[0]]
    )
    assert len(merged) == 2
    assert {day for day, _ in merged.group_by_date()} == {
        date(2019, 7, 15),
        date(2003, 8, 1),
    }


def test_merging_no_products_gives_an_empty_set():
    assert len(merge_hot_cells([])) == 0


# --- Jobs ---------------------------------------------------------------------------


def test_one_job_per_date():
    grid = AlltimeGrid.empty()
    fold(grid, {(1000, 2000): 65.0}, date(2003, 8, 1))
    fold(grid, {(1200, 4784): 70.0}, date(2019, 7, 15))

    jobs = build_jobs(extract_hot_cells(grid, 60.0)[0])
    assert [job.day for job in jobs] == [date(2019, 7, 15), date(2003, 8, 1)]


def test_jobs_come_back_hottest_first():
    grid = AlltimeGrid.empty()
    fold(grid, {(100, 100): 62.0}, date(2001, 1, 1))
    fold(grid, {(200, 200): 70.0}, date(2002, 2, 2))
    fold(grid, {(300, 300): 66.0}, date(2003, 3, 3))

    values = [job.cmg_max_c for job in build_jobs(extract_hot_cells(grid, 60.0)[0])]
    assert values == sorted(values, reverse=True)


def test_a_job_reports_its_hottest_cell_and_cell_count():
    grid = AlltimeGrid.empty()
    fold(grid, {(1000, 2000): 65.0, (1000, 2001): 68.0}, date(2003, 8, 1))

    (job,) = build_jobs(extract_hot_cells(grid, 60.0)[0])
    assert job.cells == 2
    assert job.cmg_max_c == pytest.approx(68.0, abs=0.01)


def test_one_date_can_produce_several_far_apart_boxes():
    # The Sahara and the Sonoran Desert can both peak on the same day, and
    # merging them into one box would fetch the Atlantic.
    grid = AlltimeGrid.empty()
    sahara_row = int(round((89.975 - 25.0) / 0.05))
    sahara_col = int(round((10.0 + 179.975) / 0.05))
    sonoran_row = int(round((89.975 - 33.0) / 0.05))
    sonoran_col = int(round((-114.0 + 179.975) / 0.05))
    fold(
        grid,
        {(sahara_row, sahara_col): 65.0, (sonoran_row, sonoran_col): 66.0},
        date(2003, 8, 1),
    )

    (job,) = build_jobs(extract_hot_cells(grid, 60.0)[0])
    assert len(job.bboxes) == 2


def test_adjacent_cells_with_different_dates_do_not_share_a_job():
    # The date is the load-bearing part: refining a cell on a neighbour's date
    # would measure a different day and attribute it to the record.
    grid = AlltimeGrid.empty()
    fold(grid, {(1000, 2000): 65.0}, date(2003, 8, 1))
    fold(grid, {(1000, 2001): 66.0}, date(2011, 7, 2))

    jobs = build_jobs(extract_hot_cells(grid, 60.0)[0])
    assert len(jobs) == 2
    assert all(len(job.bboxes) == 1 for job in jobs)


def test_no_cells_makes_no_jobs():
    assert build_jobs(extract_hot_cells(AlltimeGrid.empty(), 60.0)[0]) == []


# --- Jobs file ----------------------------------------------------------------------


def test_jobs_payload_shape():
    grid = AlltimeGrid.empty()
    fold(grid, {(1208, 4784): 70.19}, date(2019, 7, 15))
    jobs = build_jobs(extract_hot_cells(grid, 60.0)[0])

    payload = jobs_to_payload(
        jobs, 60.0, ["MOD11C1"], DEFAULT_MERGE_DEGREES, DEFAULT_PAD_DEGREES
    )
    assert payload["bar_c"] == 60.0
    assert payload["source_products"] == ["MOD11C1"]

    (entry,) = payload["jobs"]
    assert entry["date"] == "2019-07-15"
    assert entry["cells"] == 1
    assert entry["cmg_max_c"] == pytest.approx(70.19)
    assert len(entry["bboxes"]) == 1
    assert len(entry["bboxes"][0]) == 4


def test_jobs_payload_is_json_serialisable_and_round_trips():
    grid = AlltimeGrid.empty()
    fold(grid, {(1208, 4784): 70.19}, date(2019, 7, 15))
    fold(grid, {(1000, 2000): 61.0}, date(2003, 8, 1))
    jobs = build_jobs(extract_hot_cells(grid, 60.0)[0])

    payload = json.loads(
        json.dumps(jobs_to_payload(jobs, 60.0, ["MOD11C1"], 3.0, 0.5))
    )
    restored = jobs_from_payload(payload)

    assert [job.day for job in restored] == [job.day for job in jobs]
    assert [b.as_list() for b in restored[0].bboxes] == [
        b.as_list() for b in jobs[0].bboxes
    ]


def test_reading_a_payload_without_jobs_is_refused():
    with pytest.raises(ValueError, match="no 'jobs' array"):
        jobs_from_payload({"bar_c": 60.0})


def test_reading_a_job_without_a_date_is_refused():
    with pytest.raises(ValueError, match="no usable date"):
        jobs_from_payload({"jobs": [{"bboxes": [[0, 0, 1, 1]]}]})


def test_reading_a_job_without_boxes_is_refused():
    with pytest.raises(ValueError, match="no bounding boxes"):
        jobs_from_payload({"jobs": [{"date": "2019-07-15", "bboxes": []}]})


def test_reading_a_malformed_box_is_refused():
    with pytest.raises(ValueError, match="malformed box"):
        jobs_from_payload({"jobs": [{"date": "2019-07-15", "bboxes": [[0, 0, 1]]}]})


def test_reading_an_inverted_box_is_refused():
    with pytest.raises(ValueError, match="south at or above north"):
        jobs_from_payload({"jobs": [{"date": "2019-07-15", "bboxes": [[0, 5, 1, 2]]}]})
    with pytest.raises(ValueError, match="west at or above east"):
        jobs_from_payload({"jobs": [{"date": "2019-07-15", "bboxes": [[5, 0, 2, 1]]}]})


def test_reading_a_box_off_the_globe_is_refused():
    # The guard that would catch a clustering bug before it reached the archive.
    with pytest.raises(ValueError, match="runs off the globe"):
        jobs_from_payload(
            {"jobs": [{"date": "2019-07-15", "bboxes": [[179.5, 0, 180.5, 1]]}]}
        )


def test_expected_granule_range_scales_with_boxes_not_jobs():
    # A job covering two far-apart regions fetches both regions' overpasses, so
    # the box count is what drives the download, not the job count.
    one_box = [Job(date(2019, 7, 15), (Bbox(0, 0, 1, 1),), 1, 70.0)]
    three_boxes = [
        Job(
            date(2019, 7, 15),
            (Bbox(0, 0, 1, 1), Bbox(10, 0, 11, 1), Bbox(20, 0, 21, 1)),
            3,
            70.0,
        )
    ]
    assert expected_granule_range(one_box, products=2) == (2, 8)
    assert expected_granule_range(three_boxes, products=2) == (6, 24)


def test_expected_granule_range_of_nothing_is_nothing():
    assert expected_granule_range([], products=2) == (0, 0)
