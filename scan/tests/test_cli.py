"""Argument handling, the scan window, and an end-to-end scan with no network."""

from __future__ import annotations

import argparse
import json
from datetime import date

import numpy as np
import pytest

from kiln_scan import cli
from kiln_scan.grid import AlltimeGrid, CMG_SHAPE
from kiln_scan.store import (
    CandidateRow,
    DoneLog,
    alltime_grid_path,
    candidate_files,
    done_log_path,
    read_candidate_files,
)


# --- Argument parsing ---------------------------------------------------------------


def test_year_range_accepts_a_span():
    assert cli.year_range("2000-2026") == (2000, 2026)


def test_year_range_accepts_a_single_year():
    assert cli.year_range("2019") == (2019, 2019)


def test_year_range_refuses_a_backwards_span():
    with pytest.raises(argparse.ArgumentTypeError, match="ends before"):
        cli.year_range("2026-2000")


def test_year_range_refuses_nonsense():
    with pytest.raises(argparse.ArgumentTypeError):
        cli.year_range("last-tuesday")


def test_scan_is_the_default_subcommand():
    # The documented invocation names no subcommand.
    assert cli.normalise_argv(["--product", "MOD11C1"]) == [
        "scan",
        "--product",
        "MOD11C1",
    ]


def test_naming_a_subcommand_keeps_it():
    assert cli.normalise_argv(["summarize", "--work-dir", "w"])[0] == "summarize"
    assert cli.normalise_argv(["scan", "--product", "MOD11C1"])[0] == "scan"


def test_help_is_left_for_the_top_level_parser():
    assert cli.normalise_argv(["--help"]) == ["--help"]


def test_the_documented_invocation_parses():
    args = cli.build_parser().parse_args(
        cli.normalise_argv(
            [
                "--product",
                "MOD11C1",
                "--years",
                "2000-2026",
                "--days",
                "5",
                "--bar",
                "60.0",
                "--work-dir",
                "/tmp/work",
            ]
        )
    )
    assert args.command == "scan"
    assert args.product == "MOD11C1"
    assert args.years == (2000, 2026)
    assert args.days == 5
    assert args.bar == 60.0


def test_the_bar_defaults_to_55():
    args = cli.build_parser().parse_args(
        cli.normalise_argv(["--product", "MOD11C1", "--work-dir", "w"])
    )
    assert args.bar == pytest.approx(55.0)


# --- Scan window --------------------------------------------------------------------


def test_window_starts_at_the_instrument_s_first_day_not_january():
    start, end = cli.resolve_scan_window("MOD11C1", (2000, 2000), date(2026, 8, 31))
    assert start == date(2000, 2, 24)
    assert end == date(2000, 12, 31)


def test_window_for_aqua_starts_in_july_2002():
    start, _ = cli.resolve_scan_window("MYD11C1", (2000, 2003), date(2026, 8, 31))
    assert start == date(2002, 7, 4)


def test_window_does_not_run_past_today():
    _, end = cli.resolve_scan_window("MOD11C1", (2026, 2026), date(2026, 8, 31))
    assert end == date(2026, 8, 31)


def test_window_with_no_years_covers_the_whole_record():
    start, end = cli.resolve_scan_window("MOD11C1", None, date(2026, 8, 31))
    assert start == date(2000, 2, 24)
    assert end == date(2026, 8, 31)


def test_a_window_entirely_before_the_record_comes_back_empty():
    start, end = cli.resolve_scan_window("MYD11C1", (1999, 2000), date(2026, 8, 31))
    assert end < start


def test_explicit_day_bounds_override_the_year_range():
    start, end = cli.resolve_scan_window(
        "MOD11C1",
        (2000, 2026),
        date(2026, 8, 31),
        start_override=date(2019, 7, 15),
        end_override=date(2019, 7, 15),
    )
    assert start == end == date(2019, 7, 15)


def test_explicit_day_bounds_are_still_clamped_to_the_record():
    start, end = cli.resolve_scan_window(
        "MYD11C1",
        None,
        date(2026, 8, 31),
        start_override=date(1999, 1, 1),
        end_override=date(2030, 1, 1),
    )
    assert start == date(2002, 7, 4)
    assert end == date(2026, 8, 31)


