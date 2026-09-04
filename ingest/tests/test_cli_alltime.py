"""The all-time stage as the CLI runs it. No network, no HDF, no images on disk.

The first section is the one that matters most. A merge into the archive is a
maximum, and a maximum is permanent: a fire pixel or an implausible reading
admitted once becomes an all-time record that no later day can undo. These
tests hold the line that both screens run before anything reaches the archive.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from kiln_ingest import alltime, cli, granule, raster, storage_io
from kiln_ingest.granule import GranuleReduction
from kiln_ingest.science import (
    HIGH_LATITUDE_MAX_C,
    GranuleField,
    TileMax,
    fire_exclusion_keys,
    prepare_granule,
)
from kiln_ingest.storage_io import UploadReport

TARGET = date(2026, 8, 31)
GOOD_ATTRS = {"scale_factor": 0.02, "add_offset": 0.0, "_FillValue": 0}
KELVIN_ZERO_C = 273.15


def counts(celsius: float) -> int:
    return int(round((celsius + KELVIN_ZERO_C) / 0.02))


# --- the ordering the archive depends on --------------------------------------------


def test_neither_a_fire_nor_an_outlier_can_enter_the_archive():
    """A granule carrying both kinds of bad pixel, taken the way the CLI takes it.

    Three hot pixels in one place, each of which only one screen can catch:

    * 55 C on a pixel MOD14 flagged -- plausible, so only the fire mask stops it;
    * 78 C at 60 N that MOD14 missed -- only the plausibility screen stops it;
    * 52 C, the real reading, which is what the archive must end up holding.
    """
    raw = np.array(
        [[counts(55.0), counts(78.0)], [counts(52.0), 0]], dtype=np.uint16
    )
    qc = np.zeros((2, 2), dtype=np.uint8)
    # All four pixels sit above 50 N, where the plausibility band applies.
    lat_sub = np.array([[60.20, 60.20], [60.60, 60.60]])
    lon_sub = np.array([[100.20, 100.60], [100.20, 100.60]])

    # MOD14 detected only the 55 C pixel.
    fire_keys = fire_exclusion_keys(np.array([60.20]), np.array([100.20]))

    field = prepare_granule(
        raw_lst=raw,
        lst_attrs=GOOD_ATTRS,
        qc=qc,
        lat_sub=lat_sub,
        lon_sub=lon_sub,
        fire_exclusion=fire_keys,
    )

    # Exactly what build_reducer paints into the day accumulator.
    day_raster: raster.TileStore = {}
    raster.accumulate_granule(
        day_raster, field.celsius, field.lat, field.lon, valid=field.keep
    )

    changed = alltime.merge_day(day_raster, {key: None for key in day_raster})
    hottest = max(int(state.max()) for state in changed.values())

    # 5500 would mean the fire reached the archive; 7800 the outlier.
    assert hottest == pytest.approx(5200, abs=2)
    assert hottest < HIGH_LATITUDE_MAX_C * 100


def test_the_accumulator_is_fed_from_the_masked_field_not_the_raw_one(monkeypatch):
    """build_reducer must paint from ``keep``, not from every decoded pixel."""
    # A 90 C pixel the screens rejected, and a 45 C one they kept.
    pixels = GranuleField(
        celsius=np.array([[90.0, 45.0]]),
        keep=np.array([[False, True]]),
        lat=np.array([[31.4, 31.4]]),
        lon=np.array([[-115.2, -115.2]]),
    )

    monkeypatch.setattr(
        granule,
        "granule_reduction",
        lambda *args, **kwargs: GranuleReduction(tiles={}, pixels=pixels),
    )
    monkeypatch.setattr(granule, "read_fire_granule", lambda path: (np.empty(0), np.empty(0)))

    store: raster.TileStore = {}
    reduce_granule = cli.build_reducer(None, "token", None, None, store)
    reduce_granule("granule.hdf", "MOD11_L2.A1.hdf", "2026-08-31T11:25:00Z")

    assert [int(tile.max()) for tile in store.values()] == [4500]


# --- fakes for the stage ------------------------------------------------------------


class FakeUploader:
    """The bucket as a dict, recording the order the stage writes in."""

    def __init__(self, objects=None, fail_state=False, fail_tiles=False):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.order: list[str] = []
        self.fail_state = fail_state
        self.fail_tiles = fail_tiles
        self.manifest: dict | None = None

    def read_manifest(self, object_name):
        body = self.objects.get(object_name)
        return json.loads(body) if body else None

    def download_objects(self, paths):
        return {path: self.objects.get(path) for path in paths}

    def upload_tiles(self, objects, content_type=storage_io.TILE_CONTENT_TYPE):
        is_state = content_type == storage_io.STATE_CONTENT_TYPE
        self.order.append("state" if is_state else "tiles")
        if (is_state and self.fail_state) or (not is_state and self.fail_tiles):
            return UploadReport(uploaded=0, failed=len(objects))
        for path, body in objects:
            self.objects[path] = body
        return UploadReport(uploaded=len(objects), failed=0)

    def upload_manifest(self, manifest, object_name=storage_io.MANIFEST_OBJECT):
        self.order.append("manifest")
        self.manifest = manifest
        self.objects[object_name] = json.dumps(manifest).encode("utf-8")


class FakeWriter:
    def __init__(self, order, existing=None, rows=None):
        self.order = order
        self.existing = dict(existing or {})
        self.rows = rows if rows is not None else []

    def fetch_alltime_maxima(self):
        return self.existing

    def upsert_alltime(self, rows):
        self.order.append("rows")
        self.rows.extend(rows)
        return len(rows)


class StubSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def stage(monkeypatch):
    """Install the fakes and hand back a runner plus what it wrote."""

    def install(uploader, existing=None):
        rows: list[dict] = []
        writer = FakeWriter(uploader.order, existing=existing, rows=rows)

        import requests

        monkeypatch.setattr(requests, "Session", lambda: StubSession())
        monkeypatch.setattr(storage_io, "StorageUploader", lambda *a, **k: uploader)
        monkeypatch.setattr(cli, "SupabaseWriter", lambda *a, **k: writer)
        return rows

    return install


def day_with(pixel_c: float = 61.0, tile_key=(64, 64)) -> cli.DayAccumulator:
    day = cli.DayAccumulator()
    state = raster.blank_tile()
    state[0, 0] = int(round(pixel_c * 100))
    day.raster[tile_key] = state
    day.tiles[(31, -115)] = TileMax(
        31, -115, pixel_c, 31.4, -115.2, "2026-08-31T11:25:00Z", "MOD11_L2.A1.hdf"
    )
    return day


# --- the stage ----------------------------------------------------------------------


def test_a_dry_run_uploads_nothing_and_says_what_it_would_merge(stage, capsys):
    uploader = FakeUploader()
    stage(uploader)

    assert cli.publish_alltime(day_with(), TARGET, None, dry_run=True)

    assert uploader.order == []
    assert uploader.objects == {}
    output = capsys.readouterr().out
    assert "all-time archive (dry run" in output
    assert "1 base-zoom tiles" in output


def test_the_stage_writes_tiles_then_state_then_rows_then_the_manifest(stage):
    uploader = FakeUploader()
    rows = stage(uploader)

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False)

    # State last of the two uploads: it is what marks a tile as done, so it must
    # not advance until the tiles it describes are safely up.
    assert uploader.order == ["tiles", "state", "rows", "manifest"]

    assert storage_io.alltime_state_path(64, 64) in uploader.objects
    assert storage_io.alltime_tile_path(7, 64, 64) in uploader.objects
    assert storage_io.alltime_tile_path(0, 0, 0) in uploader.objects
    assert len(rows) == 1
    assert rows[0]["record_date"] == "2026-08-31"
    assert rows[0]["product"] == "MOD11_L2"
    assert rows[0]["satellite"] == "Terra"


def test_the_stored_state_is_the_running_maximum_across_days(stage):
    yesterday = raster.blank_tile()
    yesterday[0, 0] = 7000  # hotter than today
    yesterday[1, 1] = 4200  # today never saw this pixel

    uploader = FakeUploader({
        storage_io.alltime_state_path(64, 64): alltime.dump_state(yesterday)
    })
    stage(uploader)

    assert cli.publish_alltime(day_with(pixel_c=61.0), TARGET, "service-key", dry_run=False)

    merged = alltime.load_state(uploader.objects[storage_io.alltime_state_path(64, 64)])
    assert merged[0, 0] == 7000  # yesterday's record held
    assert merged[1, 1] == 4200  # and was not forgotten


def test_a_day_that_beats_nothing_writes_no_tiles_but_still_dates_the_archive(stage):
    yesterday = raster.blank_tile()
    yesterday[0, 0] = 9000

    uploader = FakeUploader({
        storage_io.alltime_state_path(64, 64): alltime.dump_state(yesterday)
    })
    stage(uploader, existing={(31, -115): 99.0})

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False)

    # No pyramid work at all, but the rows and the manifest still run: the
    # manifest's `through` date is what tells the frontend how current it is.
    assert uploader.order == ["rows", "manifest"]
    assert uploader.manifest["through"] == "2026-08-31"


def test_a_cool_tile_can_set_a_record_the_raster_never_sees(stage):
    # The raster only holds pixels at or above the display threshold, so the
    # table has to be driven from the 1-degree maxima, not from the pyramid.
    day = cli.DayAccumulator()
    day.tiles[(31, -115)] = TileMax(
        31, -115, 22.0, 31.4, -115.2, "2026-08-31T11:25:00Z", "MOD11_L2.A1.hdf"
    )

    uploader = FakeUploader()
    rows = stage(uploader)

    assert cli.publish_alltime(day, TARGET, "service-key", dry_run=False)
    assert uploader.order == ["rows", "manifest"]
    assert [row["max_c"] for row in rows] == [22.0]


def test_the_start_of_the_record_is_carried_across_runs(stage):
    prior = {
        "since": "2026-08-20",
        "through": "2026-08-30",
        "tile_count": 700,
        "min_zoom": 0,
        "max_zoom": 7,
        "tile_url_template": "alltime/{z}/{x}/{y}.png",
        "generated_at": "2026-08-30T09:00:00+00:00",
    }
    uploader = FakeUploader({
        storage_io.ALLTIME_MANIFEST_OBJECT: json.dumps(prior).encode("utf-8")
    })
    stage(uploader)

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False)

    assert uploader.manifest["since"] == "2026-08-20"
    assert uploader.manifest["through"] == "2026-08-31"
    # Eight new tiles, one per zoom, on top of the archive's running total.
    assert uploader.manifest["tile_count"] == 708


def test_a_lost_state_object_fails_the_stage_rather_than_the_record(stage):
    # No tolerance here, unlike display tiles: a state object that does not land
    # loses that tile's improvement for good.
    uploader = FakeUploader(fail_state=True)
    stage(uploader)

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False) is False
    assert "manifest" not in uploader.order


def test_failing_tiles_stop_the_stage_before_the_state_advances(stage):
    uploader = FakeUploader(fail_tiles=True)
    stage(uploader)

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False) is False
    # State untouched, so re-running the date does the whole job again.
    assert uploader.order == ["tiles"]
    assert storage_io.alltime_state_path(64, 64) not in uploader.objects


def test_an_unreadable_state_object_is_warned_about_not_swallowed(stage, caplog):
    import logging

    uploader = FakeUploader({storage_io.alltime_state_path(64, 64): b"not a numpy file"})
    stage(uploader)

    with caplog.at_level(logging.WARNING):
        assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False)

    assert "unreadable" in caplog.text
    assert "alltime-state/64/64.npy" in caplog.text
    # The tile is rebuilt from today rather than left broken.
    merged = alltime.load_state(uploader.objects[storage_io.alltime_state_path(64, 64)])
    assert merged[0, 0] == 6100


def test_a_reading_from_an_unrecognisable_granule_gets_no_row(stage, caplog):
    import logging

    day = day_with()
    day.tiles[(31, -115)] = TileMax(
        31, -115, 61.0, 31.4, -115.2, "2026-08-31T11:25:00Z", "mystery-file.hdf"
    )

    uploader = FakeUploader()
    rows = stage(uploader)

    with caplog.at_level(logging.WARNING):
        assert cli.publish_alltime(day, TARGET, "service-key", dry_run=False)

    assert rows == []
    assert "cannot tell which product" in caplog.text


def test_a_failure_inside_the_stage_is_reported_not_raised(stage, monkeypatch):
    uploader = FakeUploader()
    stage(uploader)
    monkeypatch.setattr(
        alltime, "merge_day", lambda *a, **k: (_ for _ in ()).throw(MemoryError("no room"))
    )

    assert cli.publish_alltime(day_with(), TARGET, "service-key", dry_run=False) is False


def test_an_empty_day_leaves_the_archive_alone(stage):
    uploader = FakeUploader()
    stage(uploader)
    assert cli.publish_alltime(cli.DayAccumulator(), TARGET, "service-key", dry_run=False)
    assert uploader.order == []
