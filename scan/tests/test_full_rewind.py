"""The full daily rewind's own pieces: the day list, the job list, and the
threaded runner's bookkeeping. ``run_full_rewind`` never shells out for real --
every job is run through a fake that records calls and replays canned exit
codes, same pattern as ``test_backfill.py``'s ``FakeRunner``.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest

from kiln_scan.backfill import BackfillLog, JobResult, done_log_path
from kiln_scan.full_rewind import (
    DEFAULT_START,
    full_range_days,
    full_range_jobs,
    run_full_rewind,
)

PYTHON = Path("/somewhere/ingest/.venv/bin/python")
INGEST_DIR = Path("/somewhere/ingest")


def test_default_start_is_the_terra_record_start():
    assert DEFAULT_START == date(2000, 2, 24)


def test_full_range_days_is_inclusive_on_both_ends():
    days = full_range_days(date(2020, 1, 1), date(2020, 1, 3))
    assert days == [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]


def test_full_range_days_of_one_day_is_a_single_day():
    assert full_range_days(date(2020, 1, 1), date(2020, 1, 1)) == [date(2020, 1, 1)]


def test_full_range_days_rejects_an_end_before_the_start():
    with pytest.raises(ValueError):
        full_range_days(date(2020, 1, 2), date(2020, 1, 1))


def test_full_range_jobs_carries_no_bounding_boxes():
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 2))
    assert len(jobs) == 2
    assert all(job.bboxes == () for job in jobs)
    assert [job.day for job in jobs] == [date(2020, 1, 1), date(2020, 1, 2)]


# --- run_full_rewind ------------------------------------------------------------------


class FakeCompleted:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class ThreadSafeFakeRunner:
    """Records every command, thread-safely, and always reports success.

    Real work happens in ``subprocess.run`` inside worker threads; this stands
    in for it so the test never launches a process, but still exercises real
    concurrency (multiple threads calling this at once).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[list[str]] = []

    def __call__(self, command, cwd, check):
        with self._lock:
            self.calls.append(list(command))
        return FakeCompleted(0)


def test_every_pending_day_gets_one_call_and_is_marked_done(tmp_path, monkeypatch):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 5))
    runner = ThreadSafeFakeRunner()
    exit_code = run_full_rewind(
        jobs,
        python=PYTHON,
        ingest_dir=INGEST_DIR,
        work_dir=tmp_path,
        tiles_dir=tmp_path / "tiles",
        workers=4,
        runner=runner,
    )

    assert exit_code == 0
    assert len(runner.calls) == 5
    log = BackfillLog.load(done_log_path(tmp_path))
    assert log.succeeded == {job.day for job in jobs}


def test_a_second_run_only_touches_the_days_still_pending(tmp_path):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 3))
    runner = ThreadSafeFakeRunner()

    run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path, workers=2, runner=runner
    )
    assert len(runner.calls) == 3

    run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path, workers=2, runner=runner
    )
    assert len(runner.calls) == 3  # nothing new: every day was already done


def test_limit_caps_how_many_pending_days_run_this_time(tmp_path):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 5))
    runner = ThreadSafeFakeRunner()

    run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path,
        workers=2, limit=2, runner=runner,
    )

    assert len(runner.calls) == 2


def test_every_call_is_a_global_fetch_dry_run_with_no_bbox_flags(tmp_path):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 1))
    runner = ThreadSafeFakeRunner()

    run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path, workers=1, runner=runner
    )

    [call] = runner.calls
    assert "--bbox" not in " ".join(call)
    assert "--dry-run" in call
    assert "--archive" in call


def test_s3_direct_reaches_every_invocation_when_asked(tmp_path):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 1))
    runner = ThreadSafeFakeRunner()

    run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path,
        workers=1, s3_direct=True, runner=runner,
    )

    [call] = runner.calls
    assert "--s3-direct" in call


def test_a_high_failure_rate_exits_nonzero(tmp_path):
    jobs = full_range_jobs(date(2020, 1, 1), date(2020, 1, 5))

    class FailingRunner:
        def __call__(self, command, cwd, check):
            return FakeCompleted(1)

    exit_code = run_full_rewind(
        jobs, python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path,
        workers=2, runner=FailingRunner(),
    )

    assert exit_code == 1


def test_no_pending_days_is_a_clean_no_op(tmp_path):
    exit_code = run_full_rewind(
        [], python=PYTHON, ingest_dir=INGEST_DIR, work_dir=tmp_path, tiles_dir=tmp_path,
        workers=2, runner=lambda *a, **k: pytest.fail("should never be called"),
    )
    assert exit_code == 0