def test_iso_date_refuses_a_non_date():
    with pytest.raises(argparse.ArgumentTypeError, match="YYYY-MM-DD"):
        cli.iso_date("2019-07")


def test_a_single_day_can_be_scanned_from_the_command_line():
    args = cli.build_parser().parse_args(
        cli.normalise_argv(
            [
                "--product",
                "MOD11C1",
                "--start",
                "2019-07-15",
                "--end",
                "2019-07-15",
                "--work-dir",
                "w",
            ]
        )
    )
    assert args.start == args.end == date(2019, 7, 15)


# --- End-to-end scan, no network ----------------------------------------------------


def counts_for(celsius: float) -> int:
    return int(round((celsius + 273.15) / 0.02))


LST_ATTRS = {
    "valid_range": [7500, 65535],
    "_FillValue": 0,
    "scale_factor": 0.02,
    "add_offset": 0.0,
}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """CMR answers for a fixed set of days; every other day has no granule."""

    def __init__(self, available: set[date]) -> None:
        self.available = available
        self.days_asked: list[str] = []

    def get(self, url: str, params: dict, timeout: int) -> FakeResponse:
        day = date.fromisoformat(params["temporal"][:10])
        self.days_asked.append(day.isoformat())
        if day not in self.available:
            return FakeResponse({"feed": {"entry": []}})
        granule = f"{params['short_name']}.A{day:%Y%j}.061.fake"
        return FakeResponse(
            {
                "feed": {
                    "entry": [
                        {
                            "producer_granule_id": granule,
                            "time_start": f"{day.isoformat()}T00:00:00.000Z",
                            "links": [
                                {
                                    "rel": (
                                        "http://esipfed.org/ns/fedsearch/1.1/data#"
                                    ),
                                    "href": f"https://data.lpdaac.earthdatacloud.nasa.gov/{granule}.hdf",
                                }
                            ],
                        }
                    ]
                }
            }
        )


@pytest.fixture
def offline_day(monkeypatch):
    """Replace only the network: the download and the HDF decode.

    Everything downstream -- scaling, QC, the plausibility screen, candidate
    selection, the all-time fold -- runs for real against a synthetic granule of
    the true CMG size. The granule has one hot cell in the Lut Desert whose
    temperature rises a degree per day, so the grid's date attribution has
    something to be right or wrong about, plus a cloudy cell and an implausible
    Siberian one that both have to be thrown away.
    """

    def fake_download(session, url, destination, token, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"stand-in for a 46 MB HDF4 file")
        return destination

    monkeypatch.setattr(cli, "download_granule", fake_download)

    class FakeArrays:
        def __init__(self, raw_lst, qc):
            self.raw_lst = raw_lst
            self.lst_attrs = LST_ATTRS
            self.qc = qc
            self.raw_view_time = None

    day_index = {"n": 0}

    def fake_read_day(path):
        raw = np.zeros(CMG_SHAPE, dtype=np.uint16)
        qc = np.full(CMG_SHAPE, 0b00000011, dtype=np.uint8)  # not produced

        raw[1208, 4784] = counts_for(60.0 + day_index["n"])  # Lut Desert, good
        qc[1208, 4784] = 0b00001000
        raw[1208, 4785] = counts_for(50.0)  # good but below any bar we test
        qc[1208, 4785] = 0b00000000
        raw[1208, 4786] = counts_for(75.0)  # hot but cloudy
        qc[1208, 4786] = 0b00000010
        raw[500, 100] = counts_for(78.75)  # hot at 64.975 N, implausible
        qc[500, 100] = 0b00000000

        day_index["n"] += 1
        return FakeArrays(raw, qc)

    import kiln_scan.hdf as hdf_module

    monkeypatch.setattr(hdf_module, "read_day", fake_read_day)


