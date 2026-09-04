"""The done-log resume contract and the candidate CSVs."""

from __future__ import annotations

import csv
from datetime import date

import numpy as np
import pytest

from kiln_scan.science import Candidate
from kiln_scan.store import (
    CANDIDATE_HEADER,
    CandidateRow,
    CandidateWriter,
    DoneLog,
    alltime_grid_path,
    candidate_files,
    candidates_path,
    days_in_range,
    dedupe_candidates,
    done_log_path,
    format_candidate_row,
    parse_candidate_rows,
    parse_done_log,
    pending_days,
    read_candidate_files,
)


# --- Layout -------------------------------------------------------------------------


def test_paths_are_named_per_product(tmp_path):
    assert done_log_path(tmp_path, "MOD11C1").name == "done_MOD11C1.txt"
    assert alltime_grid_path(tmp_path, "MYD11C1").name == "alltime_cmg_MYD11C1.npz"
    assert (
        candidates_path(tmp_path, "MOD11C1", 2019).name == "candidates_MOD11C1_2019.csv"
    )


# --- Done-log parsing ---------------------------------------------------------------


def test_done_log_parses_iso_dates():
    assert parse_done_log(["2000-02-24", "2019-07-15"]) == {
        date(2000, 2, 24),
        date(2019, 7, 15),
    }


def test_done_log_ignores_blanks_and_comments():
    assert parse_done_log(["", "  ", "# a note", "2019-07-15"]) == {date(2019, 7, 15)}


def test_done_log_survives_a_truncated_final_line():
    # A run killed mid-append can leave a partial date. The log is an
    # optimisation, so a bad tail must cost one redone day, not the whole scan.
    assert parse_done_log(["2019-07-15", "2019-07-1"]) == {date(2019, 7, 15)}


# --- Done-log durability ------------------------------------------------------------


def test_marking_a_day_makes_it_durable_immediately(tmp_path):
    path = done_log_path(tmp_path, "MOD11C1")
    log = DoneLog.load(path)
    log.mark(date(2019, 7, 15))

    # A brand new reader, as if the process had died and restarted.
    assert date(2019, 7, 15) in DoneLog.load(path)


def test_marking_the_same_day_twice_writes_one_line(tmp_path):
    path = done_log_path(tmp_path, "MOD11C1")
    log = DoneLog.load(path)
    log.mark(date(2019, 7, 15))
    log.mark(date(2019, 7, 15))

    assert path.read_text().splitlines() == ["2019-07-15"]
    assert len(log) == 1


def test_a_missing_done_log_reads_as_nothing_done(tmp_path):
    log = DoneLog.load(done_log_path(tmp_path, "MOD11C1"))
    assert len(log) == 0
    assert date(2019, 7, 15) not in log


def test_done_log_appends_rather_than_replacing(tmp_path):
    path = done_log_path(tmp_path, "MOD11C1")
    DoneLog.load(path).mark(date(2019, 7, 15))
    DoneLog.load(path).mark(date(2019, 7, 16))

    assert DoneLog.load(path).days == {date(2019, 7, 15), date(2019, 7, 16)}


# --- Resume logic -------------------------------------------------------------------


def test_days_in_range_is_inclusive_at_both_ends():
    days = list(days_in_range(date(2019, 7, 14), date(2019, 7, 16)))
    assert days == [date(2019, 7, 14), date(2019, 7, 15), date(2019, 7, 16)]


def test_days_in_range_of_a_backwards_range_is_empty():
    assert list(days_in_range(date(2019, 7, 16), date(2019, 7, 14))) == []


def test_days_in_range_includes_a_leap_day():
    days = list(days_in_range(date(2000, 2, 28), date(2000, 3, 1)))
    assert date(2000, 2, 29) in days
    assert len(days) == 3


def test_pending_skips_days_already_done():
    done = {date(2019, 7, 15)}
    assert pending_days(date(2019, 7, 14), date(2019, 7, 16), done) == [
        date(2019, 7, 14),
        date(2019, 7, 16),
    ]


def test_pending_is_empty_when_everything_is_done():
    done = set(days_in_range(date(2019, 7, 14), date(2019, 7, 16)))
    assert pending_days(date(2019, 7, 14), date(2019, 7, 16), done) == []


