"""Tests for the Supabase payload shapes and run-status logic. No network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from kiln_ingest.science import QC_NOTE, TileMax
from kiln_ingest.supabase_io import (
    ON_CONFLICT,
    REST_BASE,
    SCHEMA,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    batched,
    build_reading_row,
    build_reading_rows,
    build_run_finish_row,
    build_run_start_row,
    resolve_run_status,
    service_headers,
)

READING_DATE = date(2026, 8, 30)

TILE = TileMax(
    tile_lat=31,
    tile_lon=-115,
    max_c=63.456789,
    max_lat=31.4837219,
    max_lon=-115.2049611,
    observed_at="2026-08-30T11:25:00.000Z",
    granule_id="MOD11_L2.A2026242.1125.061.NRT.hdf",
)


def test_rest_base_targets_the_shared_tortoise_project():
    assert REST_BASE == "https://wdvguesfxcxxatzpirvy.supabase.co/rest/v1/"
    assert SCHEMA == "kiln"


def test_headers_carry_the_key_twice_and_name_the_schema():
    headers = service_headers("service-key")
    assert headers["apikey"] == "service-key"
    assert headers["Authorization"] == "Bearer service-key"
    assert headers["Content-Profile"] == "kiln"
    assert headers["Accept-Profile"] == "kiln"


def test_on_conflict_matches_the_unique_constraint():
    assert ON_CONFLICT == "reading_date,product,tile_lat,tile_lon"


def test_reading_row_shape_matches_the_table():
    row = build_reading_row(TILE, READING_DATE, "MOD11_L2")

    assert row == {
        "reading_date": "2026-08-30",
        "satellite": "Terra",
        "product": "MOD11_L2",
        "tile_lat": 31,
        "tile_lon": -115,
        "max_c": 63.46,
        "max_lat": 31.483722,
        "max_lon": -115.204961,
        "observed_at": "2026-08-30T11:25:00.000Z",
        "granule_id": "MOD11_L2.A2026242.1125.061.NRT.hdf",
        "qc_note": QC_NOTE,
        # Written whether or not a name was resolved (decision 2026-09-02), so
        # an absent name is recorded as absent rather than left as it was.
        "place_name": None,
        "country": None,
    }


def test_a_resolved_place_rides_on_the_row():
    row = build_reading_row(
        TILE, READING_DATE, "MOD11_L2", "Baja California, Mexico", "Mexico"
    )
    assert row["place_name"] == "Baja California, Mexico"
    # Stored separately rather than split back out of the display name: the
    # leaderboard filters on the country Nominatim gave, not on a substring.
    assert row["country"] == "Mexico"


def test_a_name_without_a_country_is_allowed():
    # Nominatim sometimes omits the country at sea borders.
    row = build_reading_row(TILE, READING_DATE, "MOD11_L2", "Baja California")
    assert row["place_name"] == "Baja California"
    assert row["country"] is None


def test_reading_row_derives_the_satellite_from_the_product():
    assert build_reading_row(TILE, READING_DATE, "MYD11_L2")["satellite"] == "Aqua"


def test_max_c_fits_numeric_5_2():
    row = build_reading_row(TILE, READING_DATE, "MOD11_L2")
    scaled = f"{row['max_c']:.2f}".lstrip("-").replace(".", "")
    assert len(scaled) <= 5


def test_reading_rows_preserve_input_order():
    tiles = [TILE, replace(TILE, tile_lat=20, max_c=41.0)]
    rows = build_reading_rows(tiles, READING_DATE, "MOD11_L2")
    assert [r["tile_lat"] for r in rows] == [31, 20]


def test_run_start_row():
    assert build_run_start_row(READING_DATE, "MYD11_L2") == {
        "reading_date": "2026-08-30",
        "product": "MYD11_L2",
        "status": STATUS_RUNNING,
    }


@pytest.mark.parametrize(
    ("total", "processed", "error", "expected"),
    [
        (100, 100, None, STATUS_SUCCEEDED),
        (100, 87, None, STATUS_PARTIAL),
        (100, 0, None, STATUS_FAILED),
        (0, 0, None, STATUS_FAILED),
        (100, 100, "boom", STATUS_FAILED),
        (1, 1, None, STATUS_SUCCEEDED),
    ],
)
def test_run_status_resolution(total, processed, error, expected):
    assert resolve_run_status(total, processed, error) == expected


def test_run_finish_row_records_the_tallies():
    finished = datetime(2026, 8, 31, 9, 42, tzinfo=timezone.utc)
    row = build_run_finish_row(
        granules_total=288,
        granules_processed=286,
        tiles_written=412,
        error=None,
        finished_at=finished,
    )

    assert row == {
        "finished_at": "2026-08-31T09:42:00+00:00",
        "status": STATUS_PARTIAL,
        "granules_total": 288,
        "granules_processed": 286,
        "tiles_written": 412,
        "error": None,
    }


def test_run_finish_row_marks_failure_when_an_error_is_recorded():
    row = build_run_finish_row(10, 10, 0, error="RuntimeError: CMR unreachable")
    assert row["status"] == STATUS_FAILED
    assert row["error"] == "RuntimeError: CMR unreachable"


def test_batched_splits_large_upserts():
    rows = [{"i": i} for i in range(1250)]
    batches = list(batched(rows, size=500))
    assert [len(b) for b in batches] == [500, 500, 250]
    assert [r["i"] for b in batches for r in b] == list(range(1250))


def test_batched_is_empty_for_no_rows():
    assert list(batched([], size=500)) == []
