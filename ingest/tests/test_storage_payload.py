"""Storage payload and transport tests against a fake bucket. No network."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kiln_ingest.storage_io import (
    ALLTIME_MANIFEST_OBJECT,
    ALLTIME_STATE_PREFIX,
    ALLTIME_TILES_PREFIX,
    MANIFEST_OBJECT,
    MAX_TILE_FAILURE_RATE,
    STATE_CONTENT_TYPE,
    TILE_CACHE_CONTROL,
    alltime_state_path,
    alltime_tile_path,
    build_alltime_manifest,
    is_date_prefix,
    TILE_URL_TEMPLATE,
    TILES_BUCKET,
    StorageUploader,
    StorageWriteError,
    build_manifest,
    prunable_date_prefixes,
    storage_headers,
    tile_object_path,
)

TARGET = date(2026, 8, 30)


class StubResponse:
    def __init__(self, payload=None, status_code=200, text="", content=b""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.content = content

    def json(self):
        return self._payload


class FakeBucket:
    """Just enough of the Storage REST API to exercise the uploader."""

    def __init__(self, keys=(), fail_paths=()):
        self.keys = set(keys)
        self.fail_paths = set(fail_paths)
        self.uploaded: dict[str, bytes] = {}
        self.upload_headers: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.list_calls: list[str] = []
        self.attempts: dict[str, int] = {}

    def _children(self, prefix: str):
        base = f"{prefix}/" if prefix else ""
        objects, folders = [], set()
        for key in sorted(self.keys):
            if not key.startswith(base):
                continue
            rest = key[len(base) :]
            if "/" in rest:
                folders.add(rest.split("/", 1)[0])
            else:
                objects.append({"name": rest, "id": f"id-{key}"})
        return [{"name": name, "id": None} for name in sorted(folders)] + objects

    def request(self, method, url, headers=None, timeout=None, json=None, data=None):
        if method == "GET":
            path = url.split(f"/object/{TILES_BUCKET}/", 1)[1]
            if path not in self.uploaded:
                # What Supabase Storage actually answers: 400 carrying a 404.
                return StubResponse(status_code=400, text='{"error":"not_found"}')
            return StubResponse(content=self.uploaded[path])

        if method == "POST" and f"/object/list/{TILES_BUCKET}" in url:
            prefix = json["prefix"]
            self.list_calls.append(prefix)
            page = self._children(prefix)
            return StubResponse(payload=page[json["offset"] : json["offset"] + json["limit"]])

        if method == "POST":
            path = url.split(f"/object/{TILES_BUCKET}/", 1)[1]
            self.attempts[path] = self.attempts.get(path, 0) + 1
            if path in self.fail_paths:
                return StubResponse(status_code=500, text="storage is having a moment")
            self.uploaded[path] = data
            self.upload_headers[path] = dict(headers or {})
            self.keys.add(path)
            return StubResponse(payload={"Key": path})

        if method == "DELETE":
            for path in json["prefixes"]:
                self.deleted.append(path)
                self.keys.discard(path)
            return StubResponse(payload=[])

        raise AssertionError(f"unexpected {method} {url}")


def uploader_for(bucket: FakeBucket, **kwargs) -> StorageUploader:
    kwargs.setdefault("sleep", lambda _s: None)
    return StorageUploader(lambda: bucket, "service-key", **kwargs)


# --- payload shapes -----------------------------------------------------------------


def test_the_manifest_has_exactly_the_keys_the_frontend_reads():
    manifest = build_manifest(
        TARGET, tile_count=1234, generated_at=datetime(2026, 8, 31, 9, 5, tzinfo=timezone.utc)
    )
    assert list(manifest) == [
        "date",
        "generated_at",
        "min_zoom",
        "max_zoom",
        "tile_url_template",
        "tile_count",
    ]
    assert manifest == {
        "date": "2026-08-30",
        "generated_at": "2026-08-31T09:05:00+00:00",
        "min_zoom": 0,
        "max_zoom": 7,
        "tile_url_template": "{date}/{z}/{x}/{y}.png",
        "tile_count": 1234,
    }


def test_the_manifest_timestamp_is_utc_whatever_clock_it_came_from():
    generated = datetime(2026, 8, 31, 2, 5, tzinfo=timezone(timedelta(hours=-7)))
    manifest = build_manifest(TARGET, tile_count=0, generated_at=generated)
    assert manifest["generated_at"] == "2026-08-31T09:05:00+00:00"


def test_tile_paths_expand_the_manifest_template():
    path = tile_object_path(TARGET, 7, 64, 63)
    assert path == "2026-08-30/7/64/63.png"
    assert path == TILE_URL_TEMPLATE.format(date="2026-08-30", z=7, x=64, y=63)


def test_headers_authenticate_and_overwrite():
    headers = storage_headers("service-key", "image/png")
    assert headers["Authorization"] == "Bearer service-key"
    assert headers["x-upsert"] == "true"
    assert headers["Content-Type"] == "image/png"


def test_an_empty_service_key_is_refused_up_front():
    with pytest.raises(StorageWriteError, match="SUPABASE_SERVICE_KEY"):
        StorageUploader(FakeBucket, "")


# --- prune policy -------------------------------------------------------------------


def test_pruning_keeps_the_two_most_recent_dates():
    names = ["2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"]
    assert prunable_date_prefixes(names) == ["2026-08-27", "2026-08-28"]


def test_pruning_never_touches_the_manifest_or_anything_undated():
    names = ["manifest.json", "2026-08-30", "scratch"]
    assert prunable_date_prefixes(names, keep=1) == []


def test_pruning_is_a_no_op_below_the_retention_count():
    assert prunable_date_prefixes(["2026-08-30"], keep=2) == []


def test_pruning_can_never_touch_the_all_time_archive():
    # The state arrays are the only record of every day the pipeline has
    # processed. Deleting one silently lowers an all-time maximum, so the
    # permanent prefixes must survive even the most aggressive retention.
    names = [
        ALLTIME_STATE_PREFIX,
        ALLTIME_TILES_PREFIX,
        ALLTIME_MANIFEST_OBJECT,
        MANIFEST_OBJECT,
        "2026-08-27",
        "2026-08-28",
        "2026-08-29",
        "2026-08-30",
    ]
    for keep in (0, 1, 2, 5):
        stale = prunable_date_prefixes(names, keep=keep)
        assert ALLTIME_STATE_PREFIX not in stale
        assert ALLTIME_TILES_PREFIX not in stale
        assert ALLTIME_MANIFEST_OBJECT not in stale
        assert all(is_date_prefix(name) for name in stale)


def test_pruning_never_touches_a_bucket_holding_the_archive():
    bucket = bucket_with_three_days()
    bucket.keys.update({
        f"{ALLTIME_STATE_PREFIX}/64/63.npy",
        f"{ALLTIME_TILES_PREFIX}/7/64/63.png",
        ALLTIME_MANIFEST_OBJECT,
    })
    before = set(bucket.keys)

    deleted = uploader_for(bucket).prune_old_dates(keep=1)

    assert deleted == 0
    assert bucket.deleted == []
    assert bucket.keys == before


# --- the all-time manifest ----------------------------------------------------------


def test_the_alltime_manifest_has_exactly_the_keys_the_frontend_reads():
    manifest = build_alltime_manifest(
        since="2026-08-30",
        through=TARGET,
        tile_count=717,
        generated_at=datetime(2026, 8, 31, 9, 5, tzinfo=timezone.utc),
    )
    assert list(manifest) == [
        "since",
        "through",
        "generated_at",
        "min_zoom",
        "max_zoom",
        "tile_url_template",
        "tile_count",
    ]
    assert manifest == {
        "since": "2026-08-30",
        "through": "2026-08-30",
        "generated_at": "2026-08-31T09:05:00+00:00",
        "min_zoom": 0,
        "max_zoom": 7,
        "tile_url_template": "alltime/{z}/{x}/{y}.png",
        "tile_count": 717,
    }


def test_alltime_paths_expand_the_alltime_template():
    template = build_alltime_manifest("2026-08-30", TARGET, 0)["tile_url_template"]
    assert alltime_tile_path(7, 64, 63) == "alltime/7/64/63.png"
    assert alltime_tile_path(7, 64, 63) == template.format(z=7, x=64, y=63)
    assert alltime_state_path(64, 63) == "alltime-state/64/63.npy"


def test_the_two_manifests_do_not_collide():
    assert ALLTIME_MANIFEST_OBJECT != MANIFEST_OBJECT


# --- reading objects back -----------------------------------------------------------


def test_a_stored_object_reads_back_byte_for_byte():
    uploader = uploader_for(FakeBucket())
    uploader.upload_object("alltime-state/1/2.npy", b"\x93NUMPY-ish", "application/octet-stream")
    assert uploader.download_object("alltime-state/1/2.npy") == b"\x93NUMPY-ish"


def test_a_missing_object_reads_as_none_not_an_error():
    # Supabase Storage answers a missing object with 400 and a 404 in the body;
    # a first run asks for thousands of objects that do not exist yet.
    assert uploader_for(FakeBucket()).download_object("alltime-state/9/9.npy") is None


def test_a_genuine_error_on_a_read_is_not_mistaken_for_absence():
    class Failing(FakeBucket):
        def request(self, method, url, **kwargs):
            return StubResponse(status_code=500, text="internal error")

    with pytest.raises(StorageWriteError, match="500"):
        uploader_for(Failing()).download_object("alltime-state/1/2.npy")


def test_many_objects_are_fetched_in_one_call():
    uploader = uploader_for(FakeBucket())
    uploader.upload_object("a.npy", b"one", "application/octet-stream")
    uploader.upload_object("b.npy", b"two", "application/octet-stream")
    assert uploader.download_objects(["a.npy", "b.npy", "gone.npy"]) == {
        "a.npy": b"one",
        "b.npy": b"two",
        "gone.npy": None,
    }


def test_reading_a_published_manifest():
    uploader = uploader_for(FakeBucket())
    uploader.upload_manifest(
        build_alltime_manifest("2026-08-30", TARGET, 7), ALLTIME_MANIFEST_OBJECT
    )
    assert uploader.read_manifest(ALLTIME_MANIFEST_OBJECT)["since"] == "2026-08-30"


def test_an_absent_or_mangled_manifest_reads_as_none():
    uploader = uploader_for(FakeBucket())
    assert uploader.read_manifest(ALLTIME_MANIFEST_OBJECT) is None

    uploader.upload_object(ALLTIME_MANIFEST_OBJECT, b"{ not json", "application/json")
    assert uploader.read_manifest(ALLTIME_MANIFEST_OBJECT) is None


# --- transport ----------------------------------------------------------------------


def test_tiles_land_at_their_paths_with_the_png_content_type():
    bucket = FakeBucket()
    report = uploader_for(bucket).upload_tiles(
        [("2026-08-30/7/64/63.png", b"png-one"), ("2026-08-30/6/32/31.png", b"png-two")]
    )

    assert (report.uploaded, report.failed) == (2, 0)
    assert bucket.uploaded["2026-08-30/7/64/63.png"] == b"png-one"
    assert bucket.uploaded["2026-08-30/6/32/31.png"] == b"png-two"


def test_daily_tile_uploads_get_the_immutable_cache_header():
    bucket = FakeBucket()
    path = "2026-08-30/7/64/63.png"
    uploader_for(bucket).upload_tiles([(path, b"png-one")], cache_control=TILE_CACHE_CONTROL)

    assert bucket.upload_headers[path]["Cache-Control"] == TILE_CACHE_CONTROL


def test_tile_uploads_are_not_cached_forever_unless_asked():
    # cache_control is opt-in per call, not inferred from content type.
    bucket = FakeBucket()
    path = "2026-08-30/7/64/63.png"
    uploader_for(bucket).upload_tiles([(path, b"png-one")])

    assert "Cache-Control" not in bucket.upload_headers[path]


def test_alltime_pyramid_tile_uploads_do_not_get_the_immutable_header():
    # Unlike a daily tile, an all-time z/x/y.png at an existing path can gain a
    # higher rank -- different bytes -- whenever a new reading beats the record
    # already held there. The frontend has no cache-busting on tile URLs, so an
    # immutable header here would let a stale rank stick for up to a year.
    bucket = FakeBucket()
    path = f"{ALLTIME_TILES_PREFIX}/7/64/63.png"
    uploader_for(bucket).upload_tiles([(path, b"png-one")])

    assert "Cache-Control" not in bucket.upload_headers[path]


def test_state_object_uploads_do_not_get_the_tile_cache_header():
    # All-time state objects are overwritten with improved data as records
    # change, unlike a published tile.
    bucket = FakeBucket()
    path = f"{ALLTIME_STATE_PREFIX}/64/63.npy"
    uploader_for(bucket).upload_tiles([(path, b"\x93NUMPY")], content_type=STATE_CONTENT_TYPE)

    assert "Cache-Control" not in bucket.upload_headers[path]


def test_a_failing_tile_is_retried_then_counted_not_raised():
    bucket = FakeBucket(fail_paths={"2026-08-30/7/1/1.png"})
    objects = [(f"2026-08-30/7/1/{i}.png", b"png") for i in range(40)]

    report = uploader_for(bucket).upload_tiles(objects)

    assert (report.uploaded, report.failed) == (39, 1)
    assert bucket.attempts["2026-08-30/7/1/1.png"] == 5
    assert report.acceptable


def test_losing_more_than_the_tolerance_is_not_acceptable():
    bucket = FakeBucket(fail_paths={f"2026-08-30/7/1/{i}.png" for i in range(3)})
    objects = [(f"2026-08-30/7/1/{i}.png", b"png") for i in range(10)]

    report = uploader_for(bucket).upload_tiles(objects)

    assert report.failed == 3
    assert report.failure_rate > MAX_TILE_FAILURE_RATE
    assert not report.acceptable


def test_uploading_nothing_is_an_empty_report():
    report = uploader_for(FakeBucket()).upload_tiles([])
    assert (report.uploaded, report.failed, report.failure_rate) == (0, 0, 0.0)


def test_the_manifest_goes_to_the_bucket_root_as_json():
    bucket = FakeBucket()
    uploader_for(bucket).upload_manifest(build_manifest(TARGET, tile_count=7))

    assert MANIFEST_OBJECT in bucket.uploaded
    assert b'"tile_count": 7' in bucket.uploaded[MANIFEST_OBJECT]
    # The manifest is republished daily; it must not be told to cache forever.
    assert "Cache-Control" not in bucket.upload_headers[MANIFEST_OBJECT]


def test_a_rejected_upload_surfaces_the_status():
    bucket = FakeBucket(fail_paths={MANIFEST_OBJECT})
    with pytest.raises(StorageWriteError, match="500"):
        uploader_for(bucket).upload_manifest(build_manifest(TARGET, tile_count=0))


# --- pruning against the fake bucket ------------------------------------------------


def bucket_with_three_days() -> FakeBucket:
    keys = {MANIFEST_OBJECT}
    for day in ("2026-08-28", "2026-08-29", "2026-08-30"):
        keys.update({f"{day}/7/64/63.png", f"{day}/7/64/64.png", f"{day}/0/0/0.png"})
    return FakeBucket(keys)


def test_walking_a_date_prefix_finds_every_tile_under_it():
    bucket = bucket_with_three_days()
    found = uploader_for(bucket).walk_objects("2026-08-28")
    assert sorted(found) == [
        "2026-08-28/0/0/0.png",
        "2026-08-28/7/64/63.png",
        "2026-08-28/7/64/64.png",
    ]


def test_pruning_no_longer_deletes_a_stale_day():
    """Forward-looking rewind (decision 2026-09-03): every day's pyramid stays."""
    bucket = bucket_with_three_days()

    deleted = uploader_for(bucket).prune_old_dates(keep=2)

    assert deleted == 0
    assert bucket.deleted == []
    assert MANIFEST_OBJECT in bucket.keys
    assert "2026-08-28/0/0/0.png" in bucket.keys
    assert "2026-08-30/0/0/0.png" in bucket.keys


def test_pruning_a_fresh_bucket_deletes_nothing():
    bucket = FakeBucket({MANIFEST_OBJECT, "2026-08-30/0/0/0.png"})
    assert uploader_for(bucket).prune_old_dates(keep=2) == 0
    assert bucket.deleted == []


@pytest.mark.parametrize("keep", [0, 1, 2, 5])
def test_no_dated_prefix_is_ever_selected_for_deletion_no_matter_how_many_exist(keep):
    bucket = FakeBucket({MANIFEST_OBJECT})
    for day in range(1, 21):
        bucket.keys.add(f"2026-01-{day:02d}/0/0/0.png")
    before = set(bucket.keys)

    deleted = uploader_for(bucket).prune_old_dates(keep=keep)

    assert deleted == 0
    assert bucket.deleted == []
    assert bucket.keys == before