def test_pending_honours_the_day_cap_and_takes_the_earliest():
    days = pending_days(date(2019, 7, 1), date(2019, 7, 31), done=[], limit=3)
    assert days == [date(2019, 7, 1), date(2019, 7, 2), date(2019, 7, 3)]


def test_pending_counts_the_cap_after_skipping_done_days():
    done = {date(2019, 7, 1), date(2019, 7, 2)}
    days = pending_days(date(2019, 7, 1), date(2019, 7, 31), done, limit=2)
    assert days == [date(2019, 7, 3), date(2019, 7, 4)]


def test_a_rerun_after_a_kill_resumes_from_the_interrupted_day(tmp_path):
    # The whole resume contract in one test: the scan marks days as it finishes
    # them, dies partway, and the next run picks up at the day it was on.
    path = done_log_path(tmp_path, "MOD11C1")
    log = DoneLog.load(path)
    for day in days_in_range(date(2019, 7, 1), date(2019, 7, 5)):
        log.mark(day)
    del log  # the process dies here, mid-way through 2019-07-06

    resumed = DoneLog.load(path)
    assert pending_days(date(2019, 7, 1), date(2019, 7, 8), resumed.days) == [
        date(2019, 7, 6),
        date(2019, 7, 7),
        date(2019, 7, 8),
    ]


# --- Candidate rows -----------------------------------------------------------------


def test_row_format_keeps_cell_centres_exact():
    row = format_candidate_row(
        Candidate(day=date(2019, 7, 15), cell_lat=29.575, cell_lon=59.225, max_c=70.19)
    )
    assert row == ("2019-07-15", "29.575", "59.225", "70.19")


def test_row_format_keeps_the_sign_of_a_southwestern_cell():
    row = format_candidate_row(
        Candidate(day=date(2019, 1, 2), cell_lat=-23.025, cell_lon=-69.975, max_c=61.5)
    )
    assert row == ("2019-01-02", "-23.025", "-69.975", "61.50")


def test_writer_creates_one_file_per_year_with_a_header(tmp_path):
    with CandidateWriter(tmp_path, "MOD11C1") as writer:
        writer.write_day(
            date(2019, 7, 15),
            [Candidate(date(2019, 7, 15), 29.575, 59.225, 70.19)],
        )
        writer.write_day(
            date(2020, 7, 15),
            [Candidate(date(2020, 7, 15), 29.575, 59.225, 68.0)],
        )

    for year in (2019, 2020):
        rows = list(csv.reader(candidates_path(tmp_path, "MOD11C1", year).open()))
        assert tuple(rows[0]) == CANDIDATE_HEADER
        assert len(rows) == 2


def test_writer_appends_across_runs_without_repeating_the_header(tmp_path):
    for value in (70.19, 68.0):
        with CandidateWriter(tmp_path, "MOD11C1") as writer:
            writer.write_day(
                date(2019, 7, 15), [Candidate(date(2019, 7, 15), 29.575, 59.225, value)]
            )

    rows = list(csv.reader(candidates_path(tmp_path, "MOD11C1", 2019).open()))
    assert [r[0] for r in rows] == ["date", "2019-07-15", "2019-07-15"]


def test_writer_of_an_empty_day_creates_no_file(tmp_path):
    with CandidateWriter(tmp_path, "MOD11C1") as writer:
        assert writer.write_day(date(2019, 1, 15), []) == 0
    assert candidate_files(tmp_path, "MOD11C1") == []


def test_parse_skips_the_header_and_unreadable_lines():
    rows = parse_candidate_rows(
        [
            list(CANDIDATE_HEADER),
            ["2019-07-15", "29.575", "59.225", "70.19"],
            ["2019-07-15", "29.575"],  # truncated by a kill mid-write
            ["not-a-date", "1", "2", "3"],
        ]
    )
    assert len(rows) == 1
    assert rows[0].max_c == pytest.approx(70.19)


def test_dedupe_collapses_a_day_reprocessed_after_an_interrupted_run():
    # The one duplication a crash can produce: candidates written, then the
    # process dies before the day is marked done, so the rerun writes them again.
    row = CandidateRow(date(2019, 7, 15), 29.575, 59.225, 70.19)
    assert dedupe_candidates([row, row]) == [row]


def test_dedupe_keeps_distinct_cells_on_the_same_day():
    a = CandidateRow(date(2019, 7, 15), 29.575, 59.225, 70.19)
    b = CandidateRow(date(2019, 7, 15), 29.625, 59.225, 69.59)
    assert len(dedupe_candidates([a, b])) == 2