def scan_args(work_dir, **overrides) -> argparse.Namespace:
    defaults = dict(
        command="scan",
        product="MOD11C1",
        years=(2019, 2019),
        start=None,
        end=None,
        days=None,
        bar=55.0,
        work_dir=str(work_dir),
        max_error_class=1,
        progress_every=30,
        flush_every=1,
        keep_granules=False,
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def candidates_text(work_dir) -> str:
    return "".join(path.read_text() for path in candidate_files(work_dir, "MOD11C1"))


def days_args(work_dir, start: date, end: date, **overrides) -> argparse.Namespace:
    """Scan arguments pinned to an explicit day range."""
    return scan_args(work_dir, start=start, end=end, **overrides)


def test_scan_writes_candidates_grid_and_done_log(tmp_path, offline_day):
    session = FakeSession({date(2019, 7, 15), date(2019, 7, 16)})
    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 16))

    assert cli.run_scan(args, session, "fake-token") == 0

    assert DoneLog.load(done_log_path(tmp_path, "MOD11C1")).days == {
        date(2019, 7, 15),
        date(2019, 7, 16),
    }

    rows = read_candidate_files(candidate_files(tmp_path, "MOD11C1"))
    assert sorted(round(row.max_c) for row in rows) == [60, 61]

    grid = AlltimeGrid.load(alltime_grid_path(tmp_path, "MOD11C1"))
    celsius, lat, lon, day = grid.global_max()
    assert celsius == pytest.approx(61.0, abs=0.01)
    assert lat == pytest.approx(29.575)
    assert lon == pytest.approx(59.225)
    assert day == date(2019, 7, 16)


def test_scan_drops_the_cloudy_and_implausible_cells(tmp_path, offline_day):
    # The synthetic day's two hottest cells are a 75 C cloudy one and a 78.75 C
    # Siberian one. Neither may reach the candidates or the grid.
    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 15))
    cli.run_scan(args, FakeSession({date(2019, 7, 15)}), "fake-token")

    rows = read_candidate_files(candidate_files(tmp_path, "MOD11C1"))
    assert [round(row.max_c) for row in rows] == [60]

    grid = AlltimeGrid.load(alltime_grid_path(tmp_path, "MOD11C1"))
    assert grid.global_max()[0] == pytest.approx(60.0, abs=0.01)


def test_scan_deletes_each_granule_after_reading_it(tmp_path, offline_day):
    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 16))
    cli.run_scan(args, FakeSession({date(2019, 7, 15), date(2019, 7, 16)}), "t")
    assert list((tmp_path / "granules").glob("*")) == []


def test_keep_granules_leaves_the_file_on_disk(tmp_path, offline_day):
    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 15), keep_granules=True)
    cli.run_scan(args, FakeSession({date(2019, 7, 15)}), "t")
    assert len(list((tmp_path / "granules").glob("*.hdf"))) == 1


def test_scan_records_a_day_with_no_granule_as_done(tmp_path, offline_day):
    # 2000-02-29 has no MOD11C1 file. Marking it done is what stops every rerun
    # from asking CMR about it again for the next twenty years.
    args = days_args(tmp_path, date(2000, 2, 29), date(2000, 2, 29))
    assert cli.run_scan(args, FakeSession(set()), "t") == 0

    assert date(2000, 2, 29) in DoneLog.load(done_log_path(tmp_path, "MOD11C1"))
    assert candidate_files(tmp_path, "MOD11C1") == []


def test_a_day_that_raises_is_skipped_not_fatal(tmp_path, offline_day, monkeypatch):
    import kiln_scan.hdf as hdf_module

    good = hdf_module.read_day

    def sometimes_broken(path):
        if "2019196" in str(path):
            raise OSError("HDF4 read failed: file is truncated")
        return good(path)

    monkeypatch.setattr(hdf_module, "read_day", sometimes_broken)

    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 16))
    session = FakeSession({date(2019, 7, 15), date(2019, 7, 16)})
    assert cli.run_scan(args, session, "t") == 0

    # The bad day is not marked done, so a rerun will try it again; the good one
    # went through.
    assert DoneLog.load(done_log_path(tmp_path, "MOD11C1")).days == {date(2019, 7, 16)}


