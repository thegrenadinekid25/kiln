"""The ingest subprocess contract: argv construction, the done-log, retry policy.

Nothing here launches the real ingest CLI. ``run_job`` takes its runner as an
argument precisely so the command can be asserted without a subprocess.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kiln_scan.backfill import (
    INGEST_PRODUCTS,
    BackfillLog,
    JobResult,
    done_log_path,
    ingest_command,
    parse_backfill_log,
    pending_jobs,
    run_job,
)
from kiln_scan.worklist import Bbox, Job

PYTHON = Path("/somewhere/ingest/.venv/bin/python")
INGEST_DIR = Path("/somewhere/ingest")

LUT = Bbox(58.7, 29.05, 59.75, 30.1)
SONORAN = Bbox(-115.0, 32.0, -113.0, 34.0)


def job(*bboxes: Bbox, day: date = date(2019, 7, 15)) -> Job:
    return Job(day=day, bboxes=bboxes or (LUT,), cells=1, cmg_max_c=70.19)


class FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class FakeRunner:
    """Records every command it was asked to run and replays canned exit codes."""

    def __init__(self, codes: list[int] | None = None) -> None:
        self.codes = list(codes or [])
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, command, cwd, check):
        self.calls.append((list(command), cwd))
        return FakeCompleted(self.codes.pop(0) if self.codes else 0)


# --- Command construction -----------------------------------------------------------


def test_command_names_the_ingest_module_date_product_and_archive():
    command = ingest_command(PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT])
    assert command[:3] == [str(PYTHON), "-m", "kiln_ingest"]
    assert "--date" in command and "2019-07-15" in command
    assert "--product" in command and "MOD11_L2" in command
    assert "--archive" in command


def test_archive_is_always_passed():
    # Every date a worklist can hold is historical, and LANCE only keeps a few
    # days, so without --archive the granules simply are not there.
    command = ingest_command(PYTHON, date(2000, 6, 1), "MYD11_L2", [LUT])
    assert "--archive" in command


def test_bbox_uses_the_equals_form():
    # A separate argv entry starting with "-" is read by argparse as a flag, so
    # any western-hemisphere box would break the command. The equals form makes
    # a box's hemisphere irrelevant to whether the command parses.
    command = ingest_command(PYTHON, date(2019, 7, 15), "MOD11_L2", [SONORAN])
    assert "--bbox=-115,32,-113,34" in command
    assert "--bbox" not in command


def test_a_western_box_survives_round_tripping_through_argv():
    command = ingest_command(PYTHON, date(2019, 7, 15), "MOD11_L2", [SONORAN])
    (arg,) = [part for part in command if part.startswith("--bbox=")]
    west, south, east, north = (float(v) for v in arg.split("=", 1)[1].split(","))
    assert (west, south, east, north) == (-115.0, 32.0, -113.0, 34.0)


def test_every_box_becomes_its_own_repeated_flag():
    command = ingest_command(PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT, SONORAN])
    boxes = [part for part in command if part.startswith("--bbox=")]
    assert len(boxes) == 2


def test_dry_run_is_passed_through_only_when_asked():
    assert "--dry-run" in ingest_command(
        PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT], dry_run=True
    )
    assert "--dry-run" not in ingest_command(
        PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT]
    )


def test_max_granules_is_passed_through_only_when_given():
    command = ingest_command(
        PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT], max_granules=2
    )
    assert command[command.index("--max-granules") + 1] == "2"
    assert "--max-granules" not in ingest_command(
        PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT]
    )


def test_a_job_with_no_boxes_is_refused():
    with pytest.raises(ValueError, match="no bounding boxes"):
        ingest_command(PYTHON, date(2019, 7, 15), "MOD11_L2", [])


def test_an_unknown_product_is_refused():
    with pytest.raises(ValueError, match="unknown ingest product"):
        ingest_command(PYTHON, date(2019, 7, 15), "MOD11C1", [LUT])


# --- Running a job ------------------------------------------------------------------


def test_a_job_runs_once_covering_both_products():
    # ONE invocation with no --product: the CLI runs both satellites in a
    # single process, which is what feeds the corroboration screen both
    # accumulators. Two per-product invocations silently degrade every
    # record-tier reading to "single-satellite, uncorroborated".
    runner = FakeRunner()
    result = run_job(job(), PYTHON, INGEST_DIR, runner=runner)

    assert len(runner.calls) == 1
    command, cwd = runner.calls[0]
    assert cwd == str(INGEST_DIR)
    assert "--product" not in command
    assert sorted(result.exit_codes) == sorted(INGEST_PRODUCTS)
    assert result.ok


def test_both_products_get_the_same_date_and_boxes():
    runner = FakeRunner()
    run_job(job(LUT, SONORAN), PYTHON, INGEST_DIR, runner=runner)

    for command, _ in runner.calls:
        assert "2019-07-15" in command
        assert len([p for p in command if p.startswith("--bbox=")]) == 2


def test_a_job_is_not_ok_when_the_invocation_fails():
    result = run_job(job(), PYTHON, INGEST_DIR, runner=FakeRunner([3]))
    assert not result.ok
    assert set(result.exit_codes.values()) == {3}


def test_a_pre_aqua_job_runs_single_product_mode():
    # Terra-only era: one invocation WITH --product MOD11_L2; the screen's
    # single-satellite marking is scientifically correct there.
    runner = FakeRunner()
    result = run_job(job(day=date(2001, 7, 15)), PYTHON, INGEST_DIR, runner=runner)
    assert len(runner.calls) == 1
    command, _ = runner.calls[0]
    assert "--product" in command and "MOD11_L2" in command
    assert list(result.exit_codes) == ["MOD11_L2"]


def test_a_runner_that_raises_is_recorded_as_a_failed_product():
    # A missing interpreter or a bad cwd must fail one job, not end the backfill.
    def explodes(command, cwd, check):
        raise FileNotFoundError("no such interpreter")

    result = run_job(job(), PYTHON, INGEST_DIR, runner=explodes)
    assert not result.ok
    assert set(result.exit_codes.values()) == {-1}


def test_dry_run_reaches_every_product_command():
    runner = FakeRunner()
    run_job(job(), PYTHON, INGEST_DIR, dry_run=True, runner=runner)
    assert all("--dry-run" in command for command, _ in runner.calls)


# --- Done-log -----------------------------------------------------------------------


def test_log_line_records_the_date_and_every_exit_code():
    result = JobResult(date(2019, 7, 15), {"MOD11_L2": 0, "MYD11_L2": 2})
    assert result.log_line() == "2019-07-15 MOD11_L2=0 MYD11_L2=2"


def test_a_fully_successful_line_marks_the_date_done():
    assert parse_backfill_log(["2019-07-15 MOD11_L2=0 MYD11_L2=0"]) == {
        date(2019, 7, 15)
    }


def test_a_line_with_any_nonzero_code_does_not_mark_the_date_done():
    # The record of the attempt stays, but a rerun must retry it.
    assert parse_backfill_log(["2019-07-15 MOD11_L2=0 MYD11_L2=3"]) == set()


def test_a_later_success_overrides_an_earlier_failure():
    assert parse_backfill_log(
        ["2019-07-15 MOD11_L2=0 MYD11_L2=3", "2019-07-15 MOD11_L2=0 MYD11_L2=0"]
    ) == {date(2019, 7, 15)}


def test_the_log_ignores_blanks_comments_and_partial_lines():
    lines = ["", "# started 2026-08-31", "2019-07-1", "not-a-date MOD11_L2=0"]
    assert parse_backfill_log(lines) == set()


def test_a_line_with_an_unparseable_code_is_not_counted_as_done():
    assert parse_backfill_log(["2019-07-15 MOD11_L2=ok"]) == set()


def test_a_date_with_no_codes_at_all_is_not_done():
    assert parse_backfill_log(["2019-07-15"]) == set()


def test_recording_a_result_makes_it_durable_immediately(tmp_path):
    path = done_log_path(tmp_path)
    log = BackfillLog.load(path)
    log.record(JobResult(date(2019, 7, 15), {"MOD11_L2": 0, "MYD11_L2": 0}))

    # A brand new reader, as if the process had died and restarted.
    assert date(2019, 7, 15) in BackfillLog.load(path)


def test_a_recorded_failure_is_written_but_not_marked_done(tmp_path):
    path = done_log_path(tmp_path)
    log = BackfillLog.load(path)
    log.record(JobResult(date(2019, 7, 15), {"MOD11_L2": 0, "MYD11_L2": 1}))

    assert "MYD11_L2=1" in path.read_text()
    assert date(2019, 7, 15) not in log


def test_the_log_appends_across_runs(tmp_path):
    path = done_log_path(tmp_path)
    BackfillLog.load(path).record(JobResult(date(2019, 7, 15), {"MOD11_L2": 0}))
    BackfillLog.load(path).record(JobResult(date(2019, 7, 16), {"MOD11_L2": 0}))

    assert BackfillLog.load(path).succeeded == {date(2019, 7, 15), date(2019, 7, 16)}


def test_a_missing_log_reads_as_nothing_done(tmp_path):
    assert len(BackfillLog.load(done_log_path(tmp_path))) == 0


def test_the_log_file_is_named_per_work_dir(tmp_path):
    assert done_log_path(tmp_path).name == "backfill_done.txt"


# --- Job selection ------------------------------------------------------------------


def test_pending_skips_jobs_already_done():
    jobs = [job(day=date(2019, 7, day)) for day in (15, 16, 17)]
    todo = pending_jobs(jobs, {date(2019, 7, 16)})
    assert [j.day for j in todo] == [date(2019, 7, 15), date(2019, 7, 17)]


def test_pending_keeps_the_worklist_order():
    # The worklist is hottest-first, so a --limit spends its budget on the days
    # most likely to hold a record.
    jobs = [job(day=date(2019, 7, day)) for day in (17, 15, 16)]
    assert [j.day for j in pending_jobs(jobs, set())] == [
        date(2019, 7, 17),
        date(2019, 7, 15),
        date(2019, 7, 16),
    ]


def test_pending_honours_the_limit_after_skipping_done_jobs():
    jobs = [job(day=date(2019, 7, day)) for day in (15, 16, 17, 18)]
    todo = pending_jobs(jobs, {date(2019, 7, 15)}, limit=2)
    assert [j.day for j in todo] == [date(2019, 7, 16), date(2019, 7, 17)]


def test_pending_is_empty_when_everything_is_done():
    jobs = [job(day=date(2019, 7, 15))]
    assert pending_jobs(jobs, {date(2019, 7, 15)}) == []


def test_a_failed_job_is_retried_on_the_next_run(tmp_path):
    # The whole retry contract: a job that failed is not in the done set, so
    # pending_jobs hands it back.
    path = done_log_path(tmp_path)
    log = BackfillLog.load(path)
    log.record(JobResult(date(2019, 7, 15), {"MOD11_L2": 0, "MYD11_L2": 1}))

    resumed = BackfillLog.load(path)
    jobs = [job(day=date(2019, 7, 15))]
    assert [j.day for j in pending_jobs(jobs, resumed.succeeded)] == [date(2019, 7, 15)]


def test_tiles_dir_is_passed_through_only_when_given():
    # Without it the ingest CLI writes its raster pyramid into its own checkout,
    # so a dry-run backfill would scribble on the daily pipeline's directory.
    command = ingest_command(
        PYTHON,
        date(2019, 7, 15),
        "MOD11_L2",
        [LUT],
        dry_run=True,
        tiles_dir=Path("/work/dry-run-tiles"),
    )
    assert command[command.index("--tiles-dir") + 1] == "/work/dry-run-tiles"
    assert "--tiles-dir" not in ingest_command(
        PYTHON, date(2019, 7, 15), "MOD11_L2", [LUT], dry_run=True
    )


def test_a_dry_run_backfill_keeps_its_tiles_out_of_the_ingest_checkout():
    runner = FakeRunner()
    run_job(
        job(),
        PYTHON,
        INGEST_DIR,
        dry_run=True,
        tiles_dir=Path("/work/dry-run-tiles"),
        runner=runner,
    )
    for command, _ in runner.calls:
        assert "/work/dry-run-tiles" in command


# --- Satellite availability ---------------------------------------------------------


def test_both_products_are_available_after_aqua_launched():
    from kiln_scan.backfill import products_for_day

    assert products_for_day(date(2019, 7, 15)) == ["MOD11_L2", "MYD11_L2"]


def test_only_terra_is_available_before_aqua_launched():
    # Aqua's first MYD11_L2 granule is 2002-07-04, verified against CMR. A large
    # share of the worklist predates it.
    from kiln_scan.backfill import products_for_day

    assert products_for_day(date(2001, 1, 13)) == ["MOD11_L2"]


def test_aqua_is_available_on_its_very_first_day():
    from kiln_scan.backfill import products_for_day

    assert products_for_day(date(2002, 7, 4)) == ["MOD11_L2", "MYD11_L2"]
    assert products_for_day(date(2002, 7, 3)) == ["MOD11_L2"]


def test_terra_is_available_on_its_very_first_day():
    from kiln_scan.backfill import products_for_day

    assert products_for_day(date(2000, 2, 24)) == ["MOD11_L2"]
    assert products_for_day(date(2000, 2, 23)) == []


def test_a_pre_aqua_job_runs_terra_only_and_succeeds():
    # Before this was handled, every pre-2002 job asked the archive for Aqua
    # data that cannot exist, was recorded as failed, and tripped the
    # failure-rate guard across the whole early record.
    runner = FakeRunner()
    result = run_job(job(day=date(2001, 1, 13)), PYTHON, INGEST_DIR, runner=runner)

    assert len(runner.calls) == 1
    assert "MOD11_L2" in runner.calls[0][0]
    assert result.exit_codes == {"MOD11_L2": 0}
    assert result.ok


def test_a_pre_aqua_job_is_marked_done_by_the_log(tmp_path):
    path = done_log_path(tmp_path)
    log = BackfillLog.load(path)
    log.record(JobResult(date(2001, 1, 13), {"MOD11_L2": 0}))

    assert date(2001, 1, 13) in BackfillLog.load(path)