def test_dedupe_keeps_the_same_cell_on_different_days():
    a = CandidateRow(date(2019, 7, 15), 29.575, 59.225, 70.19)
    b = CandidateRow(date(2020, 7, 15), 29.575, 59.225, 68.0)
    assert len(dedupe_candidates([a, b])) == 2


def test_read_candidate_files_spans_years_and_deduplicates(tmp_path):
    with CandidateWriter(tmp_path, "MOD11C1") as writer:
        writer.write_day(
            date(2019, 7, 15), [Candidate(date(2019, 7, 15), 29.575, 59.225, 70.19)]
        )
        writer.write_day(
            date(2019, 7, 15), [Candidate(date(2019, 7, 15), 29.575, 59.225, 70.19)]
        )
        writer.write_day(
            date(2020, 7, 15), [Candidate(date(2020, 7, 15), 29.575, 59.225, 68.0)]
        )

    rows = read_candidate_files(candidate_files(tmp_path, "MOD11C1"))
    assert len(rows) == 2


def test_candidate_files_can_be_narrowed_to_one_product(tmp_path):
    for product in ("MOD11C1", "MYD11C1"):
        with CandidateWriter(tmp_path, product) as writer:
            writer.write_day(
                date(2019, 7, 15), [Candidate(date(2019, 7, 15), 29.575, 59.225, 60.0)]
            )

    assert len(candidate_files(tmp_path)) == 2
    assert len(candidate_files(tmp_path, "MYD11C1")) == 1


def test_candidate_files_of_an_empty_work_dir_is_empty(tmp_path):
    assert candidate_files(tmp_path) == []


# --- Finding all-time grids ---------------------------------------------------------


def test_no_grids_in_an_empty_work_dir(tmp_path):
    from kiln_scan.store import load_alltime_grid, products_with_grids

    assert products_with_grids(tmp_path) == []
    assert load_alltime_grid(tmp_path, "MOD11C1") is None


def test_the_npz_the_scanner_writes_is_found_and_read(tmp_path):
    from kiln_scan.grid import AlltimeGrid
    from kiln_scan.store import load_alltime_grid, products_with_grids

    grid = AlltimeGrid.empty()
    grid.max_centi[0, 0] = 7019
    grid.date_int[0, 0] = 20190715
    grid.save(alltime_grid_path(tmp_path, "MOD11C1"))

    assert products_with_grids(tmp_path) == ["MOD11C1"]
    loaded = load_alltime_grid(tmp_path, "MOD11C1")
    assert int(loaded.max_centi[0, 0]) == 7019
    assert int(loaded.date_int[0, 0]) == 20190715


def test_an_exported_npy_pair_is_read_as_a_fallback(tmp_path):
    from kiln_scan.grid import AlltimeGrid
    from kiln_scan.store import alltime_npy_paths, load_alltime_grid, products_with_grids

    grid = AlltimeGrid.empty()
    grid.max_centi[5, 5] = 6300
    grid.date_int[5, 5] = 20030801
    grid.export_npy(*alltime_npy_paths(tmp_path, "MYD11C1"))

    assert products_with_grids(tmp_path) == ["MYD11C1"]
    assert int(load_alltime_grid(tmp_path, "MYD11C1").date_int[5, 5]) == 20030801


def test_half_an_exported_pair_is_not_a_grid(tmp_path):
    # A maximum with no dates cannot be turned into jobs, so it must not look
    # like a usable grid.
    from kiln_scan.grid import AlltimeGrid
    from kiln_scan.store import alltime_npy_paths, load_alltime_grid, products_with_grids

    max_path, _ = alltime_npy_paths(tmp_path, "MOD11C1")
    np.save(max_path, AlltimeGrid.empty().max_centi)

    assert products_with_grids(tmp_path) == []
    assert load_alltime_grid(tmp_path, "MOD11C1") is None


def test_both_products_are_listed_in_a_stable_order(tmp_path):
    from kiln_scan.grid import AlltimeGrid
    from kiln_scan.store import products_with_grids

    for product in ("MYD11C1", "MOD11C1"):
        AlltimeGrid.empty().save(alltime_grid_path(tmp_path, product))
    assert products_with_grids(tmp_path) == ["MOD11C1", "MYD11C1"]