def test_the_day_cap_stops_early_and_leaves_the_rest_pending(tmp_path, offline_day):
    args = days_args(tmp_path, date(2019, 7, 1), date(2019, 7, 31), days=3)
    session = FakeSession(set(cli_days(date(2019, 7, 1), date(2019, 7, 31))))

    cli.run_scan(args, session, "t")

    assert len(DoneLog.load(done_log_path(tmp_path, "MOD11C1"))) == 3
    assert session.days_asked == ["2019-07-01", "2019-07-02", "2019-07-03"]


def test_rerunning_a_completed_scan_changes_nothing(tmp_path, offline_day):
    args = days_args(tmp_path, date(2019, 7, 15), date(2019, 7, 15))
    session = FakeSession({date(2019, 7, 15)})

    cli.run_scan(args, session, "t")
    first = candidates_text(tmp_path)
    asked_once = len(session.days_asked)

    cli.run_scan(args, session, "t")

    assert candidates_text(tmp_path) == first
    assert len(session.days_asked) == asked_once  # CMR was not queried again


def test_an_interrupted_scan_resumes_where_it_stopped(tmp_path, offline_day):
    days = set(cli_days(date(2019, 7, 1), date(2019, 7, 4)))

    cli.run_scan(
        days_args(tmp_path, date(2019, 7, 1), date(2019, 7, 4), days=2),
        FakeSession(days),
        "t",
    )
    second = FakeSession(days)
    cli.run_scan(days_args(tmp_path, date(2019, 7, 1), date(2019, 7, 4)), second, "t")

    assert second.days_asked == ["2019-07-03", "2019-07-04"]
    assert len(DoneLog.load(done_log_path(tmp_path, "MOD11C1"))) == 4
    rows = read_candidate_files(candidate_files(tmp_path, "MOD11C1"))
    assert len(rows) == 4  # one per day, no duplicates


def cli_days(start: date, end: date) -> list[date]:
    from kiln_scan.store import days_in_range

    return list(days_in_range(start, end))


# --- Summarize ----------------------------------------------------------------------


def test_summary_reports_the_grid_and_the_top_candidates():
    grid = AlltimeGrid.empty()
    celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
    keep = np.zeros(CMG_SHAPE, dtype=bool)
    celsius[1208, 4784] = 70.19
    keep[1208, 4784] = True
    grid.fold_day(celsius, keep, date(2019, 7, 15))

    rows = [
        CandidateRow(date(2019, 7, 15), 29.575, 59.225, 70.19),
        CandidateRow(date(2003, 8, 1), 31.0, 8.0, 66.0),
    ]
    summary = cli.build_summary("MOD11C1", grid, rows, bar_c=55.0, top_n=1, days_done=2)

    assert summary["candidate_rows"] == 2
    assert summary["candidate_days"] == 2
    assert [entry["max_c"] for entry in summary["top"]] == [70.19]
    assert summary["alltime_grid"]["cells_at_or_above_bar"] == 1
    assert summary["alltime_grid"]["global_max"]["date"] == "2019-07-15"
    assert summary["alltime_grid"]["global_max"]["cell_lat"] == pytest.approx(29.575)


def test_summary_without_a_grid_says_so():
    summary = cli.build_summary(None, None, [], bar_c=55.0, top_n=5, days_done=0)
    assert summary["alltime_grid"] is None
    assert summary["top"] == []


def test_summary_is_json_serialisable():
    summary = cli.build_summary(
        "MOD11C1",
        AlltimeGrid.empty(),
        [CandidateRow(date(2019, 7, 15), 29.575, 59.225, 70.19)],
        bar_c=55.0,
        top_n=5,
        days_done=1,
    )
    assert json.loads(json.dumps(summary))["product"] == "MOD11C1"


# --- Worklist -----------------------------------------------------------------------


def worklist_args(work_dir, out, **overrides) -> argparse.Namespace:
    defaults = dict(
        command="worklist",
        work_dir=str(work_dir),
        product=None,
        bar=60.0,
        merge_degrees=3.0,
        pad_degrees=0.5,
        top=10,
        out=str(out),
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def seeded_grid(work_dir, product="MOD11C1"):
    """A saved all-time grid with two hot cells on two different days."""
    from kiln_scan.store import alltime_grid_path as grid_path

    grid = AlltimeGrid.empty()
    for cell, value, day in (
        ((1208, 4784), 70.19, date(2019, 7, 15)),
        ((1500, 3800), 63.0, date(2003, 8, 1)),
    ):
        celsius = np.zeros(CMG_SHAPE, dtype=np.float64)
        keep = np.zeros(CMG_SHAPE, dtype=bool)
        celsius[cell] = value
        keep[cell] = True
        grid.fold_day(celsius, keep, day)
    grid.save(grid_path(work_dir, product))
    return grid


def test_worklist_writes_a_jobs_file_hottest_first(tmp_path, capsys):
    seeded_grid(tmp_path)
    out = tmp_path / "jobs.json"

    assert cli.run_worklist(worklist_args(tmp_path, out)) == 0

    payload = json.loads(out.read_text())
    assert payload["bar_c"] == 60.0
    assert payload["source_products"] == ["MOD11C1"]
    assert [job["date"] for job in payload["jobs"]] == ["2019-07-15", "2003-08-01"]
    assert payload["jobs"][0]["cmg_max_c"] == pytest.approx(70.19)


def test_worklist_boxes_are_valid_for_the_ingest_cli(tmp_path):
    seeded_grid(tmp_path)
    out = tmp_path / "jobs.json"
    cli.run_worklist(worklist_args(tmp_path, out))

    for job in json.loads(out.read_text())["jobs"]:
        for west, south, east, north in job["bboxes"]:
            assert -180.0 <= west < east <= 180.0
            assert -90.0 <= south < north <= 90.0


def test_worklist_prints_a_summary(tmp_path, capsys):
    seeded_grid(tmp_path)
    cli.run_worklist(worklist_args(tmp_path, tmp_path / "jobs.json"))

    printed = capsys.readouterr().out
    assert "jobs (unique dates): 2" in printed
    assert "expected downloads" in printed
    assert "2019-07-15" in printed


def test_worklist_bar_filters_cells(tmp_path):
    seeded_grid(tmp_path)
    out = tmp_path / "jobs.json"
    cli.run_worklist(worklist_args(tmp_path, out, bar=65.0))

    assert [job["date"] for job in json.loads(out.read_text())["jobs"]] == [
        "2019-07-15"
    ]


def test_worklist_reads_every_product_the_work_dir_has(tmp_path):
    seeded_grid(tmp_path, "MOD11C1")
    seeded_grid(tmp_path, "MYD11C1")
    out = tmp_path / "jobs.json"
    cli.run_worklist(worklist_args(tmp_path, out))

    assert json.loads(out.read_text())["source_products"] == ["MOD11C1", "MYD11C1"]


def test_worklist_can_be_narrowed_to_one_product(tmp_path):
    seeded_grid(tmp_path, "MOD11C1")
    seeded_grid(tmp_path, "MYD11C1")
    out = tmp_path / "jobs.json"
    cli.run_worklist(worklist_args(tmp_path, out, product="MYD11C1"))

    assert json.loads(out.read_text())["source_products"] == ["MYD11C1"]


def test_worklist_without_a_grid_fails_rather_than_writing_an_empty_file(tmp_path):
    out = tmp_path / "jobs.json"
    assert cli.run_worklist(worklist_args(tmp_path, out)) == 1
    assert not out.exists()


# --- Backfill -----------------------------------------------------------------------


def backfill_args(jobs_path, work_dir, python, **overrides) -> argparse.Namespace:
    defaults = dict(
        command="backfill",
        jobs=str(jobs_path),
        work_dir=str(work_dir),
        limit=None,
        dry_run=False,
        max_granules=None,
        ingest_dir=str(work_dir),
        ingest_python=str(python),
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def write_jobs(path, days) -> None:
    path.write_text(
        json.dumps(
            {
                "bar_c": 60.0,
                "jobs": [
                    {
                        "date": day.isoformat(),
                        "bboxes": [[58.7, 29.05, 59.75, 30.1]],
                        "cells": 1,
                        "cmg_max_c": 70.0,
                    }
                    for day in days
                ],
            }
        )
    )


@pytest.fixture
def fake_ingest(tmp_path, monkeypatch):
    """Stand in for run_job so no subprocess is launched, recording each call."""
    from kiln_scan.backfill import JobResult

    python = tmp_path / "fake-python"
    python.write_text("#!/bin/sh\n")
    calls: list[date] = []
    codes: dict[date, int] = {}

    def fake_run_job(job, python, ingest_dir, dry_run=False, max_granules=None, **kw):
        calls.append(job.day)
        code = codes.get(job.day, 0)
        return JobResult(job.day, {"MOD11_L2": code, "MYD11_L2": code})

    monkeypatch.setattr(cli, "run_job", fake_run_job)
    return python, calls, codes


def test_backfill_runs_every_job_and_records_them(tmp_path, fake_ingest):
    python, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [date(2019, 7, 15), date(2019, 7, 16)])

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 0

    assert calls == [date(2019, 7, 15), date(2019, 7, 16)]
    assert "MOD11_L2=0" in (tmp_path / "backfill_done.txt").read_text()


def test_backfill_skips_jobs_already_done(tmp_path, fake_ingest):
    python, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [date(2019, 7, 15), date(2019, 7, 16)])

    cli.run_backfill(backfill_args(jobs_path, tmp_path, python))
    calls.clear()
    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 0

    assert calls == []


def test_backfill_retries_a_job_that_failed(tmp_path, fake_ingest):
    python, calls, codes = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [date(2019, 7, 15)])

    codes[date(2019, 7, 15)] = 2
    cli.run_backfill(backfill_args(jobs_path, tmp_path, python))
    calls.clear()

    codes.clear()
    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 0
    assert calls == [date(2019, 7, 15)]


def test_backfill_honours_the_limit(tmp_path, fake_ingest):
    python, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [date(2019, 7, day) for day in (15, 16, 17)])

    cli.run_backfill(backfill_args(jobs_path, tmp_path, python, limit=1))
    assert calls == [date(2019, 7, 15)]


def test_a_few_failures_still_exit_zero(tmp_path, fake_ingest):
    # One in five is at the limit, not over it: a couple of bad days is what a
    # long backfill looks like.
    python, _, codes = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    days = [date(2019, 7, day) for day in (15, 16, 17, 18, 19)]
    write_jobs(jobs_path, days)
    codes[days[0]] = 1

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 0


def test_too_many_failures_exit_nonzero(tmp_path, fake_ingest):
    # Half the jobs failing is a broken configuration, not a few bad days.
    python, _, codes = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    days = [date(2019, 7, 15), date(2019, 7, 16)]
    write_jobs(jobs_path, days)
    codes[days[0]] = 1

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 1


def test_backfill_refuses_a_missing_ingest_interpreter(tmp_path, fake_ingest):
    _, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [date(2019, 7, 15)])

    args = backfill_args(jobs_path, tmp_path, tmp_path / "not-here")
    assert cli.run_backfill(args) == 1
    assert calls == []


def test_backfill_refuses_an_unreadable_jobs_file(tmp_path, fake_ingest):
    python, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text("{not json")

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 1
    assert calls == []


def test_backfill_refuses_a_jobs_file_with_a_bad_box(tmp_path, fake_ingest):
    # The guard that stops a clustering bug from reaching the archive.
    python, calls, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps({"jobs": [{"date": "2019-07-15", "bboxes": [[0, 0, 200, 1]]}]})
    )

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 1
    assert calls == []


def test_backfill_with_nothing_to_do_exits_zero(tmp_path, fake_ingest):
    python, _, _ = fake_ingest
    jobs_path = tmp_path / "jobs.json"
    write_jobs(jobs_path, [])

    assert cli.run_backfill(backfill_args(jobs_path, tmp_path, python)) == 0


def test_worklist_and_backfill_are_real_subcommands():
    assert cli.normalise_argv(["worklist", "--work-dir", "w"])[0] == "worklist"
    assert cli.normalise_argv(["backfill", "--jobs", "j"])[0] == "backfill"
